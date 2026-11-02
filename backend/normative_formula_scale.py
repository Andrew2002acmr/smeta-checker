"""Recognize a work amount / literal norm measure independently of its rate."""
import ast


def scale_operand(work,owner,result,value,expression,*,arithmetic_confirmed):
    from normative_parameters import number,measure,unit_scale_quoted
    from normative_source_roles import classification_source
    if not arithmetic_confirmed or work.index==owner.index:
        return None
    expected=number(value)
    text=expression.strip().lstrip('=').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval').body
    except (SyntaxError,ValueError):
        return None
    # No arbitrary division/functions: exactly (work quantity / basis) × rate.
    if not isinstance(tree,ast.BinOp) or not isinstance(tree.op,ast.Mult):
        return None
    division=tree.left
    if (not isinstance(division,ast.BinOp) or not isinstance(division.op,ast.Div)
            or not all(isinstance(node,ast.Constant) and not isinstance(node.value,bool)
                for node in (division.left,division.right,tree.right))):
        return None
    amount=number(ast.get_source_segment(text,division.left))
    divisor=number(ast.get_source_segment(text,division.right))
    if (divisor is None or divisor<=0 or divisor!=expected or amount!=number(owner.volume)
            or number(ast.get_source_segment(text,tree.right)) in {None,divisor}):
        return None
    selected={sid for item in result.get('items',[]) if 'normative:applicability' in item.get('claim_ids',[])
        for sid in item.get('normative_source_ids',[])}
    sources={s['id']:s for s in result.get('sources',[])}
    for card in result.get('parameter_cards',[]):
        if not card.get('id','').startswith('unit_scale:') or card.get('state')!='confirmed':
            continue
        if (number(card.get('found_value'))!=divisor
                or measure('1',card.get('found_unit',''))!=measure('1',owner.unit)):
            continue
        for sid in card.get('source_ids',[]):
            source=sources.get(sid)
            if (sid not in selected or not source or source['kind']!='normative'
                    or classification_source(source) or source.get('origin')=='uncertain_table'
                    or source.get('origin','').startswith('ocr') and not source.get('numeric_verified')
                    or not unit_scale_quoted(card['found_value'],card['found_unit'],card.get('quote',''),source['text'])):
                continue
            return dict(state='confirmed',role='calculation',label='Приведение к измерителю нормы',
                found_value=str(divisor),found_unit=owner.unit,source_ids=[sid],quote=card['quote'],
                source_anchors=card.get('source_anchors',[]),
                reason=f'Количество работы делится на измеритель {divisor} {owner.unit}, прочитанный из нормы. '
                    'Подтверждён масштаб формулы; количество по ПД, расход материала и применимость нормы проверяются отдельно.')
    return None
