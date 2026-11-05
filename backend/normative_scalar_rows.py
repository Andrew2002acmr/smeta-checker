"""Native scalar row quantities with separately labelled unit/value cells.

This is literal acquisition only. It never chooses a VOR quantity, approves
engineering ownership, or relaxes the independent OCR/paired-value contracts.
"""
from copy import deepcopy
from decimal import Decimal
import math
import re

from analysis_replay import digest
from audit_measurements import canonical_unit
from normative_source_identity import canonical_source_id

VERSION = 1
_UNIT_HEADER = r"единиц.{0,20}измер|\bunit\b"
_VALUE_HEADER = r"количеств|quantity"
_CELL_FIELDS = ('cell_id', 'role', 'row', 'column', 'colspan', 'rowspan', 'text', 'bbox',
                'origin', 'quality', 'reasons', 'geometry_validation', 'header_ids')


def _cell(raw, table_origin):
    value = {key: deepcopy(raw[key]) for key in _CELL_FIELDS if key in raw}
    for key, default in (('origin', table_origin), ('quality', 'usable'), ('rowspan', 1),
                         ('colspan', 1), ('reasons', []), ('geometry_validation', {}), ('header_ids', [])):
        value.setdefault(key, default)
    return value


def _box(box):
    return (isinstance(box, list) and len(box) == 4
        and all(type(v) in {int, float} and math.isfinite(v) for v in box)
        and 0 <= box[0] < box[2] and 0 <= box[1] < box[3])


def _native(cell, role):
    return (isinstance(cell, dict) and cell.get('role') == role and cell.get('origin') == 'native'
        and cell.get('quality') == 'usable' and not cell.get('reasons')
        and not (cell.get('geometry_validation') or {}).get('requires_source_review')
        and isinstance(cell.get('cell_id'), str) and bool(cell['cell_id'])
        and isinstance(cell.get('text'), str) and _box(cell.get('bbox'))
        and all(type(cell.get(k)) is int and cell[k] >= 0 for k in ('row', 'column'))
        and all(type(cell.get(k)) is int and cell[k] >= 1 for k in ('rowspan', 'colspan'))
        and isinstance(cell.get('header_ids'), list))


def _overlap_columns(a, b):
    return a['column'] < b['column'] + b['colspan'] and b['column'] < a['column'] + a['colspan']


def _header(cell, headers, pattern):
    candidates = [h for h in headers if h['row'] < cell['row']
        and h['column'] == cell['column'] and h['colspan'] == cell['colspan']
        and re.search(pattern, h['text'].replace('\n', ' '), re.I)]
    if len(candidates) != 1:
        return None
    header = candidates[0]
    if header['cell_id'] not in cell['header_ids'] or not _native(header, 'header'):
        return None
    if (abs(header['bbox'][0] - cell['bbox'][0]) > .02
            or abs(header['bbox'][2] - cell['bbox'][2]) > .02
            or header['bbox'][3] > cell['bbox'][1] + .02):
        return None
    if any(header['row'] < h['row'] < cell['row']
            and cell['column'] <= h['column'] < h['column'] + h['colspan'] <= cell['column'] + cell['colspan']
            and h['colspan'] < cell['colspan'] for h in headers):
        return None
    return header


def _measurement(context):
    if (type(context.get('schema')) is not int or context.get('schema') != VERSION or context.get('table_origin') != 'native'
            or context.get('scope_verified') is not False or not context.get('version_id')
            or type(context.get('page')) is not int or context['page'] < 1
            or not context.get('table_id') or type(context.get('row')) is not int):
        return None
    rows, headers = context.get('row_cells') or [], context.get('header_cells') or []
    all_cells = rows + headers
    if (not rows or not headers or any(not _native(c, 'cell') for c in rows)
            or any(not _native(h, 'header') for h in headers)
            or len({c['cell_id'] for c in all_cells}) != len(all_cells)
            or any(c['row'] != context['row'] for c in rows)):
        return None
    lookup = {c['cell_id']: c for c in rows}
    unit, value = (lookup.get(context.get(k)) for k in ('unit_cell_id', 'value_cell_id'))
    if (unit is None or value is None or unit['cell_id'] == value['cell_id']
            or unit['rowspan'] != 1 or value['rowspan'] != 1
            or unit['column'] + unit['colspan'] != value['column']
            or unit['bbox'][2] > value['bbox'][0] + .01
            or abs(unit['bbox'][1] - value['bbox'][1]) > .01
            or abs(unit['bbox'][3] - value['bbox'][3]) > .01):
        return None
    if any(c['cell_id'] not in {unit['cell_id'], value['cell_id']}
            and (_overlap_columns(c, unit) or _overlap_columns(c, value)) for c in rows):
        return None
    unit_header, value_header = _header(unit, headers, _UNIT_HEADER), _header(value, headers, _VALUE_HEADER)
    if not unit_header or not value_header or unit_header['cell_id'] == value_header['cell_id']:
        return None
    unit_candidates = [c for c in rows if _header(c, headers, _UNIT_HEADER) is not None]
    if len(unit_candidates) != 1:
        return None
    # A single closed scalar unit only: slash/rate/alternative units belong to
    # other independent contracts and are never reinterpreted as a scalar.
    spec = canonical_unit(unit['text'])
    if ('/' in unit['text'] or not spec or spec[1] not in {'length', 'area', 'volume', 'mass', 'count'}
            or re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', value['text'].strip()) is None):
        return None
    from normative_parameters import measure
    if measure('1', spec[0]) is None:
        return None
    return dict(value=str(Decimal(value['text'].strip().replace(',', '.'))), unit=spec[0],
        unit_cell=unit, value_cell=value, unit_header=unit_header, value_header=value_header)


def source_id(context):
    return f"agent:p{context['page']}:scalarrow:" + digest(context)[:24]


def _anchor_matches(anchor, cell, context):
    return (isinstance(anchor, dict) and anchor.get('state') == 'located'
        and anchor.get('version_id') == context['version_id'] and anchor.get('page') == context['page']
        and anchor.get('table_id') == context['table_id'] and anchor.get('cell_id') == cell['cell_id']
        and anchor.get('literal_text') == cell['text'] and anchor.get('bbox') == cell['bbox']
        and anchor.get('header_ids') == cell['header_ids'] and bool(anchor.get('rects'))
        and all(_box(rect) and all(0 <= x <= 1 for x in rect) for rect in anchor['rects']))


def verified_components(source):
    """Recheck the entire native physical envelope, including valid aliases."""
    if (source.get('kind') != 'project' or source.get('origin') != 'native_row_measurement'
            or source.get('numeric_verified') is not True or source.get('unit_verified') is not True):
        return []
    context = source.get('scalar_row_context') or {}
    try:
        found = _measurement(context)
        if (found is None or canonical_source_id(source) != source_id(context)
                or source.get('version_id') != context['version_id'] or source.get('page') != context['page']
                or source.get('unit') != found['unit']):
            return []
        text = ' | '.join(c['text'] for c in sorted(context['row_cells'], key=lambda c: c['column']))
        if source.get('text') != text or source.get('literal_text') != text:
            return []
        anchors, header_anchors = context.get('source_anchors') or [], context.get('header_anchors') or []
        if len(anchors) != 2 or len(header_anchors) != 2:
            return []
        if not all(_anchor_matches(a, found[k], context) for a, k in zip(anchors,
                ('unit_cell', 'value_cell'), strict=True)):
            return []
        if not all(_anchor_matches(a, found[k], context) for a, k in zip(header_anchors,
                ('unit_header', 'value_header'), strict=True)):
            return []
        if source.get('rects') != [rect for a in anchors for rect in a['rects']]:
            return []
        return [dict(value=found['value'], unit=found['unit'])]
    except (KeyError, TypeError, ValueError, AttributeError):
        return []


def proposal_matches(proposal, source):
    from normative_parameters import measure, number
    return bool(proposal.get('parameter') == 'quantity' and proposal.get('operation', 'direct') == 'direct' and not proposal.get('operand_source_ids')
        and any(number(proposal.get('value')) == number(c['value'])
            and measure('1', proposal.get('unit', '')) == measure('1', c['unit'])
            for c in verified_components(source)))


def _observations(index, version, page, table, selected, label):
    """Read scalar cells selected by ordinary table addresses, never VOR data."""
    if table.get('origin') != 'native' or not version or type(page) is not int or page < 1:
        return []
    cells = [_cell(c, table['origin']) for c in table.get('cells', [])]
    if len({c.get('cell_id') for c in cells}) != len(cells):
        return []
    selected_ids = {c.get('cell_id') for c in selected}
    headers = [c for c in cells if c.get('role') == 'header']
    results = []
    for value in cells:
        if value['cell_id'] not in selected_ids or not _native(value, 'cell'):
            continue
        if _header(value, headers, _VALUE_HEADER) is None:
            continue
        rows = [c for c in cells if c.get('role') == 'cell' and c.get('row') == value['row']]
        units = [c for c in rows if _header(c, headers, _UNIT_HEADER) is not None]
        if len(units) != 1:
            continue
        unit = units[0]
        relevant_headers = [h for h in headers if _overlap_columns(h, unit) or _overlap_columns(h, value)]
        context = dict(schema=VERSION, table_origin=table['origin'], version_id=version, page=page,
            table_id=table['table_id'], row=value['row'], unit_cell_id=unit['cell_id'], value_cell_id=value['cell_id'],
            row_cells=deepcopy(rows), header_cells=deepcopy(relevant_headers), scope_verified=False)
        found = _measurement(context)
        if found is None:
            continue
        from pdf_source_links import source_link
        context['source_anchors'] = [source_link(index, version, page, table, c) for c in (unit, value)]
        context['header_anchors'] = [source_link(index, version, page, table, found[k])
            for k in ('unit_header', 'value_header')]
        text = ' | '.join(c['text'] for c in sorted(rows, key=lambda c: c['column']))
        source = dict(id=source_id(context), kind='project', version_id=version, page=page, label=label,
            origin='native_row_measurement', text=text, literal_text=text, numeric_verified=True,
            unit_verified=True, unit=found['unit'], scalar_row_context=context,
            rects=[rect for a in context['source_anchors'] for rect in a.get('rects', [])])
        if not verified_components(source):
            continue
        preview = dict(row=value['row'], source_id=source['id'], value=found['value'], unit=found['unit'],
            recognition_verified=True, scope_verified=False,
            read_request=dict(tool='read_table', document_version_id=version, page=page, table_id=table['table_id'],
                row_ids=[value['row']], column_ids=[unit['column'], value['column']]))
        results.append(dict(source=source, preview=preview))
    return results


def observations(index, version, page, table, selected, label):
    """Malformed snapshots yield no fact rather than an invented replacement."""
    try:
        return _observations(index, version, page, table, selected, label)
    except (KeyError, TypeError, ValueError, AttributeError):
        return []
