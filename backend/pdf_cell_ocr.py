"""Bounded CPU refinement of physical cells; no VOR values or model requests.

Numeric evidence requires RapidOCR plus two high-confidence Tesseract reads.
Unit evidence belongs to a physical sub-column, never to its merged neighbour.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from functools import lru_cache
from statistics import median

VERSION = 'cell-ocr-v1'
NUMERIC_RECOVERY_VERSION = 'numeric-crop-recovery-v1'


def column_unit(table, cell):
    proof = (table.get('column_units') or {}).get(str(cell['column'])) or {}
    from pdf_table_verification import VERSION as HEADER_VERSION, verified_column_unit
    if proof.get('version') == HEADER_VERSION:
        return verified_column_unit(table,cell,proof)
    if table.get('recovery'):
        return ''  # Recovered raster headers require a physical-column proof.
    if (proof.get('version') == VERSION and proof.get('unit') in {'м2','м3'}
            and proof.get('header_id') in cell.get('header_ids', [])
            and len(proof.get('variants', [])) == 2
            and all(literal_unit(v['words']) == proof['unit'] for v in proof['variants'])):
        return proof['unit']
    units = set()
    for header in table['cells']:
        if header.get('cell_id') not in cell.get('header_ids', []) or header.get('colspan',1) != 1:
            continue
        if header.get('header_refinement'):
            continue  # Crop-derived units need their explicit two-read proof.
        text = unicodedata.normalize('NFKC',header['text']).casefold()
        units.update(re.findall(r'(?<!\w)(?:м[23]|m[23]|мм|см|км|м|шт|кг|т)(?!\w)', text))
    return next(iter(units)) if len(units) == 1 else ''


def clean_glyphs(image):
    """Remove only components connected to the crop edge (typically grid lines)."""
    import cv2
    import numpy as np
    from PIL import Image
    gray = np.array(image.convert('L'))
    if gray.size > 4_000_000 or min(gray.shape) < 8:
        return None
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink)
    cleaned = np.zeros_like(ink)
    for i in range(1, count):
        x,y,w,h,area = stats[i]
        if x > 1 and y > 1 and x+w < gray.shape[1]-1 and y+h < gray.shape[0]-1 and area >= 3:
            cleaned[labels == i] = 255
    points = cv2.findNonZero(cleaned)
    if points is None:
        return None
    x,y,w,h = cv2.boundingRect(points)
    return Image.fromarray(255-cleaned[y:y+h,x:x+w])


def literal_unit(words):
    """Accept a literal m²/m³ or a geometrically attached superscript."""
    tokens = [(unicodedata.normalize('NFKC', w['text']).casefold().strip(), w) for w in words]
    good = [(t,w) for t,w in tokens if w.get('conf', 0) >= 75]
    if len(good) == 1 and re.fullmatch(r'[мm][23]', good[0][0]):
        return 'м'+good[0][0][-1]
    if len(good) != 2:
        return None
    base = next((w for t,w in good if t in {'м','m'}), None)
    power = next(((t,w) for t,w in good if t in {'2','3'} and w.get('conf', 0) >= 90), None)
    if base is None or power is None:
        return None
    digit, exponent = power
    gap = exponent['left']-base['left']-base['width']
    adjacent = -.25*base['width'] <= gap <= max(base['width'],base['height'])*.8
    # A lowercase m has a short x-height. A clearly raised digit can be
    # almost as tall as m; requiring a 10% smaller box rejects real units.
    # Accept that case only when its bottom is at the top of the base glyph.
    raised = (exponent['height'] <= base['height']*1.05
        and exponent['top']+exponent['height'] <= base['top']+base['height']*.15)
    if (adjacent and exponent['left']+exponent['width']/2 > base['left']+base['width']/2
            and exponent['top']+exponent['height']/2 < base['top']+base['height']/2
            and (exponent['height'] <= base['height']*.9 or raised)):
        return 'м'+digit
    return None


def column_axis(table):
    shifts = []
    for row in sorted({c['row'] for c in table['cells'] if c.get('role') == 'cell'})[:8]:
        cells = sorted((c for c in table['cells'] if c['row'] == row and c.get('colspan',1) == 1), key=lambda c:c['column'])
        for a,b in zip(cells,cells[1:], strict=False):
            if b['column'] != a['column']+1:
                continue
            shifts.append([abs((a['bbox'][i]+a['bbox'][i+2]-b['bbox'][i]-b['bbox'][i+2])/2) for i in (0,1)])
    return int(bool(shifts) and median(s[1] for s in shifts) > median(s[0] for s in shifts))


def subcolumn_box(table, header, column, axis):
    cells = [c for c in table['cells'] if c.get('role') == 'cell' and c['column'] == column
        and c.get('colspan',1) == 1 and c['row'] > header['row']]
    if not cells:
        return None
    box = list(header['bbox'])
    box[axis] = max(box[axis], median(c['bbox'][axis] for c in cells))
    box[axis+2] = min(box[axis+2], median(c['bbox'][axis+2] for c in cells))
    return box if box[axis+2]-box[axis] > 2 else None


def _read_variants(image, *, numeric, read, deadline):
    from PIL import Image, ImageOps
    from ocr_engine import _to_png
    clean = clean_glyphs(image)
    if clean is None:
        return []
    results = []
    for height in ((32,48,64) if numeric else (48,64)):
        if time.monotonic() >= deadline:
            break
        resized = clean.resize((max(1, round(clean.width*height/clean.height)),height), Image.Resampling.LANCZOS)
        resized = ImageOps.expand(resized, border=20, fill='white')
        words = read(_to_png(resized), language='eng' if numeric else 'rus+eng', psm=7 if numeric else 6,
            timeout=max(.1, min(3, deadline-time.monotonic())))
        results.append(dict(height=height, words=words))
    return results


def numeric_agreement(value, variants):
    from normative_parameters import number
    votes = []
    for variant in variants:
        words = variant['words']
        actual = number(''.join(w['text'] for w in words))
        confidence = min((w.get('conf',0) for w in words), default=0)
        if actual is not None and confidence >= 75 and actual != number(value):
            return False
        if actual == number(value) and confidence >= 90:
            votes.append(actual)
    return len(votes) >= 2


def _numeric_disagreement(value, variants):
    from normative_parameters import number
    for variant in variants:
        words=variant.get('words',[])
        actual=number(''.join(w['text'] for w in words))
        if actual is not None and actual!=number(value) and min((w.get('conf',0) for w in words),default=0)>=75:
            return True
    return False


@lru_cache(maxsize=1)
def _numeric_rapid_engine():
    """Recognition only: reuse shipped Cyrillic weights, no table model."""
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
    return RapidOCR(params={'Rec.lang_type':LangRec.CYRILLIC,
        'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,
        'Global.use_det':False,'Global.use_cls':False,'Global.use_rec':True,
        'EngineConfig.onnxruntime.intra_op_num_threads':1,
        'EngineConfig.onnxruntime.inter_op_num_threads':1})


def _rapid_numeric(image):
    result=_numeric_rapid_engine()(image,use_det=False,use_cls=False,use_rec=True)
    return [dict(text=str(text),confidence=float(confidence))
        for text,confidence in zip(result.txts if result.txts is not None else [],
            result.scores if result.scores is not None else [], strict=False)]


def recovered_numeric_value(cell, proof):
    """Fresh independent reads may fill a physical cell, never a neighbour."""
    from normative_parameters import number
    if proof.get('version')!=NUMERIC_RECOVERY_VERSION or proof.get('bbox')!=cell.get('bbox'):
        return None
    rapid=proof.get('rapid_words') or []
    if len(rapid)!=1 or rapid[0].get('confidence',0)<.9:
        return None
    value=number(rapid[0]['text'])
    if value is None:
        return None
    original=number(cell.get('text',''))
    if original is not None and original!=value:
        return None  # A new OCR vote cannot silently rewrite an existing number.
    if any(w.get('confidence',0)>=.9 and number(w['text']) not in {None,value}
            for w in cell.get('words',[])):
        return None
    variants=proof.get('variants',[])
    if _numeric_disagreement(value,proof.get('prior_variants',[])):
        return None
    # Two distinct numbers must not become one by concatenating OCR tokens.
    if (not numeric_agreement(value,variants) or not numeric_agreement(value,
            [v for v in variants if len(v.get('words',[]))==1])):
        return None
    votes={(v.get('height'),v.get('psm',7)) for v in variants if len(v.get('words',[]))==1
        and number(v['words'][0]['text'])==value and v['words'][0].get('conf',0)>=90}
    if len(votes)<2:
        return None
    return str(value)


def _recover_numeric(image, cell, proof, read, deadline):
    from copy import deepcopy
    from PIL import Image, ImageOps
    from ocr_engine import _to_png
    recovery=dict(version=NUMERIC_RECOVERY_VERSION,bbox=list(cell['bbox']),
        image_sha256=proof['image_sha256'],dpi=proof['dpi'],rotation=proof['rotation'],
        prior_text=cell['text'],prior_words=deepcopy(cell.get('words',[])),
        prior_variants=deepcopy((cell.get('raw_numeric_refinement') or {}).get('variants',[])),
        rapid_engine='RapidOCR PP-OCRv5 Cyrillic mobile recognition',rapid_words=[],variants=deepcopy(proof['variants']))
    if proof.get('image_key'):
        recovery['image_key']=proof['image_key']
    cell['numeric_recovery']=recovery
    clean=clean_glyphs(image)
    if clean is None or time.monotonic()>=deadline:
        return
    recovery['rapid_words']=_rapid_numeric(clean)
    for height,psm in ((40,7),(56,7),(80,7),(96,7),(64,6),(64,13)):
        if time.monotonic()>=deadline or recovered_numeric_value(cell,recovery) is not None:
            break
        scaled=clean.resize((max(1,round(clean.width*height/clean.height)),height),Image.Resampling.LANCZOS)
        scaled=ImageOps.expand(scaled,border=20,fill='white')
        words=read(_to_png(scaled),language='eng',psm=psm,timeout=max(.1,min(3,deadline-time.monotonic())))
        recovery['variants'].append(dict(height=height,psm=psm,words=words))
    value=recovered_numeric_value(cell,recovery)
    if value is not None:
        cell['text']=value if not cell['text'].strip() else cell['text']
        cell['numeric_verified']=True
        cell['quality']='usable'


def refine_tables(page, tables, *, rotation=0, cache_dir=None, store_image=None, seconds:float=25, max_numbers=12, max_columns=32):
    """Refine uncertain totals and unit headers; preserve the source's raw OCR."""
    import pymupdf
    import ocr_engine
    from normative_parameters import number
    from pathlib import Path
    deadline = time.monotonic()+seconds
    original_rotation = page.rotation
    private = pymupdf.open()
    def render(box):
        box = pymupdf.Rect(box) & render_page.rect
        if box.is_empty or box.is_infinite or box.width*box.height*(600/72)**2 > 4_000_000:
            raise ValueError('Cell image exceeds pixel budget')
        pix = render_page.get_pixmap(dpi=600, clip=box, alpha=False)
        image = ocr_engine._rotate(ocr_engine._pil_from_pixmap(pix), (rotation-original_rotation)%360)
        data = ocr_engine._to_png(image)
        digest = hashlib.sha256(data).hexdigest()
        if cache_dir is not None:
            directory = Path(cache_dir)/'cell-images'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory/(digest+'.png')
            if not path.exists():
                path.write_bytes(data)
        proof = dict(version=VERSION, image_sha256=digest, bbox=list(box), dpi=600,
            rotation=(rotation-original_rotation)%360, engine=ocr_engine.tesseract_version())
        if store_image is not None:
            proof['image_key'] = store_image(data).key
        return image, proof
    def read(data, **options):
        return ocr_engine.parse_tsv(ocr_engine.run_tsv(data, **options))
    numbers = columns = 0
    try:
        # Restoring page.rotation still writes /Rotate 0 when the original key
        # was absent. All rendering adjustments belong to a private copy.
        private.insert_pdf(page.parent, from_page=page.number, to_page=page.number,annots=False,links=False)
        render_page = private[0]
        render_page.set_rotation(0)
        for table in tables:
            axis = column_axis(table)
            headers = {c['cell_id']:c for c in table['cells'] if c.get('role') == 'header'}
            totals = {c['row'] for c in table['cells'] if c['text'].strip().casefold().rstrip(':') in {'всего','итог','итого'}}
            candidates = sorted((c for c in table['cells'] if c['row'] in totals
                and (number(c['text']) is not None or (table.get('recovery') and not c['text'].strip()))
                and c.get('role')=='cell' and c.get('header_ids') and c.get('colspan',1)==1
                and c.get('rowspan',1)==1 and not c.get('numeric_verified')
                and (c.get('quality')=='usable' or table.get('recovery'))
                and 'unrecovered_source_region' not in c.get('reasons',[])),key=lambda c:(-c['row'],c['column']))
            for cell in candidates:
                if numbers >= max_numbers or time.monotonic() >= deadline:
                    break
                numbers += 1
                try:
                    from copy import deepcopy
                    image, proof = render(cell['bbox'])
                    proof['variants'] = _read_variants(image,numeric=True,read=read,deadline=deadline)
                    if cell.get('numeric_refinement'):
                        cell.setdefault('raw_numeric_refinement',deepcopy(cell['numeric_refinement']))
                    cell['numeric_refinement'] = proof
                    existing=cell.get('words') or []
                    cell['numeric_verified'] = bool(existing) and all(w.get('confidence',0)>=.9 for w in existing) \
                        and numeric_agreement(cell['text'],proof['variants']) \
                        and not _numeric_disagreement(cell['text'],(cell.get('raw_numeric_refinement') or {}).get('variants',[]))
                    if table.get('recovery') and not cell['numeric_verified']:
                        _recover_numeric(image,cell,proof,read,deadline)
                except (RuntimeError, ValueError) as exc:
                    cell['refinement_error'] = type(exc).__name__
            units = table.setdefault('column_units', {})
            for column in range(table['col_count']):
                if columns >= max_columns or time.monotonic() >= deadline:
                    break
                # Read the lowest narrow header using physical data-column
                # bounds, including when OCR merged the m² and m³ cells.
                choices = [h for h in headers.values() if h['column'] <= column < h['column']+h.get('colspan',1)
                    and h.get('colspan',1) <= 4]
                if not choices:
                    continue
                header = max(choices, key=lambda h:h['row'])
                box = subcolumn_box(table,header,column,axis)
                if box is None:
                    continue
                columns += 1
                try:
                    image, proof = render(box)
                    proof['variants'] = _read_variants(image,numeric=False,read=read,deadline=deadline)
                    votes = [literal_unit(v['words']) for v in proof['variants']]
                    if len(votes) == 2 and votes[0] and len(set(votes)) == 1:
                        units[str(column)] = dict(**proof,unit=votes[0],header_id=header['cell_id'])
                except (RuntimeError, ValueError):
                    continue
            table['cell_refinement'] = dict(version=VERSION,numbers_attempted=numbers,columns_attempted=columns,
                budget_exhausted=time.monotonic() >= deadline)
    finally:
        private.close()
