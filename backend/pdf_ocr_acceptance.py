"""Compare source-anchored OCR literals and optional physical topology without repair."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


def normalized(value, kind):
    if kind == 'exact':
        return str(value)
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


def _recognition_matches(cell, expected):
    state = cell.get('recognition', {}).get('state')
    if expected == 'nonempty':
        return bool(cell.get('text')) and state in {'recognized', 'uncertain'}
    if expected not in {'literal_empty', 'literal_dash'}:
        raise ValueError('Invalid expected recognition state')
    marker = cell.get('literal_marker') or {}
    version, meaning, text = {
        'literal_empty': ('physical-empty-paper-v1', 'empty_paper_not_numeric_zero', ''),
        'literal_dash': ('physical-printed-dash-v1', 'printed_dash_not_numeric_zero', '–'),
    }[expected]
    return (state == expected and cell.get('text') == text
        and marker.get('version') == version and marker.get('meaning') == meaning)


def _structure_check(checks, check, passed, reason, **details):
    checks.append(dict(check=check, passed=bool(passed), reason='ok' if passed else reason, **details))


def _check_structure(index, annotations, matches):
    checks = []
    expected_table = annotations.get('expected_table')
    if expected_table is not None:
        page = expected_table['page']
        tables = index.tables[page - 1] if index.tables and 1 <= page <= len(index.tables) else ()
        _structure_check(checks, 'table_cardinality', len(tables) == 1,
            'source_table_missing' if not tables else 'source_table_ambiguous',
            page=page, expected=1, actual=len(tables))
        if len(tables) == 1:
            table = tables[0]
            _structure_check(checks, 'table_page', table.get('page') == page,
                'table_page_differs', expected=page, actual=table.get('page'))
            cells = table['cells']
            for field, coordinate, span in (
                ('row_count', 'row', 'rowspan'), ('col_count', 'column', 'colspan'),
            ):
                if field not in expected_table:
                    continue
                extent = max((cell.get(coordinate, -1) + cell.get(span, 0) for cell in cells), default=0)
                expected = expected_table[field]
                _structure_check(checks, field, table.get(field) == expected and extent == expected,
                    'table_size_differs', page=page, expected=expected, actual=table.get(field),
                    physical_extent=extent)
            if 'physical_cells' in expected_table:
                _structure_check(checks, 'physical_cells', len(cells) == expected_table['physical_cells'],
                    'physical_cell_count_differs', page=page,
                    expected=expected_table['physical_cells'], actual=len(cells))
            cell_ids = [cell.get('cell_id') for cell in cells]
            _structure_check(checks, 'physical_cell_ids',
                all(cell_ids) and len(set(cell_ids)) == len(cell_ids), 'physical_cell_id_missing_or_duplicate',
                page=page)
    cases_by_id = {}
    for case, candidates in zip(annotations['cases'], matches, strict=True):
        cases_by_id.setdefault(case['id'], []).append((case, candidates))
    if expected_table is not None or any(
        field in case for case in annotations['cases']
        for field in ('row', 'column', 'rowspan', 'colspan', 'header_case_ids')
    ):
        _structure_check(checks, 'annotation_case_ids',
            all(case_id and len(entries) == 1 for case_id, entries in cases_by_id.items()),
            'annotation_case_id_missing_or_duplicate')
    for case, candidates in zip(annotations['cases'], matches, strict=True):
        fields = [field for field in ('row', 'column', 'rowspan', 'colspan') if field in case]
        if not fields and 'header_case_ids' not in case:
            continue
        if len(candidates) != 1:
            _structure_check(checks, 'source_cell', False,
                'source_cell_missing' if not candidates else 'source_cell_ambiguous', case_id=case['id'])
            continue
        table, cell = candidates[0]
        for field in fields:
            _structure_check(checks, field, cell.get(field) == case[field], 'cell_geometry_differs',
                case_id=case['id'], expected=case[field], actual=cell.get(field))
        if 'header_case_ids' not in case:
            continue
        expected_ids = []
        reason = None
        header_cases = case['header_case_ids']
        if len(set(header_cases)) != len(header_cases):
            reason = 'header_case_duplicate'
        for header_id in header_cases:
            references = cases_by_id.get(header_id, [])
            if len(references) != 1:
                reason = reason or ('header_case_missing' if not references else 'header_case_ambiguous')
                continue
            header_case, header_matches = references[0]
            if len(header_matches) != 1:
                reason = reason or ('header_source_cell_missing' if not header_matches else 'header_source_cell_ambiguous')
                continue
            header_table, header_cell = header_matches[0]
            if header_case['page'] != case['page'] or header_table is not table:
                reason = reason or 'header_from_other_table'
            expected_ids.append(header_cell['cell_id'])
        actual_ids = cell.get('header_ids')
        table_ids = [candidate.get('cell_id') for candidate in table['cells']]
        if len(set(expected_ids)) != len(expected_ids):
            reason = reason or 'header_source_cell_duplicate'
        if actual_ids is None:
            reason = reason or 'header_ancestry_missing'
        elif len(set(actual_ids)) != len(actual_ids):
            reason = reason or 'header_ancestry_duplicate'
        elif any(table_ids.count(header_id) != 1 for header_id in actual_ids):
            reason = reason or 'header_ancestor_missing_or_duplicate'
        elif actual_ids != expected_ids:
            reason = reason or 'header_ancestry_differs'
        _structure_check(checks, 'header_case_ids', reason is None, reason,
            case_id=case['id'], expected=expected_ids, actual=actual_ids)
    return checks


def evaluate(index, source_sha256, annotations):
    if annotations.get('source_sha256') != source_sha256:
        raise ValueError('OCR annotations belong to a different PDF')
    from pdf_cell_ocr import column_unit
    results = []
    matches = []
    for case in annotations['cases']:
        page, box = case['page'], case['bbox']
        if page < 1 or page > len(index.texts) or len(box) != 4 or case['check'] not in {'unit', 'literal'}:
            raise ValueError('Invalid source anchor')
        page_tables = index.tables[page - 1] if index.tables and page <= len(index.tables) else ()
        candidates = [(t, c) for t in page_tables for c in t['cells']
            if c.get('role') == case.get('role', 'cell') and len(c.get('bbox', [])) == 4
            and _overlap(box, c['bbox']) >= .9]
        matches.append(candidates)
        result: dict[str, Any] = dict(id=case['id'], page=page, bbox=box, expected=case['expected'], passed=False)
        if len(candidates) != 1:
            result['reason'] = 'source_cell_missing' if not candidates else 'source_cell_ambiguous'
        else:
            table, cell = candidates[0]
            actual = column_unit(table, cell) if case['check'] == 'unit' else cell['text']
            kind = case.get('normalization', 'literal')
            result.update(actual=actual, table_id=table['table_id'], cell_id=cell['cell_id'])
            same = normalized(actual, kind) is not None and normalized(actual, kind) == normalized(case['expected'], kind)
            expected_state = case.get('expected_recognition_state')
            if expected_state is None and kind == 'exact' and case['expected'] == '':
                expected_state = 'literal_empty'
            state_matches = expected_state is None or _recognition_matches(cell, expected_state)
            verified = bool(actual) if case['check'] == 'unit' else bool(cell.get('numeric_verified'))
            result.update(passed=same and state_matches and (not case.get('require_verified') or verified),
                verified=verified)
            if expected_state is not None:
                result.update(expected_recognition_state=expected_state,
                    actual_recognition_state=cell.get('recognition', {}).get('state'))
            if not result['passed']:
                result['reason'] = ('reading_differs' if not same else
                    'recognition_state_differs' if not state_matches else 'reading_not_verified')
        results.append(result)
    return dict(total=len(results), passed=sum(r['passed'] for r in results),
        failed=sum(not r['passed'] for r in results), cases=results, model_requests=0,
        preparation_modified=False, structure_checks=_check_structure(index, annotations, matches))
