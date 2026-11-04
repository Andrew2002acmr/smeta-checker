"""Evidence-backed numeric cards. Decimal arithmetic; no model-supplied verdicts."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import ast
import math
import re
import unicodedata

from normative_local_checks import layer_group
from normative_source_roles import classification_source, MESSAGE as CLASSIFICATION_MESSAGE
from vor_formula import calculate_cell

PARAMETER_SCHEMA_VERSION = 16


def _parameter_claim(claim, kind):
    # A normative threshold (geometry:comment:thickness:*) is a different
    # obligation from the literal physical thickness extracted from the PD.
    if kind == 'quantity':
        return claim == 'quantity'
    if kind == 'input:area':
        return claim == kind
    return claim in {'geometry:thickness', 'thickness'}


def material_fractions(text):
    # A drawing designation such as 006/26 is not aggregate grading. Bind
    # slash values to material vocabulary instead of scanning arbitrary IDs.
    text = re.sub(r'\s+', ' ', str(text).casefold())
    return {a+'/'+b.replace(',', '.') for a,b in re.findall(
        r'(?:фр(?:акц\w*)?\.?|смес\w*|щпс|тип(?:а)?)\s*[^|]{0,50}?\b(0|[1-9]\d*)/((?:0|[1-9]\d*)(?:[.,]\d+)?)\b',text)}


def construction_conflict(text, work, height_cm=None):
    wanted, found = material_fractions(work), material_fractions(text)
    if wanted and found and not found <= wanted:
        return True
    heights = [Decimal(v.replace(',', '.'))*(100 if u in {'м','m'} else 1)
        for v,u in re.findall(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|[мm])\b', text, re.I)]
    return bool(height_cm is not None and any(h != number(height_cm) for h in heights))


def number(value):
    try:
        text = str(value).replace(',', '.').replace(' ', '')
        if len(text) > 48 or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            return None
        result = Decimal(text)
        return result if result.is_finite() and abs(result) <= Decimal('1e18') else None
    except InvalidOperation:
        return None


def same_numeric_result(left, right):
    """Ignore at most two binary-float ULPs, not engineering differences."""
    a, b = (number(format(v, 'f')) if isinstance(v, Decimal) else number(v)
            for v in (left, right))
    if a is None or b is None:
        return False
    if a == b:
        return True
    tolerance = Decimal.from_float(max(math.ulp(float(a)), math.ulp(float(b)))) * 2
    return abs(a - b) <= tolerance


def clean(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text)).casefold()).strip()


UNITS = {'м': ('length', '1'), 'см': ('length', '.01'), 'мм': ('length', '.001'), 'км': ('length', '1000'),
    'м2': ('area', '1'), 'га': ('area', '10000'), 'м3': ('volume', '1'), 'шт': ('count', '1'), 'т': ('mass', '1000'),
    'кг': ('mass', '1'), '': ('factor', '1'), '1': ('factor', '1')}
UNITS.update({'m': UNITS['м'], 'mm': UNITS['мм'], 'cm': UNITS['см'], 'km': UNITS['км'],
    'm2': UNITS['м2'], 'm3': UNITS['м3'], 'kg': UNITS['кг'], 't': UNITS['т']})


def measure(value, unit):
    unit = clean(unit).replace(' ', '').rstrip('.')
    spec = UNITS.get(unit)
    amount = number(value)
    return (spec[0], amount * Decimal(spec[1])) if spec and amount is not None else None


def unit_quoted(unit, quote):
    literal=clean(unit).replace(' ','').rstrip('.')
    # The measurement parser already accepts SI Latin/Russian spellings.
    # Citation validation must use the same aliases, without converting scale
    # (m is not cm), dimension (m is not m2) or missing units.
    aliases=[key for key,spec in UNITS.items() if key and literal in UNITS and spec==UNITS[literal]]
    aliases=aliases or [literal]
    return bool(literal and re.search(r'(?<![а-яa-z])(?:'+'|'.join(map(re.escape,aliases))+
        r')(?![а-яa-z0-9])',clean(quote)))


def quoted(value, quote, text):
    """A fabricated quote, number embedded in another number, or NaN is no proof."""
    amount = number(value)
    literal = clean(quote)
    # PDF extraction can split a decimal after its separator (``0, 20``).
    # Neither 0 nor 20 is a standalone literal in that fragment. Preserve the
    # decimal value; a repaired anchor must still supply the actual units.
    # A complete unit is a boundary in PDF geometry such as h=25см. Arbitrary
    # word suffixes and numbers embedded in designations remain rejected.
    values = {number(match[0]) for match in re.finditer(
        r'(?<![\w.,])[-+]?\d+(?:[.,]\s*\d+)?', literal)
        if (not re.match(r'\w',literal[match.end():]) or re.match(
            r'(?:мм|см|м[23]?|mm|cm|m[23]?|кг|kg|т|t|шт|га)(?!\w)',literal[match.end():]))
        and not re.match(r'[.,]\s*\d',literal[match.end():])
        if not re.search(r'\d[.,]\s*$', literal[:match.start()])
        and not re.match(r'[.,]\s+\d', literal[match.end():])}
    # A complete dimensioned literal such as 20cm or 5см is independently
    # bounded even when shorter than five characters. Bare short numbers,
    # partial words and fabricated units retain the original rejection.
    dimensioned=re.fullmatch(r'[+-]?\d+(?:[.,]\s*\d+)?\s*'
        r'(?:мм|см|м[23]?|mm|cm|m[23]?|кг|kg|т|t|шт|га)',literal)
    occurrence=(re.search(r'(?<![\w.,])'+re.escape(literal)+r'(?!\w)',clean(text))
        if dimensioned else literal in clean(text))
    return bool(amount is not None and (len(literal)>=5 or dimensioned) and occurrence
        and amount in values)


def unit_scale_quoted(value, unit, quote, text):
    """A literal norm measure may omit 1, e.g. ``Измеритель: т``."""
    if quoted(value, quote, text) and unit_quoted(unit, quote):
        return True
    implicit = re.fullmatch(r'измеритель\s*:?\s*(м[23]|m[23]|шт\.?|т|кг|м|m|га)\.?', clean(quote))
    return bool(number(value) == 1 and implicit and clean(quote) in clean(text)
        and measure('1', implicit[1]) == measure('1', unit) and measure('1', unit))


def formula_control_reason(expression, value, owner, work):
    """Prove only formula syntax or a dimensionally complete direct conversion.

    An arbitrary /1000, rate or density is never a proved unit conversion.
    The caller must have independently recalculated the whole expression.
    """
    try:
        tree = ast.parse(expression.lstrip('=').replace('$', ''), mode='eval')
    except (SyntaxError, ValueError):
        return None
    amount = number(value)
    literals = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float)) and number(node.value) == amount]
    round_digits = [node.args[1] for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id.upper() == 'ROUND' and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, int)
        and 0 <= node.args[1].value <= 28]
    if literals and all(node in round_digits for node in literals):
        return f'Параметр ROUND задаёт {value} знаков после запятой; это точность вычисления, а не исходная величина.'
    original, target = measure('1', owner.unit), measure('1', work.unit)
    root = tree.body
    if (owner.index == work.index or not original or not target or original[0] != target[0]
            or not isinstance(root, ast.BinOp) or not isinstance(root.op, ast.Div)
            or not isinstance(root.right, ast.Constant) or number(root.right.value) != amount
            or not amount or amount != target[1]/original[1]):
        return None
    source_row = owner.position_source.row if owner.position_source else None
    operand = root.left
    is_parent = ((isinstance(operand, ast.Name) and source_row and operand.id.upper() == f'D{source_row}')
        or (isinstance(operand, ast.Constant) and number(operand.value) == number(owner.volume)))
    if is_parent:
        return f'Перевод единиц связанной работы {owner.unit} → {work.unit}: делитель {value} подтверждён размерностями.'
    return None


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


def build_cards(position, result, positions, relation, *, resource_proofs=None):
    sources = {s['id']: s for s in result['sources']}
    proposals = result.get('parameter_proposals') or []
    cards, verified = [], {}
    owner = positions.get((relation.get('parent') or {}).get('index'), position)
    row = position.position_source.row if position.position_source else None
    group = layer_group(owner, result.get('related_positions') or [],positions)
    from normative_layer_assembly import layer_assembly,material_allocation_proof
    allocation=(material_allocation_proof(position,result,positions,relation)
        if str(getattr(position,'position_type','')).casefold()=='материал'
        and all(callable(getattr(p,'model_dump',None)) for p in positions.values()) else None)
    thickness_group = group or layer_assembly(owner,positions)
    work_scope = owner.work_name+' '+position.work_name+' '+' '.join(
        child.get('work', '') for child in relation.get('children') or [])
    height = re.search(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', owner.work_name, re.I)
    from normative_layer_scope import technological_part

    def role_supported(proposal,source,comparison_group):
        declared=set(re.findall(r'нижн|средн|верхн',clean(owner.work_name)))
        found=set(re.findall(r'нижн|средн|верхн',clean(proposal.get('scope_quote',''))))
        return (not declared or found==declared
            or technological_part(owner,proposal,source,comparison_group,work_scope))

    def card(key, label, value, unit, role='project_value', **extra):
        return dict(id=key, label=label, role=role, row=row, declared_value=str(value), unit=unit,
            state='needs_facts', reason='Нет структурированного подтверждения из источника.', **extra)

    def compare(c, kind, expected_value=None, *, claim_kind=None, verification_key=None, dimension_required=None):
        comparison_group=thickness_group if kind=='thickness' else group
        expected = measure(expected_value or c['declared_value'], c['unit'])
        wanted_kind = 'project' if kind in {'quantity', 'thickness'} else 'normative'
        matches = [p for p in proposals if p['parameter'] == kind and p['source_id'] in sources
            and sources[p['source_id']]['kind'] == wanted_kind
            and (p.get('operation') != 'sum' or sources[p['source_id']]['origin'] == 'calculated_from_cells')]
        if kind == 'quantity':
            from normative_fact_contract import dimension_candidates
            if dimension_required:
                matches=[p for p in matches if (m:=measure(p['value'],p['unit'])) and m[0]==dimension_required]
            matches = dimension_candidates(matches, expected)
        if kind=='thickness' and thickness_group and not group:
            from normative_layer_assembly import assembly_scope_matches
            matches=[p for p in matches if assembly_scope_matches(owner,p,sources[p['source_id']])]
            if not matches:
                c['reason']='Для суммы технологических слоёв не подтверждена конструкция того же участка ПД.'
                return c
        if c['role'] == 'coefficient':
            scalar_matches=[p for p in matches if (m:=measure(p['value'],p['unit'])) and m[0]=='factor']
            if matches and not scalar_matches:
                c['reason']='Предложен размерный расход вместо безразмерного коэффициента; сравнение не выполнено.'
                return c
            matches=scalar_matches
            exact = [p for p in matches if number(p['value']) == number(c['declared_value'])]
            matches = exact or matches
        # Several reads of one physical page can expose the SAME literal
        # parameter. Duplicate excerpts are not conflicting engineering facts.
        if kind in {'quantity','thickness'}:
            valid = [p for p in matches if p.get('scope_state')=='verified'
                and not construction_conflict(p.get('scope_quote',''),work_scope,comparison_group['height'] if comparison_group else None)
                and (kind != 'thickness' or role_supported(p,sources[p['source_id']],comparison_group))
                and quoted(p['value'],p['quote'],sources[p['source_id']]['text'])]
            if valid:
                # A claimed same-scope fact with a broken numeric citation is
                # unresolved, not evidence that the other candidate is right.
                # Retain it until anchor repair can verify its literal value.
                # A fabricated quote has no evidentiary weight and must not
                # veto an independently verified fact from the actual text.
                invalid_quotes = [p for p in matches if p.get('scope_state') == 'verified'
                    and len(clean(p['quote'])) >= 5 and clean(p['quote']) in clean(sources[p['source_id']]['text'])
                    and not quoted(p['value'], p['quote'], sources[p['source_id']]['text'])]
                matches = valid + invalid_quotes
        def evidence_key(p):
            s = sources[p['source_id']]
            scope = p.get('scope_quote', '')
            # Repeated, independently readable statements in the SAME document
            # version and exact scope corroborate one fact even on two pages.
            # A different scope/version or unverified OCR is never merged.
            if (kind in {'quantity', 'thickness'} and s.get('version_id')
                    and p.get('scope_state') == 'verified' and scope
                    and clean(scope) in clean(s['text']) and quoted(p['value'], p['quote'], scope)
                    and unit_quoted(p['unit'], p['quote'])
                    and not construction_conflict(s['text'] if ':cell:' in s['id'] or s['origin'] == 'calculated_from_cells'
                        else scope, work_scope, comparison_group['height'] if comparison_group else None)
                    and not re.search(r'\b(?:не более|не менее|до\s*\d|от\s*\d|в пределах|диапазон)', clean(p['quote']))
                    and not any(i.get('state') == 'needs_facts' and p['source_id'] in i.get('project_source_ids', [])
                        and any(_parameter_claim(claim, kind)
                            for claim in i.get('claim_ids', [])) for i in result.get('items', []))
                    and s['origin'] != 'uncertain_table'
                    and (not s['origin'].startswith('ocr') or s.get('numeric_verified'))):
                return ('corroborated', s['version_id'], measure(p['value'], p['unit']), clean(scope))
            return ('single', s.get('version_id', p['source_id']), s.get('page', p['source_id']),
                measure(p['value'], p['unit']), clean(scope))
        distinct = {evidence_key(p) for p in matches}
        if len(distinct) != 1:
            c['reason'] = 'Нужен однозначный источник параметра.' if matches else c['reason']
            if not matches and kind=='coefficient':
                c['reason']='Не найден нормативный источник коэффициента и условий его применения.'
            pending = [p for p in proposals if p['parameter'] == kind and p.get('operation') == 'sum'
                and 2 <= len(p.get('operand_source_ids', [])) <= 12]
            if not matches and len(pending) == 1:
                p = pending[0]
                ids = p['operand_source_ids']
                operands = [sources[i] for i in ids if i in sources and sources[i]['kind'] == 'project']
                values = [number(s['text'].rsplit('\n', 1)[-1]) for s in operands]
                if any(construction_conflict(s['text'], work_scope, comparison_group['height'] if comparison_group else None) for s in operands):
                    c['reason'] = 'Выбранные ячейки относятся к другому материалу или проектной толщине; совпадение чисел не подтверждает основание.'
                    return c
                if len(operands) == len(ids) == len(set(ids)) and all(v is not None for v in values):
                    # Showing located operands is useful even when independent
                    # OCR or their unit is unresolved. Never compare this sum
                    # or treat it as a confirmed project quantity.
                    total = sum((v for v in values if v is not None), Decimal(0))
                    c.update(found_value=str(total), found_unit=p['unit'], source_ids=ids,
                        source_anchors=[dict(source_id=i, quote=s['text'].rsplit('\n', 1)[-1], rects=s.get('rects', []))
                            for i,s in zip(ids, operands, strict=True)],
                        reason='Слагаемые найдены; принадлежность, единица или независимое подтверждение OCR ещё не проверены. Сравнение не выполнено.')
            if not matches and kind == 'quantity':
                previous = next((v for v in result.get('items') or []
                    if v.get('topic') == 'Локальная проверка: площадь по структуре OCR-таблицы'), None)
                if previous:
                    c['reason'] = previous['explanation']
                    c['source_ids'] = [v for v in previous.get('project_source_ids') or [] if v in sources]
            return c
        p = matches[0]
        source = sources[p['source_id']]
        if kind == 'coefficient' and classification_source(source):
            c['reason'] = CLASSIFICATION_MESSAGE
            return c
        if kind=='quantity':
            from normative_quantity_scope import partial_side_issue,unresolved_layer_issue
            owner_source=getattr(owner,'position_source',None)
            section_context=' '.join(section.get('text','') if isinstance(section,dict) else getattr(section,'text','')
                for section in getattr(owner_source,'sections',[]) or [])
            issue=partial_side_issue(p,list(sources.values()),owner.work_name,section_context)
            if issue:
                c['reason']=issue['reason']
                return c
            layer_issue=unresolved_layer_issue(p,source,owner.work_name,group['height'] if group else None)
            if layer_issue:
                c['reason']=layer_issue
                return c
        if kind in {'quantity', 'thickness'} and construction_conflict(
                source['text'] if ':cell:' in source['id'] or source['origin'] == 'calculated_from_cells' else p.get('scope_quote', ''),
                work_scope, comparison_group['height'] if comparison_group else None):
            c['reason'] = 'Источник относится к другому материалу или проектной толщине; сравнение не выполнено.'
            return c
        found = measure(p['value'], p['unit'])
        from normative_parameter_feedback import header_parameter_verified
        if not header_parameter_verified(p,source):
            c['reason']='Число физической ячейки проверено отдельно; толщина в OCR-заголовке не имеет независимого подтверждения.'
            return c
        from normative_confirmed_units import unit_confirmed
        unit_present = (kind == 'coefficient' or unit_quoted(p['unit'], p['quote'])
            or kind == 'quantity' and unit_confirmed(source, p['unit']))
        if source.get('origin')=='ocr_verified_header' and not any(
                f.get('parameter')==kind and measure(f.get('value'),f.get('unit',''))==found
                for f in source.get('verified_parameters',[])):
            c['reason']='В этом заголовке независимо проверен другой параметр; его нельзя использовать как количество.'
            return c
        if source['kind'] != wanted_kind or not quoted(p['value'], p['quote'], source['text']) or not expected or not found or not unit_present:
            c['reason'] = ('Для коэффициента нужен нормативный источник с условиями применения.' if kind=='coefficient'
                else 'Число, цитата, тип источника или единица не подтверждены программно.')
            return c
        c.update(found_value=p['value'], found_unit=p['unit'], source_ids=[p['source_id']], quote=p['quote'],
            source_anchors=[dict(source_id=p['source_id'], quote=p['quote'], rects=source.get('rects', []))])
        if (source['origin'].startswith('ocr') and not source.get('numeric_verified')) or source['origin'] == 'uncertain_table':
            c['reason'] = 'Распознано OCR: требуется подтверждение по изображению.'
            return c
        if result.get('pipeline_version', 1) >= 3 and kind in {'quantity', 'thickness'}:
            scope = p.get('scope_quote', '')
            def roles(text):
                return set(re.findall(r'нижн|средн|верхн', clean(text)))
            declared_roles, found_roles = roles(owner.work_name), roles(scope)
            relevant = [i for i in result.get('items', []) if p['source_id'] in i.get('project_source_ids', [])
                and any(_parameter_claim(claim, claim_kind or kind) for claim in i.get('claim_ids', []))]
            disputed = any(i.get('state') == 'needs_facts' for i in relevant)
            if (p.get('scope_state') != 'verified' or not quoted(p['value'], p['quote'], scope)
                    or clean(scope) not in clean(source['text']) or disputed
                    or (declared_roles and (found_roles != declared_roles) and (kind == 'thickness' or found_roles)
                        and not technological_part(owner,p,source,comparison_group,work_scope))):
                c['reason'] = 'Принадлежность числа той же конструкции, слою и участку не подтверждена; сравнение не выполнено.'
                return c
        if re.search(r'\b(?:не более|не менее|до\s*\d|от\s*\d|в пределах|диапазон)', clean(p['quote'])):
            c['reason'] = 'В цитате указано ограничение или диапазон, а не однозначное фактическое значение.'
            return c
        if expected[0] != found[0]:
            c.update(state='possible_mismatch', reason='Размерности единиц не совпадают.')
            return c
        equal = expected[1] == found[1] or (
            kind in {'quantity', 'thickness'} and same_numeric_result(expected[1], found[1]))
        precision = number(c['declared_value'])
        precision_exp = int(precision.as_tuple().exponent) if precision is not None else 0
        base_unit = measure('1', c['unit'])
        if kind == 'quantity' and expected[0] != 'count' and precision is not None and precision_exp < 0 and base_unit:
            equal = equal or (found[1]/base_unit[1]).quantize(Decimal(1).scaleb(precision_exp), rounding=ROUND_HALF_UP) == precision
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
        if kind == 'quantity' and unit_confirmed(source, p['unit']):
            c['reason'] += ' Единица измерения подтверждена пользователем по контексту таблицы; в исходной ячейке она не подписана.'
        if len(matches) > 1 and all(evidence_key(item)[0] == 'corroborated' for item in matches):
            c['source_ids'] = list(dict.fromkeys(item['source_id'] for item in matches))
            c['source_anchors'] = [dict(source_id=item['source_id'], quote=item['quote'],
                rects=sources[item['source_id']].get('rects', [])) for item in matches]
        if kind in {'quantity', 'thickness'}:
            verified[verification_key or claim_kind or kind] = found
        return c

    quantity_card = card('quantity', 'Количество родительской работы по ПД' if owner.index != position.index else 'Количество по ПД', owner.volume, owner.unit)
    quantity_card['row'] = owner.position_source.row if owner.position_source else None
    from normative_workbook_rate import analyze as area_rate
    recipe=area_rate(owner)
    direct_output=any(p.get('parameter')=='quantity' and (m:=measure(p.get('value'),p.get('unit','')))
        and m[0]==(recipe or {}).get('output_dimension','mass') and p.get('source_id') in sources
        and sources[p['source_id']]['kind']=='project' for p in proposals)
    area_input_mode=bool(recipe and not direct_output)
    if recipe and area_input_mode:
        quantity_card['reason']=(('Масса рассчитывается из площади и расхода; площадь ПД не является готовой массой. '
            if recipe['output_dimension']=='mass' else
            'Объём рассчитывается из площади и размерного расхода; площадь ПД не является готовым объёмом. ')
            +('Для расхода в л/м² дополнительно нужна подтверждённая плотность.' if recipe['density_required'] else
              'Нужно подтвердить применимость расхода; масштаб и перевод единиц проверяются отдельно.'))
        cards.append(quantity_card)
    else:
        cards.append(compare(quantity_card, 'quantity'))
    if recipe:
        area=card('input:area','Площадь-основание расчёта',recipe['area'],'м2')
        area['row']=quantity_card['row']
        area['expression']=recipe['expression']
        explicit_role=any('input:area' in item.get('claim_ids',[]) for item in result.get('items',[]))
        cards.append(compare(area,'quantity',claim_kind='input:area' if explicit_role else 'quantity',
            verification_key='input:area',dimension_required='area'))
    if height:
        h_value, h_unit = height[1], height[2].lower()
        total = str(thickness_group['height']) if thickness_group else h_value
        if thickness_group:
            h_unit = 'см'
        c = card('thickness', 'Толщина проектного слоя', h_value, h_unit, compared_value=total)
        c['row'] = owner.position_source.row if owner.position_source else None
        if 'двухслойн' in owner.work_name.casefold() and not thickness_group:
            c['reason'] = 'Для двухслойного основания не удалось однозначно установить связанную толщину второго слоя.'
            cards.append(c)
        else:
            compared=compare(c, 'thickness', total)
            if thickness_group and not group:
                compared['reason']+=' Толщина сопоставлена по явно связанным технологическим слоям ВОР. '
                compared['reason']+='Общий охват их площадей этим не подтверждён.'
            cards.append(compared)

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
        equal = same_numeric_result(actual.get('value'), work.volume)
        if actual['status'] == 'calculated' and equal and not calc.get('cached_mismatch'):
            c.update(state='confirmed', reason='Формула пересчитана программно по сохранённым значениям ячеек.')
        elif actual['status'] == 'calculated' and not equal:
            c.update(state='possible_mismatch', reason='Результат формулы не совпадает с объёмом ВОР.')
        elif calc.get('cached_mismatch'):
            c['reason'] = 'Сохранённый результат Excel отличается от формулы: требуется сверка исходной ячейки.'
        else:
            c['reason'] = 'Формулу нельзя надёжно пересчитать по сохранённым данным.'
        cards.append(c)
        arithmetic_confirmed = c['state'] == 'confirmed'
        from normative_addition_basis import addition_basis
        additive_basis = addition_basis(work,owner,expression,quantity_card,
            arithmetic_confirmed=arithmetic_confirmed)
        excluded = {number(owner.volume), Decimal(0)}
        if height:
            depth = measure(height[1], height[2].lower())
            if depth:
                excluded.add(depth[1])
        factors = list(dict.fromkeys(str(d['value']) for d in actual.get('dependencies') or []
            if d.get('origin') == 'formula_literal' and number(d.get('value')) not in excluded))
        literal_values={number(d.get('value')) for d in actual.get('dependencies') or [] if d.get('origin')=='formula_literal'}
        owner_measure,work_measure=measure('1',owner.unit),measure('1',work.unit)
        dimensional_factor=bool(owner_measure and work_measure and owner_measure[0]=='area' and work_measure[0]=='volume'
            and height and (depth:=measure(height[1],height[2].lower()))
            and {number(owner.volume),depth[1]}<=literal_values and re.fullmatch(r'[()\d.,*\s=]+',expression))
        if len(factors) > 8:
            cards.append(card(f'factors:limit:{work.index}', 'Дополнительные параметры формулы', str(len(factors)), '', role='coefficient'))
        for value in factors[:8]:
            # A literal is not automatically a dimensionless normative factor.
            # Areas, thicknesses and unit divisors also occur in VOR formulae.
            literal=re.escape(str(value)).replace(r'\.',r'[.,]')
            explicit=bool(re.search(r'(?:коэфф\w*|к[\s_.-]*упл|купл)[^\d]{0,25}'+literal+r'(?![\d.,])',
                getattr(work,'comment',''),re.I))
            normative=any(p['parameter']=='coefficient' and number(p['value'])==number(value)
                and p['source_id'] in sources and sources[p['source_id']]['kind']=='normative'
                and quoted(p['value'],p['quote'],sources[p['source_id']]['text'])
                and re.search(r'коэфф|уплотн',clean(p['quote'])) for p in proposals)
            is_factor=explicit or normative or dimensional_factor
            c = card(f'factor:{work.index}:{value}', f"{'Коэффициент' if is_factor else 'Исходное число'} формулы {value} · позиция №{work_number}",
                value, '', role='coefficient' if is_factor else 'project_value')
            c['row'] = work.position_source.row if work.position_source else None
            from normative_resource_consumption import consumption_proof
            resource_proof = consumption_proof(work,owner,result,value,expression,
                arithmetic_confirmed=arithmetic_confirmed)
            from normative_parent_operand import parent_operand
            workbook_input = parent_operand(work,owner,result,value,expression,relation,
                arithmetic_confirmed=arithmetic_confirmed) if not resource_proof and not is_factor else None
            control_reason = formula_control_reason(expression, value, owner, work) if arithmetic_confirmed else None
            if control_reason and (explicit or normative) and not control_reason.startswith('Параметр ROUND'):
                control_reason = None
            if resource_proof:
                c.update(resource_proof['card'])
                if resource_proofs is not None:
                    resource_proofs.append(resource_proof['proof'])
                cards.append(c)
            elif workbook_input:
                c.update(workbook_input['card'])
                if resource_proofs is not None:
                    resource_proofs.append(workbook_input['proof'])
                cards.append(c)
            elif additive_basis and not is_factor:
                c.update(additive_basis)
                cards.append(c)
            elif control_reason:
                c.update(role='calculation', state='confirmed', reason=control_reason)
                cards.append(c)
            elif is_factor:
                cards.append(compare(c, 'coefficient'))
            else:
                c['reason']='Нужно установить назначение числа и источник: объём связанной работы, геометрический параметр, расход или перевод единиц.'
                if re.search(r'/\s*\(?\s*'+literal+r'\s*\)?(?:$|[+*/-])',expression):
                    c['role']='calculation'
                    c['reason']='Делитель формулы: нужно подтвердить входную единицу и масштаб пересчёта.'
                cards.append(c)
    for p in proposals:
        if p['parameter'] != 'unit_scale' or p['source_id'] not in sources:
            continue
        s, scale, declared = sources[p['source_id']], measure(p['value'], p['unit']), measure(owner.volume, owner.unit)
        input_scale=bool(recipe and scale and scale[0]=='area')
        declared_value,declared_unit=(recipe['area'],'м2') if recipe is not None and input_scale else (owner.volume,owner.unit)
        declared=measure(declared_value,declared_unit)
        c = card('unit_scale:'+p['source_id'], 'Масштаб измерителя нормы', declared_value, declared_unit, role='unit_scale',
            found_value=p['value'], found_unit=p['unit'], source_ids=[p['source_id']], quote=p['quote'],
            source_anchors=[dict(source_id=p['source_id'], quote=p['quote'], rects=[])])
        readable = s['origin'] != 'uncertain_table' and (not s['origin'].startswith('ocr') or s.get('numeric_verified'))
        if classification_source(s):
            c['reason'] = CLASSIFICATION_MESSAGE
        elif s['kind'] == 'normative' and readable and 'измерител' in clean(p['quote']) and unit_scale_quoted(p['value'], p['unit'], p['quote'], s['text']) and scale and declared and scale[0] == declared[0] and scale[1] > 0:
            c.update(state='confirmed', reason=f"Объём ВОР соответствует {declared[1]/scale[1]} измерителя нормы; масштаб {p['value']} {p['unit']} учтён.")
            if input_scale:
                c['quantity_role']='calculation_input'
                c['reason']='Площадь-основание формулы приведена к буквальному площадному измерителю нормы. '
                c['reason']+='Собственное количество материала, расход и применимость нормы проверяются отдельно.'
        cards.append(c)
    selected_norms = {sid for item in result.get('items',[]) if 'normative:applicability' in item.get('claim_ids',[])
        for sid in item.get('normative_source_ids',[])}
    if not any(c['role'] == 'unit_scale' for c in cards) and any(s['kind'] == 'normative' and s['id'] in selected_norms
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
    if basis and (not area_input_mode or measure('1',basis['unit'])==measure('1',owner.unit)):
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
        if number(composition['declared_value']) == number(owner.volume) and measure('1',composition['unit'])==measure('1',owner.unit):
            c['source_anchors'] = [dict(source_id=composition['source_id'], quote=' + '.join(v['value'] for v in composition['terms']),
                rects=[v['rect'] for v in composition['terms']])]
    if allocation:
        # The PD proves the whole homogeneous requirement. Individual bases
        # remain declared planning allocations, never fabricated PD cells.
        operand=next(o for o in allocation['operands'] if o['material_index']==position.index)
        limits={key:allocation[key] for key in ('individual_project_volumes_verified',
            'stage_areas_verified','coefficient_applicability_verified','normative_rate_verified')}
        limits['stage_thicknesses_verified']=False
        anchors=deepcopy(allocation['source_anchors'])
        common=dict(state='confirmed',source_ids=list(allocation['root_source_ids']),
            source_anchors=anchors,quote=allocation['project_basis']['volume']['scope_quote'],
            proof_limits=limits,project_total=allocation['project_volume'])
        quantity_card.update(**common,label='Расчётное количество распределённого материала',
            role='calculation',quantity_role='allocated_output',row=row,
            declared_value=str(position.volume),unit=position.unit,
            found_value=operand['calculated_material_volume'],found_unit=operand['unit'],
            reason='Результат расчёта заявленного распределения полного проектного объёма; '
                'часть не является буквальным числом ПД. Площади и физические толщины отдельных '
                'рабочих операций этим не подтверждаются. Коэффициент проверяется отдельно.')
        cards=[c for c in cards if c['id']!='thickness']
        for c in cards:
            if (c['id'].startswith(f'factor:{position.index}:')
                    and number(c['declared_value'])==number(operand['declared_volume'])):
                c.update(**common,role='calculation_declared_allocation',unit=operand['unit'],
                    found_value=operand['declared_volume'],found_unit=operand['unit'],
                    label='Заявленный объём распределения',
                    reason='Плановая часть полного объёма ПД; сумма частей и структурная полнота '
                        'пересобраны по исходным данным. Это не измеренный объём отдельного '
                        'физического слоя и не проектная площадь работы.')
        if resource_proofs is not None:
            resource_proofs.append(allocation)
    if relation.get('material_owner') and not relation.get('parent'):
        from vor_material_ownership import annotate_quantity_cards
        annotate_quantity_cards(cards,position.legacy_task(),relation['material_owner'])
    return cards


def enrich(position, result, positions, relation):
    output = deepcopy(result)
    from normative_fact_contract import restore_numeric_items, record_projection
    restore_numeric_items(output)
    from normative_layer_assembly import recover_thickness
    geometry_owner=positions.get((relation.get('parent') or {}).get('index'),position)
    recover_thickness(geometry_owner,output,positions)
    from normative_claim_migration import apply_review_migration
    apply_review_migration(position,output)
    from normative_quantity_bridge import remove_previous, append_quantity_bridge
    remove_previous(output)
    resource_proofs = []
    output['parameter_cards'] = build_cards(position, output, positions, relation,resource_proofs=resource_proofs)
    from normative_conditional_selection import apply_guard
    if apply_guard(position,output,positions,relation,cards=output['parameter_cards']):
        # A rate derived from a rejected normative choice is no longer proof;
        # independent project geometry remains available to both work layers.
        resource_proofs=[]
        output['parameter_cards']=build_cards(position,output,positions,relation,resource_proofs=resource_proofs)
    from normative_resource_consumption import PROOF
    from normative_layer_assembly import ALLOCATION_PROOF
    output['evidence_trace'] = [event for event in output.get('evidence_trace',[])
        if event.get('kind') not in {PROOF,'rounded_parent_workbook_operand_v1',ALLOCATION_PROOF}]+resource_proofs
    output['parameter_schema_version'] = PARAMETER_SCHEMA_VERSION
    from normative_workbook_arithmetic import record_arithmetic
    record_arithmetic(position,output)
    from normative_unit_measure import apply as apply_unit_measure
    apply_unit_measure(position,output,positions,relation)
    from normative_formula_scale import scale_operand
    owner=positions.get((relation.get('parent') or {}).get('index'),position)
    for card in output['parameter_cards']:
        parts=card['id'].split(':')
        if len(parts)!=3 or parts[0]!='factor' or not parts[1].isdigit():
            continue
        work=positions.get(int(parts[1]))
        if work is None or card['state']!='needs_facts':
            continue
        arithmetic=any(c['id']==f'calculation:{work.index}' and c['state']=='confirmed'
            for c in output['parameter_cards'])
        expression=(work.calculation or {}).get('expression') or work.formula
        scale=scale_operand(work,owner,output,parts[2],expression,arithmetic_confirmed=arithmetic)
        if scale:
            card.update(scale)
    from normative_workbook_rate import annotate as annotate_workbook_rate
    annotate_workbook_rate(position, output, positions)
    if output.get('pipeline_version', 1) >= 3:
        changed = False
        for item in output.get('items', []):
            keys = numeric_card_keys(item.get('claim_ids', []))
            if not keys or item['state'] == 'needs_facts':
                continue
            required_state = 'possible_mismatch' if item['state'] == 'possible_mismatch' else 'confirmed'
            cards = [c for c in output['parameter_cards'] if c['id'] in keys]
            if not cards or any(c['state'] != required_state for c in cards):
                original = deepcopy(item)
                item['state'] = 'needs_facts'
                item['explanation'] += ' Числовое основание не прошло проверку цитаты, OCR, единицы или конструкции; расхождение не доказано.'
                record_projection(output, original, item)
                changed = True
        if changed:
            output['state'] = 'possible_mismatch' if any(i['state']=='possible_mismatch' for i in output['items']) else 'needs_facts'
        gaps = [c for c in output['parameter_cards'] if c.get('required', True) and c['state'] != 'confirmed']
        if gaps:
            output['summary'] = ' '.join(c['label']+': '+c['reason'] for c in gaps)[:2500]
        else:
            old_gaps = [c for c in result.get('parameter_cards') or []
                        if c.get('required', True) and c['state'] != 'confirmed']
            old_summary = ' '.join(c['label']+': '+c['reason'] for c in old_gaps)[:2500]
            if old_gaps and output.get('summary') == old_summary:
                output['summary'] = ' '.join(i['explanation'] for i in output.get('items', [])) or (
                    'Числовые основания проверены; расхождений в арифметике не обнаружено.')
        from normative_layer_conditions import layer_condition_proof,TOPIC
        output['items']=[i for i in output.get('items',[]) if i.get('topic')!=TOPIC]
        proof=layer_condition_proof(position,output,layer_group(position,output.get('related_positions') or []))
        if proof:
            output['items'].append(proof)
        append_quantity_bridge(position, output, positions, relation)
    from normative_coefficient_policy import apply as apply_coefficient_policy
    from normative_material_search import apply_specification_compatibility
    return apply_specification_compatibility(position,apply_coefficient_policy(position,output))


def numeric_card_keys(claims):
    return {'input:area' if claim=='input:area' else 'quantity' if claim in {'quantity', 'unit'} else 'thickness'
        for claim in claims if claim in {'quantity','unit','input:area'} or 'thickness' in claim}


def inherit_work_evidence(result, parent, index):
    """Reuse verified work geometry for a linked material, retaining source IDs."""
    if not parent.get('local_work_basis'):
        return result
    output = deepcopy(result)
    mapping = {s['id']: f'work-{index}:{s["id"]}' for s in parent['sources']}
    output['sources'] = [s for s in output['sources'] if s['id'] not in mapping.values()]
    output['sources'] += [{**deepcopy(s), 'id':mapping[s['id']],
        'physical_source_id':s.get('physical_source_id',s['id'])} for s in parent['sources']]
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
