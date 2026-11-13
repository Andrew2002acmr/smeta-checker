"""Propose quantity dependencies without treating a repeated number as proof."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from codex_contract import _arithmetic


def _number(value):
    try:
        result = Decimal(str(value).replace(',', '.'))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _formula(position):
    raw = position.get('workbook_calculation') or {}
    if expression := raw.get('expression'):
        return str(expression).lstrip('=')
    formula = position.get('formula') or ''
    return str(formula).lstrip('=')


def _scope(position):
    return position['sheet'], tuple((h.get('address'), h['text']) for h in position.get('section_headings', []))


def _unit(position):
    unit = unicodedata.normalize('NFKC', position.get('unit') or '').lower()
    unit = re.sub(r'[.\s]', '', unit)
    return 'м' if unit == 'пм' else unit


def _material_stems(name):
    return {word[:5] for word in re.findall(r'[а-яё]{5,}', name)}


def _material_context(position, parent, has_formula, same_quantity):
    if position.get('position_type') != 'Материал':
        return None
    material, work = (item['work_name'].casefold() for item in (position, parent))
    units = _unit(parent), _unit(position)
    if (has_formula and units == ('м2', 'м3') and 'основан' in work
            and re.search(r'щебен\w*[- ]песчан|\bщпс\b', material)):
        return 'layer_material_volume'
    if (not has_formula and same_quantity and units[0] == units[1]
            and units[0] in {'м', 'м2', 'м3', 'шт'}):
        # Compare stems only for substantial words. Product names and units
        # alone cannot bind a row to a work item.
        if _material_stems(material) & _material_stems(work):
            return 'installed_material'
    return None


def _rounded(value, displayed):
    if displayed == value:
        return True
    if displayed.as_tuple().exponent >= 0:
        return False
    return abs(value - displayed) < Decimal(5).scaleb(displayed.as_tuple().exponent - 1)


def _volume_from_length(position, parent):
    if not (position.get('unit') or '').startswith(('м3', 'м³')) or parent.get('unit') != 'м':
        return None
    name = position['work_name'].casefold()
    match = re.search(r'бр\s*(\d+)\.\d+\.\d+', name)
    per_piece = re.search(r'v\s*ед\s*=\s*(\d+[.,]\d+)\s*м[3³]\s*/\s*шт', position.get('comment', ''), re.I)
    if 'камень' not in name or not match or not per_piece:
        return None
    length = _number(parent.get('vor_value'))
    piece_length = Decimal(match[1]) / 100
    if length is None or piece_length <= 0:
        return None
    expression = f'{length}/{piece_length}*{_number(per_piece[1])}'
    return {'expression': expression, 'expected_vor_value': str(_arithmetic(expression)),
            'assumption': 'Номинальная длина камня по марке. Проверить раскладку и подрезку.',
            'requires_project_length': True}


def quantity_links(positions):
    """Expose explicit references and bounded candidates from workbook context."""
    links = {}
    by_row = {(p['sheet'], p['row']): p for p in positions}
    for index, position in enumerate(positions):
        candidates = []
        for ref in position.get('excel_references', []):
            match = re.fullmatch(r'\$?[A-Z]+\$?(\d+)', str(ref.get('cell') or ''), re.I)
            if match and (parent := by_row.get((ref.get('sheet') or position['sheet'], int(match[1])))):
                candidates.append({'row': parent['row'], 'sheet': parent['sheet'],
                                   'relation': 'excel_reference', 'reference': ref,
                                   'engineering_status': 'unresolved'})
        expression = _formula(position).replace(',', '.')
        operands = [(_number(value), value) for value in
                    re.findall(r'(?<![\w$\.])\d+(?:\.\d+)?(?![\w.])', expression)]
        nearest_work = True
        for parent in reversed(positions[max(0, index-4):index]):
            if _scope(parent) != _scope(position) or not 0 < position['row'] - parent['row'] <= 5:
                break
            work = parent.get('position_type') == 'Работа'
            value = _number(parent.get('vor_value'))
            derived = _volume_from_length(position, parent) if work and nearest_work else None
            context = _material_context(position, parent, bool(expression),
                value is not None and value == _number(position.get('vor_value'))) if work and nearest_work else None
            if work:
                nearest_work = False
            if value is None or value <= 0:
                continue
            match = next((literal for number, literal in operands
                          if number is not None and _rounded(value, number)), None)
            if match is None and derived is None and context is None:
                continue
            item = {'row': parent['row'], 'sheet': parent['sheet'], 'work_name': parent['work_name'],
                    'unit': parent['unit'], 'vor_value': str(value), 'relation': 'candidate',
                    'literal': match, 'engineering_status': 'unresolved'}
            if derived:
                item['nominal_recalculation'] = derived
            if context:
                item['context'] = context
            candidates.append(item)
        if candidates:
            links[position['position_index']] = candidates
    return links
