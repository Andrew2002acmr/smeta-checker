"""A proved aggregate does not require a second proof of arithmetic grouping."""
import ast
from copy import deepcopy


def addition_basis(work, owner, expression, quantity_card, *, arithmetic_confirmed):
    from normative_parameters import number,same_numeric_result
    if (work.index != owner.index or str(getattr(work,'position_type','')).casefold() != 'работа'
            or not arithmetic_confirmed or quantity_card.get('state') != 'confirmed'
            or not quantity_card.get('source_ids') or not quantity_card.get('source_anchors')):
        return None
    text=expression.strip().lstrip('=').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval')
    except (SyntaxError,ValueError):
        return None
    values=[]
    def visit(node):
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
            return visit(node.left) and visit(node.right)
        if isinstance(node,ast.Constant) and not isinstance(node.value,bool):
            value=number(ast.get_source_segment(text,node))
            if value is not None and value>0:
                values.append(value)
                return True
        return False
    if (not visit(tree.body) or not 2<=len(values)<=12
            or not same_numeric_result(sum(values),work.volume)):
        return None
    return dict(role='calculation',state='confirmed',
        reason=f'Слагаемое арифметической записи: итог {sum(values)} {work.unit} подтверждён по ПД. '
            'В формуле нет коэффициентов и пересчёта единиц. Отдельные размеры участков этим не подтверждаются.',
        source_ids=list(quantity_card['source_ids']),
        source_anchors=deepcopy(quantity_card['source_anchors']),quote=quantity_card.get('quote',''))
