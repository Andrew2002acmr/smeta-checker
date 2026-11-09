"""Read-only OCR regression against source-anchored annotations, never VOR values."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


def normalized(value, kind):
    text = unicodedata.normalize('NFKC', str(value))
    if kind == 'decimal':
        text = text.replace(' ', '').replace('\u00a0', '').replace(',', '.')
        if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    text = re.sub(r'\s+', ' ', text).strip().casefold()
    # A raster stroke has no Unicode code point. A standalone dash is a
    # literal blank marker; a minus inside a number is still significant.
    return '–' if text in {'-', '‐', '‑', '–', '—', '−'} else text


def _overlap(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def evaluate(index, source_sha256, annotations):
    if annotations.get('source_sha256') != source_sha256:
        raise ValueError('OCR annotations belong to a different PDF')
    from pdf_cell_ocr import column_unit
    results = []
    for case in annotations['cases']:
        page, box = case['page'], case['bbox']
        if page < 1 or page > len(index.texts) or len(box) != 4 or case['check'] not in {'unit', 'literal'}:
            raise ValueError('Invalid source anchor')
        page_tables = index.tables[page - 1] if index.tables and page <= len(index.tables) else ()
        candidates = [(t, c) for t in page_tables for c in t['cells']
            if c.get('role') == case.get('role', 'cell') and len(c.get('bbox', [])) == 4
            and _overlap(box, c['bbox']) >= .9]
        result: dict[str, Any] = dict(id=case['id'], page=page, bbox=box, expected=case['expected'], passed=False)
        if len(candidates) != 1:
            result['reason'] = 'source_cell_missing' if not candidates else 'source_cell_ambiguous'
        else:
            table, cell = candidates[0]
            actual = column_unit(table, cell) if case['check'] == 'unit' else cell['text']
            kind = case.get('normalization', 'literal')
            result.update(actual=actual, table_id=table['table_id'], cell_id=cell['cell_id'])
            same = normalized(actual, kind) is not None and normalized(actual, kind) == normalized(case['expected'], kind)
            verified = bool(actual) if case['check'] == 'unit' else bool(cell.get('numeric_verified'))
            result.update(passed=same and (not case.get('require_verified') or verified), verified=verified)
            if not result['passed']:
                result['reason'] = 'reading_differs' if not same else 'reading_not_verified'
        results.append(result)
    return dict(total=len(results), passed=sum(r['passed'] for r in results),
        failed=sum(not r['passed'] for r in results), cases=results, model_requests=0,
        preparation_modified=False)
