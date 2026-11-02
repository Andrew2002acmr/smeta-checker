"""Evidence-backed numeric cards. Decimal arithmetic; no model-supplied verdicts."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata

from normative_local_checks import layer_group
from vor_formula import calculate_cell


def number(value):
    try:
        text = str(value).replace(',', '.').replace(' ', '')
        if len(text) > 48 or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            return None
        result = Decimal(text)
        return result if result.is_finite() and abs(result) <= Decimal('1e18') else None
    except InvalidOperation:
        return None


def clean(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text)).casefold()).strip()


UNITS = {'м': ('length', '1'), 'см': ('length', '.01'), 'мм': ('length', '.001'), 'км': ('length', '1000'),
    'м2': ('area', '1'), 'м3': ('volume', '1'), 'шт': ('count', '1'), 'т': ('mass', '1000'),
    'кг': ('mass', '1'), '': ('factor', '1'), '1': ('factor', '1')}
UNITS.update({'m': UNITS['м'], 'mm': UNITS['мм'], 'cm': UNITS['см'], 'km': UNITS['км'],
    'm2': UNITS['м2'], 'm3': UNITS['м3'], 'kg': UNITS['кг'], 't': UNITS['т']})


def measure(value, unit):
    unit = clean(unit).replace(' ', '').rstrip('.')
    spec = UNITS.get(unit)
    amount = number(value)
    return (spec[0], amount * Decimal(spec[1])) if spec and amount is not None else None


def unit_quoted(unit, quote):
    return bool(re.search(r'(?<![а-яa-z0-9])'+re.escape(clean(unit).rstrip('.'))+r'(?![а-яa-z0-9])', clean(quote)))


def quoted(value, quote, text):
    """A fabricated quote, number embedded in another number, or NaN is no proof."""
    amount = number(value)
    return bool(amount is not None and len(clean(quote)) >= 5 and clean(quote) in clean(text)
        and amount in {number(v) for v in re.findall(r'(?<![\w.,])[-+]?\d+(?:[.,]\d+)?(?!\w|[.,]\d)', clean(quote))})


def conditions(proposal, source, verified):
    output, states = [], []
    for rule in proposal.get('conditions') or []:
        actual = verified.get(rule['parameter'])
        expected = measure(rule['value'], rule['unit'])
        text = clean(rule['quote'])
        detected = next((op for op, pattern in [('le', r'\b(?:не более|не выше|до\s)'), ('ge', r'\b(?:не менее|не ниже|от\s)'),
            ('lt', r'\b(?:менее|меньше)'), ('gt', r'\b(?:более|свыше|превыш)'), ('eq', r'\b(?:равн|составля)|=')] if re.search(pattern, text)), None)
        valid = (actual and expected and actual[0] == expected[0]
            and quoted(rule['value'], rule['quote'], source['text']) and unit_quoted(rule['unit'], rule['quote'])
            and detected == rule['operator'])
        state = None
        if valid and actual is not None and expected is not None:
            a, b = actual[1], expected[1]
            state = {'eq': a == b, 'lt': a < b, 'le': a <= b, 'gt': a > b, 'ge': a >= b}[rule['operator']]
        label = {'quantity': 'Количество', 'thickness': 'Толщина'}[rule['parameter']]
        operator = {'eq': '=', 'lt': '<', 'le': '≤', 'gt': '>', 'ge': '≥'}[rule['operator']]
        output.append(f"{label} {operator} {rule['value']} {rule['unit']}: "
            + ('выполнено' if state is True else 'не выполнено' if state is False else 'недостаточно подтверждённых данных'))
        states.append(state)
    return output, states


def build_cards(position, result, positions, relation):
    sources = {s['id']: s for s in result['sources']}
    proposals = result.get('parameter_proposals') or []
    cards, verified = [], {}
    owner = positions.get((relation.get('parent') or {}).get('index'), position)
    row = position.position_source.row if position.position_source else None
    group = layer_group(owner, result.get('related_positions') or [])
    height = re.search(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', owner.work_name, re.I)

    def card(key, label, value, unit, role='project_value', **extra):
        return dict(id=key, label=label, role=role, row=row, declared_value=str(value), unit=unit,
            state='needs_facts', reason='Нет структурированного подтверждения из источника.', **extra)

    def compare(c, kind, expected_value=None):
        expected = measure(expected_value or c['declared_value'], c['unit'])
        matches = [p for p in proposals if p['parameter'] == kind and p['source_id'] in sources]
        if c['role'] == 'coefficient':
            exact = [p for p in matches if number(p['value']) == number(c['declared_value'])]
            matches = exact or matches
        distinct = {(p['source_id'], p['value'], p['unit']) for p in matches}
        if len(distinct) != 1:
            c['reason'] = 'Нужен однозначный источник параметра.' if matches else c['reason']
            if not matches and kind == 'quantity':
                previous = next((v for v in result.get('items') or []
                    if v.get('topic') == 'Локальная проверка: площадь по структуре OCR-таблицы'), None)
                if previous:
                    c['reason'] = previous['explanation']
                    c['source_ids'] = [v for v in previous.get('project_source_ids') or [] if v in sources]
            return c
        p = matches[0]
        source = sources[p['source_id']]
        wanted_kind = 'project' if kind in {'quantity', 'thickness'} else 'normative'
        c.update(found_value=p['value'], found_unit=p['unit'], source_ids=[p['source_id']], quote=p['quote'],
            source_anchors=[dict(source_id=p['source_id'], quote=p['quote'], rects=[])])
        found = measure(p['value'], p['unit'])
        unit_present = kind == 'coefficient' or unit_quoted(p['unit'], p['quote'])
        if source['kind'] != wanted_kind or not quoted(p['value'], p['quote'], source['text']) or not expected or not found or not unit_present:
            c['reason'] = 'Число, цитата, тип источника или единица не подтверждены программно.'
            return c
        if source['origin'].startswith('ocr'):
            c['reason'] = 'Распознано OCR: требуется подтверждение по изображению.'
            return c
        if re.search(r'\b(?:не более|не менее|до\s*\d|от\s*\d|в пределах|диапазон)', clean(p['quote'])):
            c['reason'] = 'В цитате указано ограничение или диапазон, а не однозначное фактическое значение.'
            return c
        if expected[0] != found[0]:
            c.update(state='possible_mismatch', reason='Размерности единиц не совпадают.')
            return c
        equal = expected[1] == found[1]
        precision = number(c['declared_value'])
        precision_exp = int(precision.as_tuple().exponent) if precision is not None else 0
        base_unit = measure('1', c['unit'])
        if kind == 'quantity' and expected[0] != 'count' and precision is not None and precision_exp < 0 and base_unit:
            equal = (found[1]/base_unit[1]).quantize(Decimal(1).scaleb(precision_exp), rounding=ROUND_HALF_UP) == precision
        if not equal:
            c.update(state='possible_mismatch', reason='Значение в источнике отличается от значения ВОР после приведения единиц.')
            return c
        if kind == 'coefficient':
            c['conditions'], states = conditions(p, source, verified)
            if not re.search('коэфф|уплотн', clean(p['quote'])) or not states or any(v is None for v in states):
                c['reason'] = 'Число найдено, но назначение или условия применения коэффициента ещё не подтверждены.'
                return c
            if not all(states):
                c.update(state='possible_mismatch', reason='Условие применения коэффициента не выполнено.')
                return c
        c.update(state='confirmed', reason='Число и цитата проверены, единицы приведены; значения совпадают.')
        if kind in {'quantity', 'thickness'}:
            verified[kind] = found
        return c

    quantity_card = card('quantity', 'Количество родительской работы по ПД' if owner.index != position.index else 'Количество по ПД', owner.volume, owner.unit)
    quantity_card['row'] = owner.position_source.row if owner.position_source else None
    cards.append(compare(quantity_card, 'quantity'))
    if height:
        h_value, h_unit = height[1], height[2].lower()
        total = str(group['height']) if group else h_value
        if group:
            h_unit = 'см'
        c = card('thickness', 'Толщина проектного слоя', h_value, h_unit, compared_value=total)
        c['row'] = owner.position_source.row if owner.position_source else None
        if 'двухслойн' in owner.work_name.casefold() and not group:
            c['reason'] = 'Для двухслойного основания не удалось однозначно установить связанную толщину второго слоя.'
            cards.append(c)
        else:
            cards.append(compare(c, 'thickness', total))

    calculated = [position] if position.formula or position.calculation else []
    if relation.get('children_truncated'):
        cards.append(card('children:limit', 'Остальные связанные расчёты', 'более 12', '', role='calculation'))
    calculated += [positions[v['material_index']] for v in relation.get('children') or []
        if v.get('material_index') in positions]
    for work in calculated[:12]:
        work_number = work.number or str(work.index + 1)
        calc = work.calculation or {}
        expression = calc.get('expression') or work.formula
        if not expression:
            continue
        cells = {d['cell']: dict(plain=number(d.get('value')), cached=None)
            for d in calc.get('dependencies') or [] if d.get('cell')}
        address = calc.get('cell') or 'XFD1048576'
        cells[address] = dict(formula='='+expression.lstrip('='), cached=None)
        actual = calculate_cell(address, cells)
        c = card(f'calculation:{work.index}', f'Арифметика позиции №{work_number}', work.volume, work.unit,
            role='calculation', expression=expression)
        c['row'] = work.position_source.row if work.position_source else None
        c['found_value'] = actual.get('value')
        if actual['status'] == 'calculated' and number(actual.get('value')) == number(work.volume) and not calc.get('cached_mismatch'):
            c.update(state='confirmed', reason='Формула пересчитана программно по сохранённым значениям ячеек.')
        elif actual['status'] == 'calculated' and number(actual.get('value')) != number(work.volume):
            c.update(state='possible_mismatch', reason='Результат формулы не совпадает с объёмом ВОР.')
        elif calc.get('cached_mismatch'):
            c['reason'] = 'Сохранённый результат Excel отличается от формулы: требуется сверка исходной ячейки.'
        else:
            c['reason'] = 'Формулу нельзя надёжно пересчитать по сохранённым данным.'
        cards.append(c)
        excluded = {number(owner.volume), Decimal(0)}
        if height:
            depth = measure(height[1], height[2].lower())
            if depth:
                excluded.add(depth[1])
        factors = list(dict.fromkeys(str(d['value']) for d in actual.get('dependencies') or []
            if d.get('origin') == 'formula_literal' and number(d.get('value')) not in excluded))
        if len(factors) > 8:
            cards.append(card(f'factors:limit:{work.index}', 'Дополнительные параметры формулы', str(len(factors)), '', role='coefficient'))
        for value in factors[:8]:
            c = card(f'factor:{work.index}:{value}', f'Параметр формулы {value} · позиция №{work_number}', value, '', role='coefficient')
            c['row'] = work.position_source.row if work.position_source else None
            cards.append(compare(c, 'coefficient'))
    for p in proposals:
        if p['parameter'] != 'unit_scale' or p['source_id'] not in sources:
            continue
        s, scale, declared = sources[p['source_id']], measure(p['value'], p['unit']), measure(owner.volume, owner.unit)
        c = card('unit_scale:'+p['source_id'], 'Масштаб измерителя нормы', owner.volume, owner.unit, role='unit_scale',
            found_value=p['value'], found_unit=p['unit'], source_ids=[p['source_id']], quote=p['quote'],
            source_anchors=[dict(source_id=p['source_id'], quote=p['quote'], rects=[])])
        if s['kind'] == 'normative' and 'измерител' in clean(p['quote']) and quoted(p['value'], p['quote'], s['text']) and unit_quoted(p['unit'], p['quote']) and scale and declared and scale[0] == declared[0] and scale[1] > 0:
            c.update(state='confirmed', reason=f"Объём ВОР соответствует {declared[1]/scale[1]} измерителя нормы; масштаб {p['value']} {p['unit']} учтён.")
        cards.append(c)
    if not any(c['role'] == 'unit_scale' for c in cards) and any(s['kind'] == 'normative'
        and re.search(r'\d{2}-\d{2}-\d{3}-\d{2}', s['label']) for s in sources.values()):
        cards.append(card('unit_scale:missing', 'Измеритель нормы', owner.volume, owner.unit, role='unit_scale'))
    # A child material has its own verdict. Its rate must not block the work.
    child_indices = {v['material_index'] for v in relation.get('children') or []}
    for c in cards:
        parts = c['id'].split(':')
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) in child_indices:
            c['required'] = False
        if c['id'] == 'children:limit':
            c['required'] = False
    basis = result.get('local_work_basis')
    composition = result.get('quantity_composition')
    if composition and number(composition['declared_value']) == number(owner.volume) and measure('1', composition['unit']) == measure('1', owner.unit):
        c = next(c for c in cards if c['id'] == 'quantity')
        anchor = dict(source_id=composition['source_id'], quote=' + '.join(v['value'] for v in composition['terms']),
            rects=[v['rect'] for v in composition['terms']])
        c.update(state=composition['state'], found_value=composition['total'], found_unit=composition['unit'],
            source_ids=[composition['source_id']], source_anchors=[anchor], quote=anchor['quote'],
            reason='Количество извлечено по заголовкам и итоговой строке таблицы; слагаемые сохранены с координатами.')
    if basis:
        for c in cards:
            if c['id'] in {'quantity', 'thickness'}:
                value = basis['quantity'] if c['id'] == 'quantity' else basis['thickness_cm']
                unit = basis['unit'] if c['id'] == 'quantity' else 'см'
                c.update(state='confirmed', found_value=value, found_unit=unit,
                    source_ids=basis['project_source_ids'], reason='Подтверждено согласованными структурой таблицы ПД и описанием конструкции.')
            elif c['id'] == 'unit_scale:missing':
                c.update(state='confirmed', found_value=basis['norm_scale'], found_unit=basis['unit'],
                    source_ids=basis['normative_source_ids'], reason='Измеритель нормы прочитан из источника; площадь ВОР приведена к его масштабу.')
            anchors = basis.get('anchors', {}).get('unit_scale' if c['role'] == 'unit_scale' else c['id'])
            if anchors:
                c['source_anchors'] = anchors
                c['source_ids'] = list(dict.fromkeys(a['source_id'] for a in anchors))
                c['quote'] = anchors[0]['quote']
    if composition:
        # Prefer the actual operands to distant header rectangles when opening a sum.
        c = next(c for c in cards if c['id'] == 'quantity')
        if number(composition['declared_value']) == number(owner.volume):
            c['source_anchors'] = [dict(source_id=composition['source_id'], quote=' + '.join(v['value'] for v in composition['terms']),
                rects=[v['rect'] for v in composition['terms']])]
    return cards


def enrich(position, result, positions, relation):
    output = deepcopy(result)
    output['parameter_cards'] = build_cards(position, output, positions, relation)
    output['parameter_schema_version'] = 1
    return output


def inherit_work_evidence(result, parent, index):
    """Reuse verified work geometry for a linked material, retaining source IDs."""
    if not parent.get('local_work_basis'):
        return result
    output = deepcopy(result)
    mapping = {s['id']: f'work-{index}:{s["id"]}' for s in parent['sources']}
    output['sources'] = [s for s in output['sources'] if s['id'] not in mapping.values()]
    output['sources'] += [{**deepcopy(s), 'id':mapping[s['id']]} for s in parent['sources']]
    basis = deepcopy(parent['local_work_basis'])
    for key in ('project_source_ids','normative_source_ids'):
        basis[key] = [mapping[i] for i in basis[key]]
    for anchors in basis.get('anchors',{}).values():
        for anchor in anchors:
            anchor['source_id'] = mapping[anchor['source_id']]
    output['local_work_basis'] = basis
    if parent.get('quantity_composition'):
        composition = deepcopy(parent['quantity_composition'])
        composition['source_id'] = mapping[composition['source_id']]
        composition['parent_position_index'] = index
        output['quantity_composition'] = composition
    return output
