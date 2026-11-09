"""Bounded independent OCR of ordered value/unit pairs in physical row cells."""
import hashlib
import re
import time
import unicodedata
from decimal import Decimal
from typing import Any

VERSION='physical-paired-cell-ocr-v6'
UNIT_LANGUAGE_POLICY='paired-units-rus-v1'
BASE_PLANS=((48,7),(64,7),(96,7),(32,7),(48,13),(64,13))


def literal(text, kind):
    from audit_measurements import paired_units
    text=unicodedata.normalize('NFKC',str(text)).strip()
    if kind=='units':
        # Alias whole unit atoms only. Never rewrite letters in quantities,
        # material codes or surrounding prose; slash order is unchanged.
        text='/'.join({'m':'м','t':'т'}.get(part.strip().casefold(),part.strip())for part in text.split('/'))
        units=paired_units(text)
        return tuple(u[0]for u in units)if units and len(set(units))==len(units)else None
    parts=re.split(r'\s*/\s*',text)
    if not 2<=len(parts)<=3 or any(not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?',p)for p in parts):
        return None
    return tuple(Decimal(p.replace(',','.'))for p in parts)


def same_cell(proof,cell,kind):
    return (proof.get('version')==VERSION and proof.get('kind')==kind
        and proof.get('cell_id')==cell['cell_id'] and proof.get('bbox')==cell.get('bbox')
        and proof.get('literal_text')==cell['text'])


def superseded_mixed_language(proof,expected,kind):
    """Retire only unparsable old bilingual reads after a complete Russian reread.

    All old observations remain stored. A different valid ordered unit is
    always a conflict, even if its original language configuration was wrong.
    """
    if kind!='units' or proof.get('unit_language_policy')!=UNIT_LANGUAGE_POLICY:
        return set()
    variants=proof.get('variants',[])
    replacements={(v.get('height'),v.get('psm')):v for v in variants
        if v.get('language')=='rus'and not v.get('model_sha256')and not v.get('model_id')}
    if not all(plan in replacements and literal(' '.join(w['text']for w in
            replacements[plan].get('words',[])),kind)==expected for plan in BASE_PLANS):
        return set()
    return {i for i in proof.get('superseded_mixed_language_indices',[])
        if type(i)is int and 0<=i<len(variants)
        and not variants[i].get('model_sha256')and not variants[i].get('model_id')
        and variants[i].get('language','rus+eng')=='rus+eng'}


def consensus(proof,kind):
    """Read only independent OCR observations, without an expected VOR value."""
    rapid=proof.get('rapid_words')or[]
    if not rapid or min(w.get('confidence',0)for w in rapid)<.9:
        return None
    expected=literal(' '.join(w['text']for w in rapid),kind)
    if expected is None:
        return None
    for attempt in proof.get('rapid_attempts',[]):
        words=attempt.get('words')or[]
        if (words and min(w.get('confidence',0)for w in words)>=.9
                and literal(' '.join(w['text']for w in words),kind)!=expected):
            return None
    votes=set()
    superseded=superseded_mixed_language(proof,expected,kind)
    for index,variant in enumerate(proof.get('variants',[])):
        if variant.get('model_id'):
            from pdf_paired_models import MODELS
            pinned=next((m for m in MODELS if m['id']==variant['model_id']),None)
            if (not pinned or variant.get('model_sha256')!=pinned['sha256']
                    or variant.get('oem')!=pinned['oem']
                    or variant.get('preprocessing')not in {'gray','binary'}
                    or variant.get('resampling','lanczos')not in {'lanczos','nearest'}
                    or not variant.get('transformed_image_key')
                    or not isinstance(variant.get('transformed_image_sha256'),str)
                    or not re.fullmatch(r'[0-9a-f]{64}',variant.get('transformed_image_sha256',''))):
                return None
        words=variant.get('words')or[]
        if not words:
            continue
        confidence=min(w.get('conf',0)for w in words)
        reading=literal(' '.join(w['text']for w in words),kind)
        if index in superseded and reading in {None,expected}:
            continue
        if confidence>=75 and reading!=expected:
            return None
        height=variant.get('height')
        if confidence>=90 and reading==expected and type(height)is int and height>0:
            votes.add(height)
    return expected if len(votes)>=2 else None


def verified_reading(cell, kind):
    proof=cell.get('paired_ocr')or{}
    expected=literal(cell['text'],kind)
    if (not expected or not same_cell(proof,cell,kind)
            or not proof.get('image_sha256')
            or not proof.get('image_key') or proof.get('state')!='observed'):
        return False
    if consensus(proof,kind)!=expected:
        return False
    original=cell.get('words')or[]
    if original and min(w.get('confidence',0)for w in original)>=.9:
        observed=literal(' '.join(w.get('text','')for w in original),kind)
        if observed is not None and observed!=expected:
            return False
    return True


def verified_pair(table, pair):
    cells={c['cell_id']:c for c in table['cells']}
    return all(verified_reading(cells[pair[key]],kind)
        for key,kind in [('unit_cell_id','units'),('value_cell_id','values')])


def pending_cells(table):
    from pdf_row_measurements import row_candidates
    from pdf_paired_models import signature
    model_signature=signature()
    pending={}
    for unit,value in row_candidates(table,table['cells']):
        if all(c.get('origin',table.get('origin'))=='native'for c in (unit,value)):
            continue
        for cell,kind in ((unit,'units'),(value,'values')):
            proof=cell.get('paired_ocr')or{}
            if (not same_cell(proof,cell,kind) or proof.get('state')not in {'observed','failed'}
                    or proof.get('state')=='observed'and(not proof.get('image_key')or not proof.get('image_sha256'))
                    or proof.get('model_signature')!=model_signature and not verified_reading(cell,kind)):
                pending[cell['cell_id']]=(cell,kind)
    return list(pending.values())


def complete(page, tables, *, rotation=0, cache_dir=None, store_image=None, seconds=20,max_cells=16):
    import pymupdf
    import ocr_engine
    from PIL import Image,ImageOps
    from pdf_cell_ocr import clean_glyphs,_rapid_numeric
    deadline=time.monotonic()+seconds
    attempted=0
    private=pymupdf.open()
    try:
        original_rotation=page.rotation
        private.insert_pdf(page.parent,from_page=page.number,to_page=page.number,annots=False,links=False)
        render_page=private[0]
        render_page.set_rotation(0)
        for table in tables:
            for cell,kind in pending_cells(table):
                if attempted>=max_cells or time.monotonic()>=deadline:
                    break
                attempted+=1
                proof: dict[str,Any]=cell.get('paired_ocr')or{}
                # Reuse the original crop and every previous OCR conflict when
                # adding pinned local models to a completed but uncertain read.
                compatible=(proof.get('version')in {VERSION,'physical-paired-cell-ocr-v3',
                    'physical-paired-cell-ocr-v4','physical-paired-cell-ocr-v5'}
                    and all(proof.get(key)==expected for key,expected in (
                        ('kind',kind),('cell_id',cell['cell_id']),('bbox',cell.get('bbox')),('literal_text',cell['text'])))
                    and proof.get('state')in {'observed','in_progress'})
                if not compatible:
                    proof=dict(version=VERSION,kind=kind,cell_id=cell['cell_id'],bbox=list(cell['bbox']),
                        literal_text=cell['text'],prior_text=cell['text'],state='in_progress',variants=[])
                if kind=='units'and proof.get('unit_language_policy')!=UNIT_LANGUAGE_POLICY:
                    proof['superseded_mixed_language_indices']=[i for i,v in enumerate(proof['variants'])
                        if not v.get('model_sha256')and not v.get('model_id')
                        and v.get('language','rus+eng')=='rus+eng']
                    proof['unit_language_policy']=UNIT_LANGUAGE_POLICY
                from pdf_paired_models import signature
                proof.update(version=VERSION,state='in_progress',model_signature=signature())
                cell['paired_ocr']=proof
                try:
                    box=pymupdf.Rect(cell['bbox'])
                    if box.is_empty or not render_page.rect.contains(box)or box.get_area()*(600/72)**2>4_000_000:
                        raise ValueError('Invalid paired-cell crop')
                    image=ocr_engine._rotate(ocr_engine._pil_from_pixmap(render_page.get_pixmap(dpi=600,clip=box,alpha=False)),
                        (rotation-original_rotation)%360)
                    raw=ocr_engine._to_png(image)
                    digest=hashlib.sha256(raw).hexdigest()
                    if proof.get('image_sha256')and proof['image_sha256']!=digest:
                        raise ValueError('Paired-cell crop changed during resume')
                    proof.update(image_sha256=digest,dpi=600,rotation=(rotation-original_rotation)%360)
                    if store_image is None:
                        raise ValueError('Paired-cell evidence requires durable crop storage')
                    if not proof.get('image_key'):
                        proof['image_key']=store_image(raw).key
                    clean=clean_glyphs(image)
                    if clean is None:
                        raise ValueError('Paired-cell crop has no readable ink')
                    if 'rapid_words'not in proof:
                        proof['rapid_words']=_rapid_numeric(clean)
                        proof['rapid_attempts']=[dict(height=0,padding=0,words=proof['rapid_words'])]
                    proof.setdefault('rapid_attempts',[dict(height=0,padding=0,words=proof['rapid_words'])])
                    if kind=='units':
                        for height,padding in ((32,0),(48,8)):
                            if any(a.get('height')==height and a.get('padding')==padding for a in proof['rapid_attempts']):
                                continue
                            if time.monotonic()>=deadline:
                                break
                            scaled=clean.resize((max(1,round(clean.width*height/clean.height)),height),Image.Resampling.LANCZOS)
                            words: list[dict[str,Any]]=_rapid_numeric(ImageOps.expand(scaled,border=padding,fill='white'))
                            proof['rapid_attempts'].append(dict(height=height,padding=padding,words=words))
                            if words and min(w.get('confidence',0)for w in words)>min((w.get('confidence',0)for w in proof['rapid_words']),default=0):
                                proof['rapid_words']=words
                    plans: list[tuple[int,int,dict[str,str]|None]]=[(h,p,None)for h,p in BASE_PLANS]
                    if kind=='units':
                        from header_ocr_model import configured
                        model=configured()
                        if model:
                            proof['best_model']=model
                            plans.extend((height,psm,model)for height,psm in ((32,7),(96,13)))
                    for height,psm,model in plans:
                        model_sha=model['sha256']if model else None
                        language='rus'if kind=='units'else'eng'
                        previous_language='rus'if model else'eng'if kind=='values'else'rus+eng'
                        if any(v.get('height')==height and v.get('psm')==psm and v.get('model_sha256')==model_sha
                                and v.get('language',previous_language)==language
                                for v in proof['variants']):
                            continue
                        if time.monotonic()>=deadline:
                            break
                        scaled=clean.resize((max(1,round(clean.width*height/clean.height)),height),Image.Resampling.LANCZOS)
                        scaled=ImageOps.expand(scaled,border=20,fill='white')
                        words=ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(scaled),
                            language=language,psm=psm,
                            **({'tessdata_dir':model['tessdata_dir']}if model else{}),
                            timeout=max(.1,min(3,deadline-time.monotonic()))))
                        proof['variants'].append(dict(height=height,psm=psm,model_sha256=model_sha,language=language,words=words))
                    else:
                        proof['state']='observed'
                    if proof['state']=='observed'and (consensus(proof,kind)is None or proof.get('supplementary_started')):
                        from pdf_paired_models import variants
                        supplementary=list(variants(kind))
                        if supplementary:
                            proof['supplementary_started']=True
                        gray=None
                        for plan in supplementary:
                            model=plan['model']
                            height,psm=plan['height'],plan['psm']
                            mode=plan['preprocessing']
                            resampling=plan.get('resampling','lanczos')
                            if any(v.get('height')==height and v.get('psm')==psm
                                    and v.get('model_sha256')==model['sha256']
                                    and v.get('preprocessing','binary')==mode
                                    and v.get('resampling','lanczos')==resampling for v in proof['variants']):
                                continue
                            if time.monotonic()>=deadline:
                                proof['state']='in_progress'
                                break
                            if mode=='gray'and gray is None:
                                gray=clean_glyphs(image,preserve_gray=True)
                            optical=gray if mode=='gray'else clean
                            if optical is None:
                                raise ValueError('Paired-cell crop has no readable interior glyphs')
                            sampling={'lanczos':Image.Resampling.LANCZOS,'nearest':Image.Resampling.NEAREST}[resampling]
                            scaled=optical.resize((max(1,round(optical.width*height/optical.height)),height),sampling)
                            scaled=ImageOps.expand(scaled,border=20,fill='white')
                            transformed=ocr_engine._to_png(scaled)
                            transformed_ref=store_image(transformed)
                            words=ocr_engine.parse_tsv(ocr_engine.run_tsv(transformed,language=model['language'],
                                psm=psm,oem=model['oem'],tessdata_dir=model['tessdata_dir'],
                                timeout=max(.1,min(3,deadline-time.monotonic()))))
                            proof['variants'].append(dict(height=height,psm=psm,words=words,preprocessing=mode,resampling=resampling,
                                language=model['language'],
                                model_id=model['id'],model_sha256=model['sha256'],oem=model['oem'],
                                transformed_image_key=transformed_ref.key,
                                transformed_image_sha256=hashlib.sha256(transformed).hexdigest()))
                    # Correct an initially unparseable OCR unit only after the
                    # independent engines establish a literal, ordered reading.
                    reading=consensus(proof,kind)
                    if kind=='units'and literal(cell['text'],kind)is None and reading and proof['state']=='observed':
                        original=cell.get('words')or[]
                        if not any(w.get('confidence',0)>=.9 and literal(w.get('text',''),kind)not in {None,reading}for w in original):
                            cell.setdefault('raw_text_before_paired_ocr',cell['text'])
                            cell['text']='/'.join(map(str,reading))
                            proof['literal_text']=cell['text']
                except (RuntimeError,ValueError)as exc:
                    proof.update(state='failed',error=str(exc)[:250])
    finally:
        private.close()
    return dict(attempted=attempted,remaining={t['table_id']:[c['cell_id']for c,_ in pending_cells(t)]
        for t in tables if pending_cells(t)})
