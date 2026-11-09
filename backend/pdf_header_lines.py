"""Independent readiness for semantic header reading, not column-unit readiness."""
import hashlib
import io
import time
from copy import deepcopy
from typing import Any

VERSION = 'physical-header-lines-v3'


def restore_conflicting_read(header):
    from pdf_table_verification import independent_text_conflict
    original=header.get('raw_recovered_text')
    if (not isinstance(original,str) or not original.strip()
            or not independent_text_conflict(header,header.get('text',''))
            or independent_text_conflict(header,original)):
        return False
    header.setdefault('rejected_header_consensus_text',header['text'])
    header['text']=original
    header['quality']='uncertain'
    header['header_consensus_rejected']='conflicts_with_independent_source_words'
    return True


def candidates(table):
    from pdf_table_verification import verified_header_text
    from header_ocr_model import configured
    model=configured()
    signature=model['sha256'] if model else None
    return [c for c in table['cells'] if table.get('recovery') and c.get('role')=='header'
        and len(c.get('bbox') or [])==4
        and not (c.get('geometry_validation') or {}).get('requires_source_review')
        and not verified_header_text(c)
        and ((c.get('header_line_observation') or {}).get('version')!=VERSION
            or (c.get('header_line_observation') or {}).get('header_best_model_sha256')!=signature)]


def pending_pages(index):
    prepared: Any = index.tables
    return [p for p,tables in enumerate(prepared or []) if any(candidates(t) for t in tables)]


def line_boxes(image):
    """Projection bands retain superscripts that overlap their base text line."""
    import numpy as np
    ink=np.asarray(image.convert('L'))<160
    ys=np.where(ink.sum(axis=1)>0)[0]
    bands=[]
    for y in ys:
        if not bands or y-bands[-1][-1]>6:
            bands.append([int(y)])
        else:
            bands[-1].append(int(y))
    return [[0,max(0,b[0]-3),image.width,min(image.height,b[-1]+4)] for b in bands]


def observe(image, *, deadline, read_rapid, read_tesseract, store_image=None, previous_lines=(),
            read_best=None,model_identity=None):
    from PIL import ImageOps
    from ocr_engine import _to_png
    from pdf_cell_ocr import clean_glyphs
    clean=clean_glyphs(image)
    if clean is None:
        return dict(state='no_readable_ink',lines=[],recognition_verified=False)
    boxes=line_boxes(clean)
    # Large diagram fragments are not semantic table headers.
    if len(boxes)>12:
        return dict(state='line_limit_exceeded',lines=[],recognition_verified=False)
    lines=[]
    for order,box in enumerate(boxes):
        previous=next((line for line in previous_lines if line.get('order')==order
            and line.get('clean_image_bbox')==box),None)
        if previous is not None and (read_best is None or
                previous.get('best_model')==model_identity and len(previous.get('best_variants',[]))==2):
            lines.append(previous)
            continue
        if time.monotonic()>=deadline:
            return dict(state='incomplete',lines=lines,recognition_verified=False)
        cropped=ImageOps.expand(clean.crop((box[0],box[1],box[2],box[3])),border=20,fill='white')
        raw=_to_png(cropped)
        observation: dict[str, Any] = deepcopy(previous) if previous is not None else dict(order=order,clean_image_bbox=box,
            image_sha256=hashlib.sha256(raw).hexdigest(),rapid_words=read_rapid(cropped),
            tesseract_words=read_tesseract(raw),recognition_verified=False)
        if store_image is not None:
            observation['image_key']=store_image(raw).key
        if read_best is not None:
            if observation.get('best_model')!=model_identity:
                observation.update(best_model=model_identity,best_variants=[])
            for scale in (1,1.35):
                if any(v.get('scale')==scale for v in observation['best_variants']):
                    continue
                if time.monotonic()>=deadline:
                    return dict(state='incomplete',lines=[*lines,observation],recognition_verified=False)
                resized=cropped.resize((round(cropped.width*scale),round(cropped.height*scale)))
                observation['best_variants'].append(dict(scale=scale,words=read_best(_to_png(resized))))
        lines.append(observation)
    return dict(state='observed',lines=lines,recognition_verified=False)


def complete(page,tables,*,rotation=0,load_image=None,store_image=None,seconds=45,max_headers=32,checkpoint=None,**_):
    import pymupdf
    import ocr_engine
    from PIL import Image
    from pdf_cell_ocr import _rapid_numeric
    from header_ocr_model import configured
    model=configured()
    deadline=time.monotonic()+seconds
    history=[]
    private=pymupdf.open()
    try:
        private.insert_pdf(page.parent,from_page=page.number,to_page=page.number,annots=False,links=False)
        rendered=private[0]
        rendered.set_rotation(0)
        for table in tables:
            for header in table['cells']:
                if header.get('role')=='header':
                    restore_conflicting_read(header)
            attempted=0
            for header in candidates(table):
                if attempted>=max_headers or time.monotonic()>=deadline:
                    break
                attempted+=1
                proof=header.get('header_refinement') or {}
                observation=dict(version=VERSION,header_id=header['cell_id'],bbox=list(header['bbox']),
                    unit_proof_independent=True,recognition_verified=False,
                    header_best_model_sha256=model['sha256'] if model else None)
                try:
                    if (load_image is not None and proof.get('image_key') and proof.get('bbox')==header['bbox']):
                        raw=load_image(proof['image_key'])
                        if hashlib.sha256(raw).hexdigest()!=proof.get('image_sha256'):
                            raise ValueError('Header image hash mismatch')
                    else:
                        box=pymupdf.Rect(header['bbox'])
                        if box.width*box.height*(600/72)**2>4_000_000:
                            raise ValueError('Header image exceeds pixel budget')
                        image=ocr_engine._rotate(ocr_engine._pil_from_pixmap(rendered.get_pixmap(dpi=600,clip=box,alpha=False)),
                            (rotation-page.rotation)%360)
                        raw=ocr_engine._to_png(image)
                    observation['image_sha256']=hashlib.sha256(raw).hexdigest()
                    if store_image is not None:
                        observation['image_key']=store_image(raw).key
                    def read(data):
                        return ocr_engine.parse_tsv(ocr_engine.run_tsv(data,language='rus',psm=7,
                            timeout=max(.1,min(5,deadline-time.monotonic()))))
                    def read_best(data):
                        assert model is not None
                        return ocr_engine.parse_tsv(ocr_engine.run_tsv(data,language='rus',psm=7,
                            tessdata_dir=model['tessdata_dir'],timeout=max(.1,min(5,deadline-time.monotonic()))))
                    previous=header.get('header_line_observation') or {}
                    prior=previous.get('lines',[]) if previous.get('image_sha256')==observation['image_sha256'] else []
                    observation.update(observe(Image.open(io.BytesIO(raw)),deadline=deadline,
                        read_rapid=_rapid_numeric,read_tesseract=read,store_image=store_image,previous_lines=prior,
                        read_best=read_best if model else None,model_identity=model))
                except (ValueError,RuntimeError,OSError) as exc:
                    observation.update(state='read_failed',error=type(exc).__name__,lines=[])
                if observation.get('state')=='incomplete':
                    observation['attempted_version']=observation.pop('version')
                header['header_line_observation']=observation
                history.append(dict(table_id=table['table_id'],header_id=header['cell_id'],state=observation['state']))
    finally:
        private.close()
    remaining={t['table_id']:[h['cell_id'] for h in candidates(t)] for t in tables if candidates(t)}
    return dict(state='incomplete_semantic_observation' if remaining else 'semantic_observation_complete',
        remaining=remaining,observations=history,recognition_correctness_asserted=False)
