"""Interpret literal area/rate/unit arithmetic without certifying its project basis."""
import ast
from decimal import Decimal
import re
from typing import Any

PROOF = 'workbook_area_rate_v1'
RATE = re.compile(r'(?:расход|норма\s+расхода)\s*[:=–—-]?\s*(\d+(?:[.,]\d+)?)\s*(кг|kg|л|l)\s*/\s*(?:м|m)(?:²|2|\^2)(?!\w)', re.I)


def analyze(position) -> dict[str, Any] | None:
    """Only an explicit, unique dimensioned rate and bounded product are typed."""
    rates = list(RATE.finditer(getattr(position, 'comment', '') or ''))
    if len(rates) != 1 or str(position.unit).strip().rstrip('.') not in {'т', 't'}:
        return None
    rate = Decimal(rates[0][1].replace(',', '.'))
    if rate <= 0 or rate == 1000:
        return None
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
    if len(top) != 2 or bottom != [Decimal(1000)] or top.count(rate) != 1:
        return None
    area = next(v for v in top if v != rate)
    volume_rate = rates[0][2].lower() in {'л', 'l'}
    return dict(kind=PROOF, expression=expression, area=str(area), area_unit='м2',
        rate=str(rate), rate_unit='л/м2' if volume_rate else 'кг/м2',
        rate_quote=rates[0][0], comment=position.comment,
        rate_origin='vor_comment', rate_applicability_verified=False,
        divisor='1000', conversion='litres_to_cubic_metres' if volume_rate else 'kilograms_to_tonnes',
        density_required=volume_rate, project_quantity_verified=False)


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


def quantity_contract(position):
    """Declare an input role, not a found quantity or certified consumption."""
    proof=analyze(position)
    if proof is None:
        return None
    return dict(role='calculated_output',output_unit=position.unit,
        project_input=dict(claim_id='input:area',parameter='quantity',role='calculation_input',unit='м2'),
        rate_unit=proof['rate_unit'],density_required=proof['density_required'],
        direct_output_evidence_allowed=True,verified=False)


def annotate(position, result, positions):
    """Identify operands in UI/model evidence; no operand becomes a PD citation."""
    result['evidence_trace'] = [e for e in result.get('evidence_trace', []) if e.get('kind') != PROOF]
    proof = analyze(position)
    if not proof:
        return
    proof['related_area_candidates'] = related_area(position, positions)
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
        elif value == 1000:
            if proof['density_required']:
                card.update(label='Перевод литров и масса', role='calculation', state='needs_facts',
                    quote=proof['rate_quote'], reason='При расходе в л/м² площадь × расход / 1000 даёт м³. '
                        'Для получения тонн нужна плотность эмульсии в т/м³; значение 1 автоматически не принимается.')
            elif any(c['id'] == f'calculation:{position.index}' and c['state'] == 'confirmed'
                    for c in result['parameter_cards']):
                card.update(label='Перевод кг в тонны', role='calculation', state='confirmed', quote=proof['rate_quote'],
                    reason='Площадь в м² × расход в кг/м² даёт кг; деление на 1000 переводит кг в тонны. '
                        'Основание площади и применимость расхода проверяются отдельно.')
        elif value == Decimal(proof['area']):
            candidates = proof['related_area_candidates']
            hint = (f" Найдена связанная работа в строке {candidates[0]['row']}: {candidates[0]['quantity']} м². "
                'Её площадь нужно сверить с исходным числом формулы и основанием ПД.' if len(candidates) == 1 else
                ' Однозначная связь с площадью другой работы пока не установлена.')
            card.update(label='Исходная площадь расчёта', unit='м2', quote=f"Формула ВОР: {proof['expression']}",
                reason='Это площадь в расчёте расхода; готовую массу отдельной строкой ПД искать не требуется.' + hint)
