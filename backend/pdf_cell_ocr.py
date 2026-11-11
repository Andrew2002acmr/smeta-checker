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
from typing import Any

from pdf_geometry_contract import usable_box

VERSION = 'cell-ocr-v1'
NUMERIC_RECOVERY_VERSION = 'numeric-crop-recovery-v1'
BODY_PREPARATION_VERSION = 'physical-body-numbers-v6'
STRONG_CONSENSUS = 'cross-engine-strong-numeric-v1'


def column_unit(table, cell):
    if (cell.get('geometry_validation') or {}).get('requires_source_review'):
        return ''
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
        if table.get('origin') == 'ocr':
            header = next((h for h in table['cells'] if h['cell_id'] == proof['header_id']), None)
            expected = subcolumn_box(table, header, cell['column'], column_axis(table)) if header else None
            if expected is None or proof.get('bbox') != expected:
                return ''
        return proof['unit']
    if table.get('origin') == 'ocr':
        return ''  # Raw raster text never inherits native-PDF unit trust.
    units = set()
    for header in table['cells']:
        if header.get('cell_id') not in cell.get('header_ids', []) or header.get('colspan',1) != 1:
            continue
        if header.get('header_refinement'):
            continue  # Crop-derived units need their explicit two-read proof.
        text = unicodedata.normalize('NFKC',header['text']).casefold()
        units.update(re.findall(r'(?<!\w)(?:м[23]|m[23]|мм|см|км|м|шт|кг|т)(?!\w)', text))
    return next(iter(units)) if len(units) == 1 else ''


def clean_glyphs(image, *, preserve_gray=False, return_bbox=False):
    """Remove only components connected to the crop edge (typically grid lines)."""
    import cv2
    import numpy as np
    from PIL import Image
    gray = np.array(image.convert('L'))
    if gray.size > 4_000_000 or min(gray.shape) < 8:
        return None
    if preserve_gray:
        # Preserve faint punctuation before cropping, not just its grayscale.
        ink = cv2.threshold(gray, 249, 255, cv2.THRESH_BINARY_INV)[1]
    else:
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
    if preserve_gray:
        # Keep the original antialiasing of interior glyphs. The same mask
        # removes border rules; no character or expected value is supplied.
        keep=cv2.dilate(cleaned,np.ones((3,3),dtype=np.uint8))>0
        result = Image.fromarray(np.where(keep,gray,255).astype(np.uint8)[y:y+h,x:x+w])
    else:
        result = Image.fromarray(255-cleaned[y:y+h,x:x+w])
    return (result, [x, y, x+w, y+h]) if return_bbox else result


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
        cells = sorted((c for c in table['cells'] if c['row'] == row and c.get('colspan',1) == 1
            and usable_box(c)), key=lambda c:c['column'])
        for a,b in zip(cells,cells[1:], strict=False):
            if b['column'] != a['column']+1:
                continue
            shifts.append([abs((a['bbox'][i]+a['bbox'][i+2]-b['bbox'][i]-b['bbox'][i+2])/2) for i in (0,1)])
    return int(bool(shifts) and median(s[1] for s in shifts) > median(s[0] for s in shifts))


def subcolumn_box(table, header, column, axis):
    if not usable_box(header):
        return None
    cells = [c for c in table['cells'] if c.get('role') == 'cell' and c['column'] == column
        and c.get('colspan',1) == 1 and c['row'] > header['row']and usable_box(c)]
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
    if number(value) is None:
        return False
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


def _numeric_disagreement(value, variants, *, confidence=75):
    from normative_parameters import number
    for variant in variants:
        words=variant.get('words',[])
        actual=number(''.join(w['text'] for w in words))
        if actual is not None and actual!=number(value) and min((w.get('conf',0) for w in words),default=0)>=confidence:
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
    result: Any = _numeric_rapid_engine()(image,use_det=False,use_cls=False,use_rec=True)
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
    if any(w.get('confidence', 0) >= .9 and number(w['text']) not in {None, value}
            for attempt in proof.get('rapid_attempts', []) for w in attempt.get('words', [])):
        return None
    original_text = cell.get('text', '').strip()
    original=number(original_text)
    if original_text and (original is None or original!=value):
        return None  # Re-observation never silently rewrites or verifies a different literal.
    if any(w.get('confidence',0)>=.9 and number(w['text']) not in {None,value}
            for w in cell.get('words',[])):
        return None
    variants=proof.get('variants',[])
    if proof.get('consensus_policy') == STRONG_CONSENSUS:
        # Strong independent-engine consensus may resolve only moderate scale
        # disagreements. All observations stay in the proof; a >=90 conflict
        # still blocks, including conflicts from earlier crop attempts.
        if rapid[0].get('confidence', 0) < .99 or _numeric_disagreement(value,
                [*proof.get('prior_variants', []), *variants], confidence=90):
            return None
        strong = {(v.get('height'), v.get('psm', 7)) for v in variants
            if len(v.get('words', [])) == 1 and number(v['words'][0]['text']) == value
            and v['words'][0].get('conf', 0) >= 95}
        return str(value) if len(strong) >= 2 else None
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
    cell['numeric_verified'] = False
    recovery: dict[str, Any] = dict(version=NUMERIC_RECOVERY_VERSION,bbox=list(cell['bbox']),
        image_sha256=proof['image_sha256'],dpi=proof['dpi'],rotation=proof['rotation'],
        prior_text=cell['text'],prior_words=deepcopy(cell.get('words',[])),
        prior_variants=deepcopy([*(cell.get('raw_numeric_refinement') or {}).get('variants', []),
            *proof.get('prior_variants', [])]),
        rapid_engine='RapidOCR PP-OCRv5 Cyrillic mobile recognition',rapid_words=[],variants=deepcopy(proof['variants']))
    if proof.get('image_key'):
        recovery['image_key']=proof['image_key']
    cell['numeric_recovery']=recovery
    clean=clean_glyphs(image)
    if clean is None or time.monotonic()>=deadline:
        return
    recovery['rapid_words']=_rapid_numeric(clean)
    from pdf_numeric_variants import padded_rapid, italic_reads
    recovery['rapid_words'], recovery['rapid_attempts'] = padded_rapid(
        clean, recovery['rapid_words'], _rapid_numeric, deadline)
    def accepted():
        value = recovered_numeric_value(cell, recovery)
        if value is not None:
            return value
        recovery['consensus_policy'] = STRONG_CONSENSUS
        value = recovered_numeric_value(cell, recovery)
        if value is None:
            recovery.pop('consensus_policy', None)
        return value
    for height,psm in ((40,7),(56,7),(80,7),(96,7),(64,6),(64,13)):
        if time.monotonic()>=deadline or accepted() is not None:
            break
        scaled=clean.resize((max(1,round(clean.width*height/clean.height)),height),Image.Resampling.LANCZOS)
        scaled=ImageOps.expand(scaled,border=20,fill='white')
        words=read(_to_png(scaled),language='eng',psm=psm,timeout=max(.1,min(3,deadline-time.monotonic())))
        recovery['variants'].append(dict(height=height,psm=psm,words=words))
    value=accepted()
    if value is None:
        for variant in italic_reads(clean, read, deadline):
            recovery['variants'].append(variant)
            value = accepted()
            if value is not None:
                break
    if value is not None:
        cell['text']=value if not cell['text'].strip() else cell['text']
        cell['numeric_verified']=True
        cell['quality']='usable'


def _measurement_column(table, cell):
    headers = ' '.join(h.get('text', '') for h in table['cells']
        if h.get('role') == 'header' and h.get('cell_id') in cell.get('header_ids', []))
    if re.search(r'пикет|наименован|обозначен|позиц|номер|шифр|марка|(?:^|\s)№', headers, re.I):
        return False
    return bool(column_unit(table, cell) or re.search(
        r'количеств|объ[её]м|площад|расстояни|длин|ширин|высот|толщин|масс', headers, re.I))


def has_measurement_context(table):
    # Drawing title blocks are physical grids too. A grid alone must not
    # trigger attempts to turn all its blank boxes into quantities.
    return any(_measurement_column(table, c) for c in table['cells'] if c.get('role') == 'cell')


def numeric_candidates(table, *, include_body=False, resume_only=False):
    from normative_parameters import number
    totals = {c['row'] for c in table['cells'] if c['text'].strip().casefold().rstrip(':') in {'всего', 'итог', 'итого'}}
    body = include_body and has_measurement_context(table)
    output = []
    for cell in table['cells']:
        origin = cell.get('origin', table.get('origin'))
        if origin == 'native' or (not table.get('recovery') and origin != 'ocr'):
            continue  # Native literals stay authoritative; only explicit raster cells are reobserved.
        if (cell.get('role') != 'cell' or not cell.get('header_ids') or cell.get('colspan', 1) != 1
                or cell.get('rowspan', 1) != 1 or cell.get('numeric_verified')
                or 'unrecovered_source_region' in cell.get('reasons', [])):
            continue
        if (cell.get('geometry_validation') or {}).get('requires_source_review'):
            continue
        measurement = body and _measurement_column(table, cell)
        text = cell['text'].strip()
        # This alphabet selects crops only; it does not substitute glyphs,
        # insert decimal separators, or concatenate observations into a value.
        suspect = measurement and bool(re.fullmatch(r'[+\-−]?[0-9оОoO]+(?:[.,\s][0-9оОoO]+)*', text))
        if not (number(text) is not None or suspect or
                (not text and table.get('recovery') and (measurement or cell['row'] in totals))):
            continue
        if cell['row'] not in totals and not measurement:
            continue
        if not (cell.get('quality') == 'usable' or table.get('recovery') or measurement):
            continue
        if resume_only and (cell.get('numeric_preparation') or {}).get('version') == BODY_PREPARATION_VERSION:
            continue
        output.append(cell)
    def suspicious(cell):
        # Actual source glyph, not inferred correction: single Cyrillic o glyph,
        # potentially truncated hundred/thousand values or ambiguous zero.
        source = cell.get('text', '').strip()
        return bool(re.fullmatch(r'[оО]', source)) or bool(re.fullmatch(r'\d*(?:\d[оО]|[оО]\d)\d*', source))
    return sorted(output, key=lambda item: (
        item['row'] not in totals,
        # Suspicious source glyphs and contradictory independent observations
        # consume bounded OCR first; blanks remain last and never preempt them.
        not (bool(item['text'].strip()) and (
            suspicious(item)
            or (item.get('numeric_preparation') or {}).get('state') == 'uncertain'
            or item.get('numeric_verification_conflicts'))),
        bool(item['text'].strip()),
        item['row'], item['column']))


def refine_tables(page, tables, *, rotation=0, cache_dir=None, store_image=None, seconds:float=25,
                  max_numbers=12, max_columns=32, include_body=False, resume_only=False):
    """Refine uncertain totals and unit headers; preserve the source's raw OCR."""
    import pymupdf
    import ocr_engine
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
    try:
        # Restoring page.rotation still writes /Rotate 0 when the original key
        # was absent. All rendering adjustments belong to a private copy.
        private.insert_pdf(page.parent, from_page=page.number, to_page=page.number,annots=False,links=False)
        render_page = private[0]
        render_page.set_rotation(0)
        for table in tables:
            numbers = columns = 0
            deferred = []
            axis = column_axis(table)
            headers = {c['cell_id']:c for c in table['cells'] if c.get('role') == 'header'}
            candidates = numeric_candidates(table, include_body=include_body, resume_only=resume_only)
            for at, cell in enumerate(candidates):
                if numbers >= max_numbers or time.monotonic() >= deadline:
                    deferred = [c['cell_id'] for c in candidates[at:]]
                    break
                numbers += 1
                try:
                    from copy import deepcopy
                    cell['numeric_verified'] = False
                    image, proof = render(cell['bbox'])
                    from pdf_empty_cells import printed_dash, empty_paper
                    blank = empty_paper(image) if not cell['text'].strip() else None
                    if blank:
                        cell['literal_marker'] = dict(blank, bbox=list(cell['bbox']),
                            image_sha256=proof['image_sha256'], image_key=proof.get('image_key'),
                            dpi=proof['dpi'], rotation=proof['rotation'])
                        cell['numeric_verified'] = False
                        cell['numeric_preparation'] = dict(version=BODY_PREPARATION_VERSION, state='literal_empty')
                        continue
                    marker = printed_dash(image) if not cell['text'].strip() else None
                    if marker:
                        cell['literal_marker'] = dict(marker, bbox=list(cell['bbox']),
                            image_sha256=proof['image_sha256'], image_key=proof.get('image_key'),
                            dpi=proof['dpi'], rotation=proof['rotation'])
                        cell['text'] = '–'
                        cell['numeric_verified'] = False
                        cell['numeric_preparation'] = dict(version=BODY_PREPARATION_VERSION, state='literal_dash')
                        continue
                    proof['variants'] = _read_variants(image,numeric=True,read=read,deadline=deadline)
                    proof['prior_text'] = cell['text']
                    proof['prior_words'] = deepcopy(cell.get('words', []))
                    if cell.get('numeric_refinement'):
                        cell.setdefault('raw_numeric_refinement',deepcopy(cell['numeric_refinement']))
                        previous = cell['numeric_refinement']
                        proof['prior_variants'] = deepcopy([
                            *previous.get('prior_variants', []), *previous.get('variants', [])])
                    cell['numeric_refinement'] = proof
                    existing=cell.get('words') or []
                    cell['numeric_verified'] = bool(existing) and all(w.get('confidence',0)>=.9 for w in existing) \
                        and numeric_agreement(cell['text'],proof['variants']) \
                        and not _numeric_disagreement(cell['text'], [
                            *(cell.get('raw_numeric_refinement') or {}).get('variants', []),
                            *proof.get('prior_variants', [])])
                    if (table.get('recovery') or include_body) and not cell['numeric_verified']:
                        _recover_numeric(image,cell,proof,read,deadline)
                except (RuntimeError, ValueError) as exc:
                    cell['refinement_error'] = type(exc).__name__
                if include_body:
                    cell['numeric_preparation'] = dict(version=BODY_PREPARATION_VERSION,
                        state='verified' if cell.get('numeric_verified') else 'uncertain')
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
                deferred_cell_ids=deferred, budget_scope='per_table_shared_page_deadline',
                budget_exhausted=bool(deferred) or time.monotonic() >= deadline)
    finally:
        private.close()
