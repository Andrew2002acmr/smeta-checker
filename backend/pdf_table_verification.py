"""Local evidence checks for newly recovered physical raster tables.

Only physical cell crops are read. There is no VOR amount, expected unit or
normative input. Source OCR words remain intact beside independent crop reads.
"""
import hashlib
import re
import time
import unicodedata
from typing import Any

VERSION = 'physical-header-ocr-v2'
SEMANTIC_VERSION = 'physical-semantic-header-ocr-v1'
VISUAL_VERSION = 'independent-visual-header-v1'


def _clean(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).casefold()).strip()


def header_text(words):
    """Keep reading order and reject incomplete low-confidence crop results."""
    if not words:
        return None
    output=[]
    for word in words:
        text=word['text'].strip()
        if not text:
            continue
        minimum=80 if re.search(r'\d', text) else 65
        if word.get('conf', 0)<minimum:
            return None
        output.append(text)
    return ' '.join(output) if output else None


def header_consensus(variants):
    if len(variants)!=2:
        return None
    texts=[header_text(v['words']) for v in variants]
    if not all(texts) or len({_clean(t) for t in texts if t})!=1:
        return None
    return texts[0]


def visual_header_text(header):
    """An explicit visual reading bound to the unchanged physical crop.

    Preparation checks source bytes and page geometry before saving this
    envelope. It is not OCR consensus or an inferred engineering requirement.
    """
    proof=header.get('visual_transcription') or {}
    original=header.get('header_refinement') or {}
    if (proof.get('version')!=VISUAL_VERSION or header.get('role')!='header'
            or any(proof.get(k)!=header.get(k,1 if k=='colspan' else None)
                for k in ('cell_id','bbox','row','column','colspan'))
            or proof.get('text')!=header.get('text') or not proof.get('text','').strip()
            or proof.get('original_text')!=header.get('raw_recovered_text')
            or not original.get('image_sha256') or proof.get('crop_image_sha256')!=original['image_sha256']
            or type(proof.get('page')) is not int or proof['page']<1
            or not proof.get('document_version_id') or not proof.get('reviewer') or not proof.get('method')
            or any(not re.fullmatch('[0-9a-f]{64}',str(proof.get(k,'')))
                for k in ('pdf_sha256','review_sha256','rendered_page_sha256'))):
        return ''
    return proof['text']


def verified_header_text(header):
    """Reuse text only with the same physical header and two complete reads."""
    visual=visual_header_text(header)
    if visual:
        return visual
    proof=header.get('header_refinement') or {}
    if (proof.get('semantic_version')!=SEMANTIC_VERSION
            or proof.get('header_id')!=header['cell_id']
            or proof.get('bbox')!=header['bbox']
            or proof.get('column')!=header['column']
            or proof.get('row')!=header['row']
            or proof.get('colspan')!=header.get('colspan',1)
            or [v.get('scale') for v in proof.get('variants',[])]!=[1,1.35]):
        return ''
    confirmed=header_consensus(proof['variants'])
    if confirmed is None:
        from pdf_header_geometry import geometry_text
        confirmed=geometry_text(proof)
    return confirmed if confirmed and _clean(confirmed)==_clean(header['text']) and not independent_text_conflict(header,confirmed) else ''


def independent_text_conflict(header, text):
    """Two scales of one engine must not outvote a conflicting source read."""
    def tokens(value):
        value=unicodedata.normalize('NFKC',value).casefold().translate(str.maketrans(
            {'a':'а','b':'в','c':'с','e':'е','k':'к','m':'м','o':'о','p':'р','t':'т','x':'х'}))
        return set(re.findall(r'[а-яёa-z0-9]+(?:[.,/]\d+)*',value))
    observed=tokens(text)
    return any(word.get('confidence',0)>=.95 and not tokens(word.get('text','')).issubset(observed)
        for word in header.get('words',[]) if word.get('text','').strip())


def refresh_header_ancestry(table):
    """Link recovered physical headers to their own descendant columns."""
    headers=[h for h in table['cells'] if h.get('role')=='header' and (h['text']
        or ((table.get('column_units') or {}).get(str(h['column'])) or {}).get('header_id')==h['cell_id'])]
    by_id={h['cell_id']:h for h in headers}
    for cell in table['cells']:
        extra=[h['cell_id'] for h in headers if h['row']<cell['row']
            and h['column']<=cell['column']
            and h['column']+h.get('colspan',1)>=cell['column']+cell.get('colspan',1)]
        ancestry=list(dict.fromkeys([*cell.get('header_ids',[]),*extra]))
        cell['header_ids']=sorted(ancestry,key=lambda key:(
            by_id.get(key,{}).get('row',-1),by_id.get(key,{}).get('column',-1),key))


def header_unit(words):
    """Literal unit in this header; a thickness value is not the column unit."""
    from pdf_cell_ocr import literal_unit
    if not words:
        return None
    units=[]
    consumed=set()
    for index, word in enumerate(words):
        token=_clean(word['text']).strip('.,;:()')
        if token not in {'м','m'}:
            continue
        powers=[(j,w) for j,w in enumerate(words) if j!=index and _clean(w['text']) in {'2','3'}
            and 0 <= w['left']-(word['left']+word['width']) <= max(word['width'],word['height'])*.8
            and literal_unit([word,w])]
        if len(powers)==1:
            j,power=powers[0]
            units.append(literal_unit([word,power]))
            consumed.update((index,j))
    previous=''
    for index, word in enumerate(words):
        token=_clean(word['text'])
        if index not in consumed:
            for match in re.finditer(r'^(?:м[23]|m[23]|кг|шт|т|м|m)$', token.strip('.,;:()')):
                before=previous+' '+token[:match.start()]
                # "h=0,20 м" / "толщиной 0,20 м" is geometry, not a unit
                # declaration. A code followed by comma, e.g. "БР..., м", is
                # still a literal unit declaration in this same physical cell.
                if re.search(r'\d(?:[.,]\d+)?\s*$', before):
                    continue
                if word.get('conf',0)<80:
                    continue
                if match[0] in {'м','m'} and any(j not in consumed and j!=index
                        and _clean(w['text']) in {'2','3'} for j,w in enumerate(words)):
                    continue  # An unattached exponent is unresolved, not metres.
                units.append(match[0].replace('m','м'))
        previous=(previous+' '+token)[-80:]
    return units[0] if units and len(set(units))==1 else None


def verified_column_unit(table, cell, proof):
    boundary=_detected_header_boundary(table)
    if (proof.get('version')!=VERSION or proof.get('column')!=cell['column']
            or cell.get('colspan',1)!=1):
        return ''
    header=next((h for h in table['cells'] if h['cell_id']==proof.get('header_id')
        and h['cell_id'] in cell.get('header_ids',[]) and h.get('colspan',1)==1
        and h['column']==cell['column'] and h.get('role')=='header'
        and (boundary is None or h['row']<boundary)),None)
    if header is None or proof.get('bbox')!=header['bbox'] or len(proof.get('variants',[]))!=2:
        return ''
    votes=[header_unit(v['words']) for v in proof['variants']]
    crop=proof.get('unit_crop')
    if crop:
        from pdf_cell_ocr import literal_unit
        expected=header_unit_region(proof['variants'],*crop.get('image_size',(0,0)))
        if crop.get('component_reading'):
            from pdf_unit_components import verified_component_unit
            unit = verified_component_unit(crop)
            if (expected == crop.get('region') and unit == proof.get('unit')
                    and not any(v and v != unit for v in votes)):
                return unit
        variants=crop.get('variants',[])
        readings=[literal_unit(v['words']) for v in variants]
        if proof.get('unit')=='м' and plain_m_proof(proof,crop):
            readings=['м','м']
        heights=[v.get('height') for v in variants]
        attempts=crop.get('attempts')
        retries_valid=(heights==[48,64] if attempts is None else
            [v.get('height') for v in attempts] in ([48,64,96],[48,64,96,128])
            and all(v in attempts for v in variants)
            and all(not literal_unit(v['words']) or literal_unit(v['words'])==proof.get('unit') for v in attempts))
        if (expected is None or crop.get('region')!=expected
                or len(heights)!=2 or len(set(heights))!=2 or not retries_valid
                or any(value!=proof.get('unit') for value in readings)
                or any(v and v!=proof.get('unit') for v in votes)):
            return ''
        return proof['unit'] if proof.get('unit') in {'м','м2','м3'} else ''
    return proof['unit'] if votes==[proof.get('unit')]*2 and proof.get('unit') else ''


def unit_region(words,width,height):
    """Locate a proposed unit without deciding its exponent or its value."""
    candidates=[(i,w) for i,w in enumerate(words)
        if re.fullmatch(r'[мm](?:[237зz]|[^\w\s])?',_clean(w['text']))]
    if len(candidates)!=1 or width<=0 or height<=0:
        return None
    index,base=candidates[0]
    nearby=[w for i,w in enumerate(words) if i!=index and _clean(w['text']) in {'2','3','?'}
        and 0 <= w['left']-base['left']-base['width'] <= max(base['width'],base['height'])*.8
        and w['top'] < base['top']+base['height']/2]
    selected=[base,*nearby]
    # First header variant has scale 1 and a 20px white border. Never allow
    # PIL's black out-of-image padding to alter the glyphs at a crop edge.
    box=[max(0,min(w['left'] for w in selected)-24),
        max(0,min(w['top'] for w in selected)-24),
        min(width,max(w['left']+w['width'] for w in selected)-16),
        min(height,max(w['top']+w['height'] for w in selected)-16)]
    return box if box[2]>box[0] and box[3]>box[1] else None


def header_unit_region(variants, width, height):
    """Either OCR scale can propose a crop; neither supplies its unit value."""
    for variant in variants:
        scale = variant.get('scale', 1)
        if scale not in (1, 1.35):
            continue
        region = unit_region(variant['words'], round(width * scale), round(height * scale))
        if region:
            return [max(0, round(region[0] / scale)), max(0, round(region[1] / scale)),
                min(width, round(region[2] / scale)), min(height, round(region[3] / scale))]
    return None


def plain_m_region(words,width,height):
    """Include the physical superscript neighbourhood, including unread ink."""
    candidates=[(i,w) for i,w in enumerate(words) if _clean(w['text']) in {'м','m'}]
    if len(candidates)!=1 or any(_clean(w['text']) in {'2','3','?'} for w in words):
        return None
    index,base=candidates[0]
    contextual=[{**w,'conf':max(80,w.get('conf',0))} if i==index else w for i,w in enumerate(words)]
    if header_unit(contextual)!='м':
        return None  # A numerical thickness followed by m is not the column unit.
    x,y=base['left']-20,base['top']-20
    w,h=base['width'],base['height']
    margin=max(w,h)*1.3
    box=[max(0,int(x-4)),max(0,int(y-margin)),min(width,int(x+w+margin)),min(height,int(y+h+4))]
    return box if box[2]>box[0] and box[3]>box[1] else None


def plain_m_clearance(clean,words):
    import cv2
    import numpy as np
    region=plain_m_region(words,clean.width,clean.height)
    if region is None:
        return None
    gray=np.array(clean.crop(region).convert('L'))
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    _,_,stats,_=cv2.connectedComponentsWithStats(ink)
    components=[list(map(int,s)) for s in stats[1:] if s[4]>=3]
    return dict(region=region,components=components)


def plain_m_proof(proof,crop):
    clearance=crop.get('plain_m_clearance') or {}
    components=clearance.get('components') or []
    region=plain_m_region(proof['variants'][0]['words'],*crop.get('image_size',(0,0)))
    if (region is None or clearance.get('region')!=region or len(components)!=1
            or len(components[0])!=5 or components[0][4]<3):
        return False
    confidences=[]
    for variant in crop.get('variants',[]):
        words=variant['words']
        if len(words)!=1 or _clean(words[0]['text']) not in {'м','m'}:
            return False
        confidences.append(words[0].get('conf',0))
    return len(confidences)==2 and min(confidences)>=80 and max(confidences)>=90


def _refine_unit(clean,proof,deadline,cache_dir,store_image):
    from pathlib import Path
    from PIL import Image,ImageOps
    import ocr_engine
    from pdf_cell_ocr import literal_unit
    if not proof['variants']:
        return None
    region=header_unit_region(proof['variants'],clean.width,clean.height)
    if region is None:
        return None
    crop=clean.crop(region)
    raw=ocr_engine._to_png(crop)
    digest=hashlib.sha256(raw).hexdigest()
    result:dict[str,Any]=dict(region=region,image_size=[clean.width,clean.height],image_sha256=digest,variants=[])
    if store_image is not None:
        result['image_key']=store_image(raw).key
    if cache_dir is not None:
        directory=Path(cache_dir)/'header-images'
        directory.mkdir(parents=True,exist_ok=True)
        (directory/(digest+'.png')).write_bytes(raw)
    for height in (48,64):
        if time.monotonic()>=deadline:
            break
        scaled=crop.resize((max(1,round(crop.width*height/crop.height)),height),Image.Resampling.LANCZOS)
        scaled=ImageOps.expand(scaled,border=20,fill='white')
        words=ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(scaled),language='rus+eng',
            psm=6,timeout=max(.1,min(3,deadline-time.monotonic()))))
        result['variants'].append(dict(height=height,words=words))
    proof['unit_crop']=result
    votes=[literal_unit(v['words']) for v in result['variants']]
    if len(votes)==2 and votes[0] and votes[0]==votes[1]:
        return votes[0]
    result['plain_m_clearance']=plain_m_clearance(clean,proof['variants'][0]['words'])
    if plain_m_proof(proof,result):
        return 'м'
    # Keep initial failures; larger crops may resolve a tiny superscript but
    # must never outvote a contradictory, confidently read unit.
    attempts=list(result['variants'])
    if len(attempts)!=2:
        return None
    for height in (96,128):
        if time.monotonic()>=deadline:
            break
        scaled=crop.resize((max(1,round(crop.width*height/crop.height)),height),Image.Resampling.LANCZOS)
        scaled=ImageOps.expand(scaled,border=20,fill='white')
        words=ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(scaled),language='rus+eng',
            psm=6,timeout=max(.1,min(3,deadline-time.monotonic()))))
        attempts.append(dict(height=height,words=words))
        result['attempts']=attempts
        readable=[(literal_unit(v['words']),v) for v in attempts if literal_unit(v['words'])]
        units={unit for unit,_ in readable}
        if len(units)>1:
            return None
        if len(readable)>=2:
            result['variants']=[v for _,v in readable[:2]]
            return readable[0][0]
    from pdf_unit_components import read_components, verified_component_unit
    result['component_reading'] = read_components(crop, deadline)
    return verified_component_unit(result)


def verify_page_numbers(tables,words):
    """Reuse independent page OCR words, never an old cell's approval flag."""
    from normative_parameters import number
    from copy import deepcopy
    for table in tables:
        if not table.get('recovery'):
            continue
        for cell in table['cells']:
            value=number(cell['text'])
            rapid=cell.get('words') or []
            if (cell.get('numeric_verified') or value is None or cell.get('quality')!='usable'
                    or not cell.get('header_ids') or not rapid
                    or any(w.get('confidence',0)<.9 for w in rapid)):
                continue
            box=cell['bbox']
            selected=[]
            for word in words:
                b=word.get('bbox') or []
                if (len(b)!=4 or word.get('conf',0)<75 or word.get('table_id')
                        or word.get('role','word') not in {'word','cell'}
                        or word.get('origin','ocr')!='ocr'):
                    continue
                if box[0]<=b[0]<b[2]<=box[2] and box[1]<=b[1]<b[3]<=box[3]:
                    candidate=number(word.get('text',''))
                    if candidate is not None:
                        selected.append(word)
            if (not selected or any(number(w['text'])!=value for w in selected)
                    or not any(w.get('conf',0)>=90 for w in selected)):
                continue
            cell['numeric_verified']=True
            cell['numeric_page_evidence']=dict(version='independent-page-ocr-v1',
                bbox=list(box),value=cell['text'],rapid_words=deepcopy(rapid),
                words=[{k:w[k] for k in ('text','bbox','conf','source_id') if k in w} for w in selected])


def _detected_header_boundary(table):
    from pdf_tables import physical_header_rows
    return physical_header_rows([{**c,'text':c.get('raw_recovered_text',c['text'])} for c in table['cells']])


def _ordered_headers(table):
    boundary=_detected_header_boundary(table)
    headers=[h for h in table['cells'] if h.get('role')=='header'
        and (boundary is None or h['row']<boundary)]
    deepest={}
    for header in headers:
        if header.get('colspan',1)!=1:
            continue
        previous=deepest.get(header['column'])
        if previous is None or header['row']>previous['row']:
            deepest[header['column']]=header
    primary=sorted(deepest.values(),key=lambda h:h['column'])
    ids={h['cell_id'] for h in primary}
    # Geometry/material declarations are often merged over area and volume.
    # Read their own crops after unit leaves, including initially empty cells.
    merged=sorted((h for h in headers if h.get('colspan',1)>1),
        key=lambda h:(-h['row'],bool(h['text'].strip()),h['column']))
    return primary+merged+[h for h in headers if h['cell_id'] not in ids and h.get('colspan',1)==1]


def reclassify_recovered_headers(table):
    """Correct an old header boundary without rebuilding the physical grid."""
    from pdf_tables import finish_table
    boundary=_detected_header_boundary(table)
    previous=table.get('header_rows',0)
    if boundary is None or not 0<boundary<previous:
        return
    rebuilt=finish_table(table['cells'],table_id=table['table_id'],page=table['page'],bbox=table['bbox'],
        origin=table.get('origin','ocr'),header_rows=boundary,reasons=table.get('reasons',[]))
    table.update(rebuilt)
    headers={c['cell_id'] for c in table['cells'] if c['role']=='header'}
    table['column_units']={key:proof for key,proof in (table.get('column_units') or {}).items()
        if proof.get('header_id') in headers}
    table['header_boundary_repair']=dict(previous=previous,current=boundary,reason='literal_body_row')


def needs_unit_component_pass(table):
    """Retry only proposed unit crops lacking the current component reader."""
    from pdf_unit_components import VERSION as COMPONENT_VERSION
    for header in table['cells']:
        if header.get('role') != 'header' or header.get('colspan', 1) != 1:
            continue
        if (header.get('geometry_validation') or {}).get('requires_source_review'):
            continue
        crop = (header.get('header_refinement') or {}).get('unit_crop') or {}
        variants = (header.get('header_refinement') or {}).get('variants') or []
        proposal = crop or header_unit_region(variants, 1_000_000, 1_000_000)
        if not proposal or (crop.get('component_reading') or {}).get('version') == COMPONENT_VERSION:
            continue
        proof = (table.get('column_units') or {}).get(str(header['column'])) or {}
        cells = [cell for cell in table['cells'] if cell.get('role') == 'cell'
            and cell['column'] == header['column']]
        if cells and not any(verified_column_unit(table, cell, proof) for cell in cells):
            return True
    return False


def verify_recovered_tables(page, tables, *, rotation=0, cache_dir=None, store_image=None,
                            seconds=45, max_headers=16, max_numbers=12, independent_words=(),numeric_seconds=45,
                            resume_only=False):
    """Refine new staging tables in place, retaining all crop and raw evidence."""
    import pymupdf
    import ocr_engine
    from PIL import Image, ImageOps
    from pathlib import Path
    from pdf_cell_ocr import clean_glyphs, refine_tables
    deadline=time.monotonic()+seconds
    original_rotation=page.rotation
    private=pymupdf.open()
    try:
        # Even a set/restore rotation roundtrip can add an explicit /Rotate 0
        # to the source xref. Keep the caller's canonical PDF bytes untouched.
        private.insert_pdf(page.parent,from_page=page.number,to_page=page.number,annots=False,links=False)
        render_page=private[0]
        render_page.set_rotation(0)
        for table in tables:
            if not table.get('recovery'):
                continue
            reclassify_recovered_headers(table)
            table_attempted=skipped=0
            ordered_headers=_ordered_headers(table)
            previous_deferred=set((table.get('header_verification') or {}).get('deferred_header_ids', []))
            # A resumed CPU pass starts with headers not attempted previously.
            # Already uncertain crops must not starve later physical columns.
            ordered_headers.sort(key=lambda h: h['cell_id'] not in previous_deferred)
            deferred=[]
            for header_at,header in enumerate(ordered_headers):
                if (header.get('geometry_validation') or {}).get('requires_source_review'):
                    header['header_refinement_error'] = 'source_geometry_requires_review'
                    continue
                unit_retry = False
                if header.get('role') == 'header':
                    # Check the same table with this single proposed header;
                    # body cells remain present to validate any existing unit.
                    unit_retry = needs_unit_component_pass(dict(table, cells=[header, *[
                        c for c in table['cells'] if c.get('role') == 'cell']]))
                if resume_only and not unit_retry and header['cell_id'] not in previous_deferred and (
                        header.get('header_refinement') or header.get('header_observation_attempted')
                        or header.get('header_refinement_error')):
                    skipped += 1
                    continue
                existing=(table.get('column_units') or {}).get(str(header['column'])) or {}
                if existing.get('header_id')==header['cell_id'] and any(
                        c.get('role')=='cell' and verified_column_unit(table,c,existing) for c in table['cells']
                        if c['column']==header['column']):
                    skipped+=1
                    continue
                if header.get('colspan',1)>1 and verified_header_text(header):
                    skipped+=1
                    continue
                if table_attempted>=max_headers or time.monotonic()>=deadline:
                    deferred=[h['cell_id'] for h in ordered_headers[header_at:]]
                    break
                table_attempted+=1
                header['header_observation_attempted'] = True
                try:
                    box=pymupdf.Rect(header['bbox']) & render_page.rect
                    if box.is_empty or box.is_infinite or box.width*box.height*(600/72)**2>4_000_000:
                        continue
                    pix=render_page.get_pixmap(dpi=600,clip=box,alpha=False)
                    image=ocr_engine._rotate(ocr_engine._pil_from_pixmap(pix),(rotation-original_rotation)%360)
                    raw=ocr_engine._to_png(image)
                    digest=hashlib.sha256(raw).hexdigest()
                    proof:dict[str,Any]=dict(version=VERSION, semantic_version=SEMANTIC_VERSION,
                        column=header['column'], row=header['row'], colspan=header.get('colspan',1), header_id=header['cell_id'],
                        bbox=list(box), image_sha256=digest, dpi=600,
                        rotation=(rotation-original_rotation)%360, engine=ocr_engine.tesseract_version(), variants=[])
                    if store_image is not None:
                        proof['image_key']=store_image(raw).key
                    if cache_dir is not None:
                        directory=Path(cache_dir)/'header-images'
                        directory.mkdir(parents=True,exist_ok=True)
                        (directory/(digest+'.png')).write_bytes(raw)
                    clean=clean_glyphs(image)
                    if clean is None:
                        continue
                    for scale in (1,1.35):
                        if time.monotonic()>=deadline:
                            break
                        scaled=clean.resize((max(1,round(clean.width*scale)),max(1,round(clean.height*scale))),Image.Resampling.LANCZOS)
                        scaled=ImageOps.expand(scaled,border=20,fill='white')
                        words=ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(scaled),language='rus+eng',
                            psm=6,timeout=max(.1,min(5,deadline-time.monotonic()))))
                        proof['variants'].append(dict(scale=scale,words=words))
                    header['header_refinement']=proof
                    confirmed=header_consensus(proof['variants'])
                    if confirmed is None and header.get('colspan',1)>1:
                        from pdf_header_geometry import read_geometry
                        confirmed=read_geometry(clean,proof,deadline,cache_dir,store_image)
                    if confirmed is not None and independent_text_conflict(header,confirmed):
                        proof['independent_text_conflict']=True
                        confirmed=None
                    if (confirmed is not None and header.get('colspan',1)>1 and header['text'].strip()
                            and _clean(confirmed).replace('m','м')!=_clean(header['text']).replace('m','м')):
                        # A second OCR engine can agree with its own typo in a
                        # material name. Keep conflicting existing text intact.
                        proof['existing_text_conflict']=True
                        confirmed=None
                    if confirmed is not None:
                        header.setdefault('raw_recovered_text',header['text'])
                        header['text']=confirmed
                        header['quality']='usable'
                    if header.get('colspan',1)!=1:
                        continue  # A merged semantic header never donates a column unit.
                    votes=[header_unit(v['words']) for v in proof['variants']]
                    existing=(table.get('column_units') or {}).get(str(header['column'])) or {}
                    previous=next((h for h in table['cells'] if h['cell_id']==existing.get('header_id')),None)
                    if previous is not None and previous['row']>header['row']:
                        continue  # Higher material headers cannot replace the unit leaf.
                    if len(votes)==2 and votes[0] and votes[0]==votes[1]:
                        table.setdefault('column_units',{})[str(header['column'])]=dict(proof,unit=votes[0])
                    else:
                        unit=_refine_unit(clean,proof,deadline,cache_dir,store_image)
                        if unit and not any(v and v!=unit for v in votes):
                            table.setdefault('column_units',{})[str(header['column'])]=dict(proof,unit=unit)
                except (RuntimeError,ValueError,OSError) as exc:
                    header['header_refinement_error']=type(exc).__name__
            # Newly read leaf headers must become explicit ancestry of their
            # data column. No text from a neighbouring cell is copied.
            refresh_header_ancestry(table)
            previous=table.get('header_verification') or {}
            table['header_verification']=dict(version=VERSION,
                semantic_version=SEMANTIC_VERSION,
                headers_attempted=previous.get('headers_attempted',0)+table_attempted,
                last_headers_attempted=table_attempted,headers_skipped_verified=skipped,
                header_seconds_budget=seconds,budget_exhausted=bool(deferred) or time.monotonic()>=deadline,
                deferred_header_ids=deferred,
                header_limit_reached=bool(deferred) and table_attempted>=max_headers,
                budget_scope='per_table_header_count_shared_page_deadline')
    finally:
        private.close()
    verify_page_numbers(tables,independent_words)
    # Existing numeric refinement requires the independent RapidOCR value plus
    # two agreeing high-confidence Tesseract crop reads. No number is invented.
    numeric_budget=max(0,deadline-time.monotonic(),numeric_seconds)
    for table in tables:
        if table.get('header_verification'):
            table['header_verification']['numeric_seconds_budget']=numeric_budget
    refine_tables(page,tables,rotation=rotation,cache_dir=cache_dir,store_image=store_image,
        seconds=numeric_budget,max_numbers=max_numbers,max_columns=0)
    from pdf_row_sections import separate_row_sections
    for table in tables:
        separate_row_sections(table)


def complete_recovered_headers(page, tables, *, rotation=0, cache_dir=None, store_image=None,
                               passes=4, seconds=90, max_headers=32, checkpoint=None):
    """Resume pending physical headers during preparation, never during checking.

    Each pass is bounded; unresolved readings differ from unattempted headers.
    Reaching a limit is recorded as incomplete, not silently called ready.
    """
    from pdf_tables import strip_legacy_unit_notes
    cleaned = strip_legacy_unit_notes(tables)
    history = []
    for attempt in range(passes):
        pending = [t for t in tables if t.get('recovery') and
            (not t.get('header_verification') or
             (t.get('header_verification') or {}).get('deferred_header_ids') or needs_unit_component_pass(t))]
        if not pending:
            break
        before = {t['table_id']: list((t.get('header_verification') or {}).get('deferred_header_ids', []))
            for t in pending}
        verify_recovered_tables(page, pending, rotation=rotation, cache_dir=cache_dir,
            store_image=store_image, seconds=seconds, max_headers=max_headers,
            max_numbers=0, numeric_seconds=0, resume_only=True)
        after = {t['table_id']: list(t['header_verification']['deferred_header_ids']) for t in pending}
        history.append(dict(pass_number=attempt + 1, before=before, after=after))
        if checkpoint is not None:
            checkpoint(tables, history[-1])
        if before == after:
            break  # A time/geometry limit must not spin forever.
    remaining = {t['table_id']: list((t.get('header_verification') or {}).get('deferred_header_ids', []))
        for t in tables if t.get('recovery') and
        (not t.get('header_verification') or (t.get('header_verification') or {}).get('deferred_header_ids'))}
    return dict(state='header_observation_complete' if not remaining else 'incomplete_header_observation',
        remaining=remaining, passes=history, literal_headers_cleaned=cleaned,
        recognition_correctness_asserted=False)
