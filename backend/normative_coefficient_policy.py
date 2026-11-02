"""Temporary user policy: accept explicitly named coefficient purposes/values.

This is an engineering assumption requested for the prototype, not a normative
citation. Quantities, dimensions, material properties and contradictions retain
their ordinary checks. Keep the provenance for a future clarification action.
"""
import ast
from copy import deepcopy
import re

TRACE = 'named_coefficient_policy_v1'
TOPIC = 'Назначение коэффициента принято'
PATTERN = re.compile(r'(?P<label>к[\s_.-]*упл\.?|к[\s_.-]*разр\.?|'
    r'коэффициент\s+(?:запаса\s+на\s+)?(?:уплотнения|разрыхления|расхода))'
    r'\s*[:=–—-]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*', re.I)


def named(position):
    from normative_parameters import number
    output=[]
    for line in re.split(r'[\n;]+',getattr(position,'comment','')):
        match=PATTERN.fullmatch(line.strip())
        if not match:
            continue
        value=number(match['value'])
        if value is None or value<=0:
            continue
        label=match['label'].casefold()
        purpose=('коэффициент запаса материала на уплотнение' if 'упл' in label else
            'коэффициент разрыхления' if 'разр' in label else 'коэффициент расхода материала')
        output.append(dict(literal=line.strip(),value=str(value),purpose=purpose))
    return output


def _product(position):
    from normative_parameters import number
    expression=(getattr(position,'calculation',None) or {}).get('expression') or getattr(position,'formula','')
    text=expression.strip().lstrip('=').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval').body
    except (ValueError,SyntaxError):
        return []
    leaves=[]
    def visit(node):
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult):
            return visit(node.left) and visit(node.right)
        if isinstance(node,ast.Constant) and not isinstance(node.value,bool):
            value=number(ast.get_source_segment(text,node))
            if value is not None and value>0:
                leaves.append(value)
                return True
        return False
    return leaves if visit(tree) else []


def accepted_claims(position,result):
    """Only an explicit named multiplier, not arbitrary resource consumption."""
    from normative_parameters import number,measure
    entries=named(position)
    if not entries:
        return set()
    claims=set()
    comment=getattr(position,'comment','').strip()
    if PATTERN.fullmatch(comment):
        claims.update({'basis:comment:consumption:1','attribute:comment:condition','comment'})
    leaves=_product(position)
    quantity=next((c for c in result.get('parameter_cards',[]) if c['id']=='quantity'),{})
    parent=measure('1',quantity.get('unit',''))
    material=measure('1',getattr(position,'unit',''))
    for entry in entries:
        factor=number(entry['value'])
        if leaves.count(factor)!=1 or not parent or not material:
            continue
        # A same-dimension work amount × named factor, or an area × depth ×
        # named factor. Remaining geometry cards still gate the complete row.
        same=parent==material and len(leaves)==2
        volume=(parent[0]=='area' and material[0]=='volume' and len(leaves)==3
            and 'уплотнение' in entry['purpose'])
        if same or volume:
            claims.add('derived:rate')
    return claims


def apply(position,result):
    from normative_parameters import number
    entries=named(position)
    if not entries:
        return result
    output=deepcopy(result)
    originals=[]
    for card in output.get('parameter_cards',[]):
        if (card.get('role')!='coefficient' or card.get('state')!='needs_facts'
                or not card.get('id','').startswith(f'factor:{position.index}:')):
            continue
        entry=next((e for e in entries if number(e['value'])==number(card.get('declared_value'))),None)
        if entry:
            originals.append(deepcopy(card))
            card.update(state='confirmed',reason=entry['purpose'].capitalize()+
                '. Значение временно принято по правилу пользователя; источник значения требует уточнения.')
    accepted=accepted_claims(position,output)
    items=[]
    for item in output.get('items',[]):
        if item.get('topic')==TOPIC:
            if set(item.get('claim_ids',[]))<=accepted:
                items.append(item)
            continue
        affected=set(item.get('claim_ids',[]))&accepted
        if not affected or item.get('state')!='needs_facts':
            items.append(item)
            continue
        remaining=[c for c in item['claim_ids'] if c not in affected]
        originals.append(deepcopy(item))
        if remaining:
            items.append({**item,'claim_ids':remaining})
        items.append(dict(topic=TOPIC,state='confirmed',required=True,claim_ids=sorted(affected),
            project_source_ids=[],normative_source_ids=[],
            explanation='Назначение явно указанного коэффициента распознано. Его значение временно принято '
                'по правилу пользователя. Это допущение не подтверждает геометрию, свойства материала или выбор нормы.'))
    output['items']=items
    output['evidence_trace']=[e for e in output.get('evidence_trace',[]) if e.get('kind')!=TRACE]
    output['evidence_trace'].append(dict(kind=TRACE,policy='temporary_user_acceptance',
        entries=entries,accepted_claims=sorted(accepted),original_checks=originals,
        normative_value_proven=False,clarification_deferred=True))
    return output
