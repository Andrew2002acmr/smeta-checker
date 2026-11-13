"""A rounded workbook input is distinct from a normative factor or PD quantity.

Only a linked material's two-term product is supported: rounded parent amount
times an independently proved resource rate. Physical parent quantity stays open.
"""
import ast
from decimal import ROUND_HALF_UP, localcontext
import re


def _binding(position):
    source=getattr(position,'position_source',None)
    if (not source or getattr(source,'format',None)!='xlsx'
            or not re.fullmatch(r'[0-9a-f]{64}',getattr(source,'document_sha256',''))
            or not getattr(source,'sheet','') or type(getattr(source,'row',None)) is not int):
        return None
    return dict(document_sha256=source.document_sha256,sheet=source.sheet,row=source.row,
        position_index=position.index,quantity=position.volume,unit=position.unit)


def parent_operand(work,owner,result,value,expression,relation,*,arithmetic_confirmed):
    from normative_parameters import number,measure
    from normative_resource_consumption import consumption_proof
    linked=((relation.get('parent') or {}).get('index')==owner.index
        or any(c.get('material_index')==work.index for c in relation.get('children',[])))
    if (not linked or work.index==owner.index or not arithmetic_confirmed
            or str(getattr(work,'position_type','')).casefold()!='материал'
            or str(getattr(owner,'position_type','')).casefold()!='работа'
            or measure('1',work.unit)!=measure('1',owner.unit)):
        return None
    child,parent=_binding(work),_binding(owner)
    if (not child or not parent or child['row']==parent['row']
            or any(child[k]!=parent[k] for k in ('document_sha256','sheet'))):
        return None
    text=expression.strip().lstrip('=').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval').body
    except (ValueError,SyntaxError):
        return None
    if not isinstance(tree,ast.BinOp) or not isinstance(tree.op,ast.Mult):
        return None
    parts=[ast.get_source_segment(text,n) if isinstance(n,ast.Constant) and not isinstance(n.value,bool) else ''
        for n in (tree.left,tree.right)]
    if any(not p or not re.fullmatch(r'\d+(?:\.\d+)?',p) for p in parts):
        return None
    amounts=[number(p) for p in parts]
    amount=number(value)
    if amount is None or amount<=0 or amounts.count(amount)!=1:
        return None
    own=number(owner.volume)
    if own is None or own<=0 or own==amount:
        return None
    literal=parts[amounts.index(amount)]
    precision=number(literal)
    if precision is None or int(precision.as_tuple().exponent)>=0:
        return None  # Whole-number coincidence is not adequate rounding provenance.
    with localcontext() as context:
        context.prec=80
        rounded=own.quantize(precision,rounding=ROUND_HALF_UP)
    if rounded!=amount:
        return None
    rate=parts[1-amounts.index(amount)]
    proof=consumption_proof(work,owner,result,rate,expression,arithmetic_confirmed=True)
    if proof is None:
        return None
    return dict(card=dict(role='calculation',label='Округлённое количество родительской работы в формуле',
        state='confirmed',found_value=literal,found_unit=owner.unit,
        reason=f'Число {literal} совпадает с количеством родительской работы {owner.volume} {owner.unit}, '
            'округлённым до точности записи формулы. Второй множитель подтверждён как нормативный расход. '
            'Подтверждена связь чисел ВОР; физический объём родительской работы по ПД проверяется отдельно.'),
        proof=dict(kind='rounded_parent_workbook_operand_v1',position_index=work.index,parent_index=owner.index,
            parent=parent,child=child,expression=expression,literal=literal,rounded_parent=str(rounded),
            rounding='ROUND_HALF_UP',decimal_places=-int(precision.as_tuple().exponent),rate_proof=proof['proof']))
