"""Interpret literal area/rate/unit arithmetic without certifying its project basis."""
import ast
from copy import deepcopy
from decimal import Decimal
import re
from typing import Any

PROOF = 'workbook_area_rate_v1'
RATE = re.compile(r'(?:расход|норма\s+расхода)(?:\s+[а-яёa-z]+){0,4}\s*[:=–—-]?\s*'
    r'(\d+(?:[.,]\d+)?)\s*(кг|kg|л|l|[мm][3³])\s*/\s*'
    r'(?:(\d+(?:[.,]\d+)?)\s*)?(?:м|m)(?:²|2|\^2)(?!\w)', re.I)


def analyze(position) -> dict[str, Any] | None:
    """Only an explicit, unique dimensioned rate and bounded product are typed."""
    rates = list(RATE.finditer(getattr(position, 'comment', '') or ''))
    if len(rates) != 1:
        return None
    rate = Decimal(rates[0][1].replace(',', '.'))
    per_area=Decimal((rates[0][3] or '1').replace(',','.'))
    if rate <= 0 or rate == 1000:
        return None
    if per_area <= 0 or per_area > Decimal('1e6'):
        return None
    output_unit=str(position.unit).strip().rstrip('.').lower().replace('³','3')
    numerator=rates[0][2].lower().replace('³','3')
    numerator={'kg':'кг','l':'л','m3':'м3'}.get(numerator,numerator)
    pair=(numerator,output_unit)
    conversions={('кг','т'):('mass','kilograms_to_tonnes',Decimal(1000),False),
        ('кг','t'):('mass','kilograms_to_tonnes',Decimal(1000),False),
        ('кг','кг'):('mass','same_mass_unit',Decimal(1),False),
        ('кг','kg'):('mass','same_mass_unit',Decimal(1),False),
        ('л','т'):('mass','litres_to_cubic_metres',Decimal(1000),True),
        ('л','t'):('mass','litres_to_cubic_metres',Decimal(1000),True),
        ('л','м3'):('volume','litres_to_cubic_metres',Decimal(1000),False),
        ('л','m3'):('volume','litres_to_cubic_metres',Decimal(1000),False),
        ('м3','м3'):('volume','same_volume_unit',Decimal(1),False),
        ('м3','m3'):('volume','same_volume_unit',Decimal(1),False)}
    if pair not in conversions:
        return None
    output_dimension,conversion,scale,density_required=conversions[pair]
    divisor=per_area*scale
    expression = (position.calculation or {}).get('expression') or position.formula
    if not isinstance(expression, str) or len(expression) > 250:
        return None
    text = expression.strip().lstrip('=').replace(',', '.').replace('×', '*')
    if not re.fullmatch(r'[0-9.\s*/()]+', text):
        return None
    try:
        tree = ast.parse(text, mode='eval')
        if len(list(ast.walk(tree))) > 30:
            return None
        def factors(node, sign=1):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                literal = ast.get_source_segment(text, node)
                if not literal or not re.fullmatch(r'\d+(?:\.\d+)?', literal):
                    raise ValueError('Unsupported literal')
                value = Decimal(literal)
                if not value.is_finite() or value <= 0 or value > Decimal('1e15'):
                    raise ValueError('Out of bounds')
                return [(value, sign)]
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
                return factors(node.left, sign) + factors(node.right, -sign if isinstance(node.op, ast.Div) else sign)
            raise ValueError('Unsupported expression')
        operands = factors(tree.body)
    except (SyntaxError, ValueError, ArithmeticError):
        return None
    top = [v for v, sign in operands if sign == 1]
    bottom = [v for v, sign in operands if sign == -1]
    denominator=Decimal(1)
    for value in bottom:
        denominator*=value
    if len(top) != 2 or len(bottom)>2 or denominator!=divisor or top.count(rate) != 1:
        return None
    area = next(v for v in top if v != rate)
    if divisor!=1 and area==divisor:
        return None  # One numeric card cannot distinguish two different roles.
    denominator_label='' if per_area==1 else str(per_area)
    return dict(kind=PROOF, expression=expression, area=str(area), area_unit='м2',
        rate=str(rate), rate_unit=numerator+'/'+denominator_label+'м2',
        rate_quote=rates[0][0], comment=position.comment,
        rate_origin='vor_comment', rate_applicability_verified=False,
        divisor=str(divisor),rate_denominator_area=str(per_area),conversion=conversion,
        output_dimension=output_dimension,output_unit=position.unit,
        density_required=density_required, project_quantity_verified=False)


def related_area(position, positions):
    """Find a unique earlier construction by layer and section, never by quantity."""
    title = position.work_name.casefold()
    layer = re.search(r'(выравнивающ|нижн|средн|верхн|подстилающ)\w*\s+сло', title)
    own = getattr(position, 'position_source', None)
    if not layer or own is None or not getattr(own, 'sections', None):
        return []
    matches = []
    for candidate in positions.values():
        source = getattr(candidate, 'position_source', None)
        name = candidate.work_name.casefold()
        if (source is None or candidate.index == position.index
                or str(candidate.unit).strip().replace('²', '2') != 'м2'
                or not name.startswith('устройство ') or layer[1] not in name
                or source.sheet != own.sheet or source.document_sha256 != own.document_sha256
                or source.sections != own.sections or source.row >= own.row):
            continue
        matches.append(dict(index=candidate.index, row=source.row, work=candidate.work_name,
            quantity=candidate.volume, unit=candidate.unit, reason='same_section_and_named_layer',
            project_quantity_verified=False))
    return matches


def quantity_contract(position) -> dict[str, Any] | None:
    """Declare an input role, not a found quantity or certified consumption."""
    proof=analyze(position)
    if proof is None:
        return None
    from normative_verification_contract import quantity_subject
    return dict(role='calculated_output',output_unit=position.unit,**quantity_subject(position),
        project_input=dict(claim_id='input:area',parameter='quantity',role='calculation_input',unit='м2'),
        rate_unit=proof['rate_unit'],density_required=proof['density_required'],
        direct_output_evidence_allowed=True,verified=False)


def project_rate_claim(claim):
    return bool(claim in {'quantity:consumption','derived:rate'}
        or re.fullmatch(r'basis:comment:consumption:\d+',claim))


def project_rate_proof(position,result,positions=None):
    """Derive a physical rate from independently proved own output and area.

    This does not certify a work norm, its resource rate or a guessed density.
    Both facts are rebuilt from literal proposals for the current subject.
    """
    from analysis_replay import digest
    from normative_parameters import build_cards,measure,same_numeric_result
    from normative_parent_basis import _anchored_card,_proposal_dependencies
    if (position is None or not result.get('sources') or not result.get('parameter_proposals')
            or not (recipe:=analyze(position)) or recipe['density_required']):
        return None
    literal={**result,'local_work_basis':None,'quantity_composition':None,'parameter_cards':[]}
    cards={c['id']:c for c in build_cards(position,literal,positions or {position.index:position},{})}
    area=cards.get('input:area') or {}
    quantity=cards.get('quantity') or {}
    arithmetic=cards.get(f'calculation:{position.index}') or {}
    lookup={s['id']:s for s in result.get('sources',[])}
    if (arithmetic.get('state')!='confirmed' or any(c.get('state')!='confirmed'
            or not _anchored_card(c,lookup) for c in (area,quantity))):
        return None
    av=measure(area.get('found_value'),area.get('found_unit',''))
    qv=measure(quantity.get('found_value'),quantity.get('found_unit',''))
    unit=measure('1',position.unit)
    if not av or not qv or not unit or av[0]!='area' or av[1]<=0 or qv[1]<=0 or qv[0]!=unit[0]:
        return None
    expected=Decimal(recipe['rate'])/Decimal(recipe['divisor'])*unit[1]
    actual=qv[1]/av[1]
    if not same_numeric_result(actual,expected):
        return None
    roots=set(area['source_ids'])|set(quantity['source_ids'])
    dependencies=set()
    proposals=[]
    for proposal in result.get('parameter_proposals',[]):
        if proposal.get('parameter')!='quantity' or proposal.get('source_id') not in roots:
            continue
        ids=_proposal_dependencies(proposal,lookup)
        if not ids:
            return None
        dependencies.update(ids)
        proposals.append(proposal)
    if not roots<=dependencies or not proposals:
        return None
    versions={lookup[sid].get('version_id') for sid in dependencies}
    if len(versions)!=1 or not all(versions) or any(lookup[sid]['kind']!='project' for sid in dependencies):
        return None
    return dict(kind='project_geometric_rate_v1',position_index=position.index,
        expression=recipe['expression'],rate=recipe['rate'],rate_unit=recipe['rate_unit'],
        ratio_in_base_units=format(actual,'f'),root_source_ids=sorted(roots),
        area_card=deepcopy(area),quantity_card=deepcopy(quantity),
        source_sha256={sid:digest(lookup[sid]) for sid in sorted(dependencies)},
        proposals_sha256=digest(proposals),norm_applicability_verified=False,density_assumed=False)



def geometric_rate_replay_supported(position,result):
    """A previously certified geometric rate must still match its raw inputs.

    Interpretation-only workbook traces create no new normative obligation.
    A stored physical proof cannot survive changed sources/proposals merely
    because its old input/output cards are still green.
    """
    events=[e for e in result.get('evidence_trace',[]) if e.get('kind')==PROOF
        and 'geometric_rate_basis' in e]
    if not events:
        return True
    if len(events)!=1 or position is None or not isinstance(events[0]['geometric_rate_basis'],dict):
        return False
    try:
        from normative_parameters import measure,same_numeric_result
        rebuilt=project_rate_proof(position,result)
        if not rebuilt or rebuilt!=events[0]['geometric_rate_basis']:
            return False
        cards=result.get('parameter_cards',[])
        for key,expected in (('quantity',rebuilt['quantity_card']),('input:area',rebuilt['area_card'])):
            actual=[c for c in cards if c.get('id')==key]
            if len(actual)!=1:
                return False
            actual=actual[0]
            if (actual.get('state')!='confirmed'
                    or measure(actual.get('found_value'),actual.get('found_unit',''))
                        !=measure(expected['found_value'],expected['found_unit'])
                    or set(actual.get('source_ids',[]))!=set(expected['source_ids'])
                    or actual.get('source_anchors')!=expected['source_anchors']):
                return False
        arithmetic=[c for c in cards if c.get('id')==f'calculation:{position.index}']
        return bool(len(arithmetic)==1 and arithmetic[0].get('state')=='confirmed'
            and same_numeric_result(arithmetic[0].get('found_value'),position.volume))
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):
        return False


def project_consumption_supported(item,sources,*,position=None,parameters=(),items=()):
    """Permit PD-only consumption claims only with a recomputed physical proof."""
    if not any(project_rate_claim(c) for c in item.get('claim_ids',[])) or item.get('normative_source_ids'):
        return True
    if item.get('state')!='confirmed':
        return False  # An absent calculation proof cannot establish a mismatch.
    result=dict(pipeline_version=4,sources=sources,parameter_proposals=list(parameters),items=list(items))
    proof=project_rate_proof(position,result)
    return bool(proof and set(proof['root_source_ids'])<=set(item.get('project_source_ids',[])))


def annotate(position, result, positions):
    """Identify operands in UI/model evidence; no operand becomes a PD citation."""
    result['evidence_trace'] = [e for e in result.get('evidence_trace', []) if e.get('kind') != PROOF]
    proof = analyze(position)
    if not proof:
        return
    proof['related_area_candidates'] = related_area(position, positions)
    physical_rate=project_rate_proof(position,result,positions)
    if physical_rate:
        proof['geometric_rate_basis']=physical_rate
    # Rebuild the input from literal evidence. A stored green card or another
    # position's amount must not certify a formula operand on its own.
    area_basis=None
    if any(c.get('id')=='input:area' and c.get('state')=='confirmed'
            for c in result.get('parameter_cards',[])) and result.get('sources'):
        from normative_parameters import build_cards,measure
        from normative_parent_basis import _anchored_card
        literal={**result,'local_work_basis':None,'quantity_composition':None,'parameter_cards':[]}
        rebuilt=[c for c in build_cards(position,literal,positions,{}) if c['id']=='input:area']
        sources={s['id']:s for s in result['sources']}
        if (len(rebuilt)==1 and rebuilt[0]['state']=='confirmed'
                and measure(rebuilt[0].get('found_value'),rebuilt[0].get('found_unit',''))==measure(proof['area'],'м2')
                and _anchored_card(rebuilt[0],sources)):
            area_basis=rebuilt[0]
            proof['area_operand_basis']=deepcopy(area_basis)
    result['evidence_trace'].append(proof)
    for card in result.get('parameter_cards', []):
        parts = card['id'].split(':')
        if len(parts) != 3 or parts[:2] != ['factor', str(position.index)]:
            continue
        try:
            value = Decimal(parts[2])
        except ArithmeticError:
            continue
        if value == Decimal(proof['rate']):
            card.update(label='Расход из примечания ВОР', unit=proof['rate_unit'], quote=proof['rate_quote'],
                reason='Расход прочитан из примечания ВОР и подставлен в формулу. '
                    'Это заданное значение; отдельно проверяются его применимость и единицы результата.')
            if physical_rate and card['state']=='needs_facts':
                anchors=[deepcopy(a) for c in (physical_rate['quantity_card'],physical_rate['area_card'])
                    for a in c['source_anchors']]
                card.update(state='confirmed',role='calculation',label='Проектный геометрический расход',
                    found_value=proof['rate'],found_unit=proof['rate_unit'],
                    source_ids=physical_rate['root_source_ids'],source_anchors=anchors,
                    reason='Расход получен из независимо подтверждённых собственного количества '
                        'материала и площади того же объекта ПД; размерность и арифметика проверены. '
                        'Норма способа работ и её условия этим не подтверждаются.')
        elif value == Decimal(proof['divisor']) and value != 1:
            if proof['density_required']:
                card.update(label='Перевод литров и масса', role='calculation', state='needs_facts',
                    quote=proof['rate_quote'], reason='При расходе в л/м² площадь × расход / 1000 даёт м³. '
                        'Для получения тонн нужна плотность эмульсии в т/м³; значение 1 автоматически не принимается.')
            elif any(c['id'] == f'calculation:{position.index}' and c['state'] == 'confirmed'
                    for c in result['parameter_cards']):
                if proof['conversion']=='kilograms_to_tonnes' and Decimal(proof['rate_denominator_area'])==1:
                    label='Перевод кг в тонны'
                    reason='Площадь в м² × расход в кг/м² даёт кг; деление на 1000 переводит кг в тонны. '
                else:
                    label='Масштаб расхода и перевод единиц'
                    reason='Размерный расход '+proof['rate_unit']+' и делитель '+proof['divisor']+' согласованы с единицей '+str(position.unit)+'. '
                card.update(label=label, role='calculation', state='confirmed', quote=proof['rate_quote'],
                    reason=reason+'Основание площади и применимость расхода проверяются отдельно.')
        elif value == Decimal(proof['area']):
            candidates = proof['related_area_candidates']
            hint = (f" Найдена связанная работа в строке {candidates[0]['row']}: {candidates[0]['quantity']} м². "
                'Её площадь нужно сверить с исходным числом формулы и основанием ПД.' if len(candidates) == 1 else
                ' Однозначная связь с площадью другой работы пока не установлена.')
            card.update(label='Исходная площадь расчёта', unit='м2', quote=f"Формула ВОР: {proof['expression']}",
                reason='Это площадь в расчёте расхода; готовый результат отдельной строкой ПД искать не требуется.' + hint)
            if area_basis and card['state']=='needs_facts':
                card.update({key:deepcopy(area_basis[key]) for key in
                    ('found_value','found_unit','source_ids','source_anchors','quote')})
                card.update(state='confirmed',reason='Площадь этого операнда повторно подтверждена '
                    'буквальными источниками карточки input:area для текущей позиции. '
                    'Применимость расхода и зависимый результат проверяются отдельно.')
