"""Prove contradictions between a row's real and displayed workbook arithmetic.

This is a narrow, local check of current XLSX inputs. It cannot choose the right
quantity, interpret a bare factor as a rate, or close any engineering claim.
"""
import ast
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any

from audit_models import VorPosition

PROOF = 'workbook_display_conflict_v1'
ADDRESS = re.compile(r'[A-Z]{1,3}([1-9]\d{0,6})')


def _expression(text: Any) -> tuple[Decimal,tuple[Any,...]] | None:
    if not isinstance(text,str) or len(text)>250:
        return None
    source=text.strip().removeprefix('=').replace(',','.').replace('×','*')
    if not source or not re.fullmatch(r'[0-9.\s+*/()\-]+',source):
        return None
    try:
        tree=ast.parse(source,mode='eval')
        if len(list(ast.walk(tree)))>80:
            return None
        def read(node: ast.AST) -> tuple[Decimal,tuple[Any,...]]:
            if isinstance(node,ast.Constant) and type(node.value) in (int,float):
                literal=ast.get_source_segment(source,node)
                if not literal or not re.fullmatch(r'\d+(?:\.\d+)?',literal):
                    raise ValueError('Unsupported numeric syntax')
                value=Decimal(literal)
                if len(literal)>40 or abs(value)>Decimal('1e18'):
                    raise ValueError('Numeric operand outside bounded arithmetic')
                return value,('number',value)
            if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
                value,shape=read(node.operand)
                return (-value if isinstance(node.op,ast.USub) else value),(type(node.op).__name__,shape)
            if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
                left,left_shape=read(node.left)
                right,right_shape=read(node.right)
                if isinstance(node.op,ast.Add):
                    value=left+right
                elif isinstance(node.op,ast.Sub):
                    value=left-right
                elif isinstance(node.op,ast.Mult):
                    value=left*right
                else:
                    value=left/right
                return value,(type(node.op).__name__,left_shape,right_shape)
            raise ValueError('Unsupported workbook expression')
        value,shape=read(tree.body)
        if not value.is_finite() or abs(value)>Decimal('1e18'):
            return None
        return value,shape
    except (SyntaxError,ValueError,InvalidOperation,ZeroDivisionError,OverflowError):
        return None


def _rounding_only(actual: tuple[Any,...],display: tuple[Any,...]) -> bool:
    """Equivalent syntax may round operand spelling; never infer factor roles."""
    from normative_parameters import same_numeric_result
    if actual[0]!=display[0] or len(actual)!=len(display):
        return False
    if actual[0]=='number':
        a,b=actual[1],display[1]
        if same_numeric_result(a,b):
            return True
        # Only a less precise decimal display can be a rounded spelling.
        if b.as_tuple().exponent>=0 or b.as_tuple().exponent<=a.as_tuple().exponent:
            return False
        return a.quantize(Decimal(1).scaleb(b.as_tuple().exponent),rounding=ROUND_HALF_UP)==b
    return all(_rounding_only(a,b) for a,b in zip(actual[1:],display[1:],strict=True))


def conflicts(position: VorPosition | None,position_key: str) -> list[dict[str,Any]]:
    """Recompute a typed display conflict from the same current source row."""
    from audit_models import position_source_dict
    from audit_measurements import close_amounts
    from human_basis import position_key as current_key
    from normative_parameters import number,same_numeric_result
    from vor_consistency import check_display_consistency
    if not isinstance(position,VorPosition) or current_key(position)!=position_key:
        return []
    source=position_source_dict(position) or {}
    cells=source.get('cells') or {}
    calc=position.calculation or {}
    refs=position.formula_refs or {}
    actual_cell,display_cell=calc.get('cell'),refs.get('display_cell')
    actual_address=ADDRESS.fullmatch(actual_cell) if isinstance(actual_cell,str) else None
    display_address=ADDRESS.fullmatch(display_cell) if isinstance(display_cell,str) else None
    row=source.get('row')
    document_sha256=source.get('document_sha256')
    if (source.get('format')!='xlsx' or not source.get('sheet') or type(row) is not int or row<1
            or not isinstance(document_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',document_sha256)
            or not actual_address or not display_address or actual_cell==display_cell
            or int(actual_address[1])!=row or int(display_address[1])!=row
            or actual_cell not in cells or display_cell not in cells
            or str(cells[display_cell])!=position.formula
            or not same_numeric_result(cells[actual_cell],position.volume)
            or calc.get('status')!='calculated' or calc.get('cached_mismatch')):
        return []
    actual,display=_expression(calc.get('expression')),_expression(position.formula)
    declared=number(position.volume)
    if (not actual or not display or declared is None
            # A bare displayed number can be a rounded result or an operand,
            # not an independently stated quantity calculation.
            or display[1][0] not in {'Add','Sub','Mult','Div'}
            or not same_numeric_result(actual[0],declared)
            or not same_numeric_result(actual[0],calc.get('value'))
            or (calc.get('cached_value') is not None and not same_numeric_result(actual[0],calc['cached_value']))
            or close_amounts(actual[0],display[0])):
        return []
    try:
        if _rounding_only(actual[1],display[1]):
            return []
    except (InvalidOperation,OverflowError):
        return []  # Uncertain arithmetic cannot become a proved conflict.
    # The physical E text above is authoritative, not an older display cache.
    notes=check_display_consistency(calc,position.formula,{'display_cell':display_cell})
    if not any(note.get('kind')=='display-operand-mismatch' and note.get('display_cell')==display_cell for note in notes):
        return []
    inputs=dict(position_key=position_key,document_sha256=source['document_sha256'],sheet=source['sheet'],row=row,
        actual_cell=actual_cell,display_cell=display_cell,actual_expression=calc['expression'],
        display_expression=position.formula,declared_value=position.volume,
        actual_value=format(actual[0],'f'),display_value=format(display[0],'f'),unit=position.unit)
    def differences(left, right):
        if left[0] == right[0] == 'number':
            return [(str(left[1]), str(right[1]))] if left[1] != right[1] else []
        if left[0] == right[0] and len(left) == len(right):
            return [pair for a, b in zip(left[1:], right[1:], strict=True) for pair in differences(a, b)]
        return []
    inputs['different_operands'] = differences(actual[1], display[1])
    binding=hashlib.sha256(json.dumps(inputs,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    message=(f'В самой ВОР расходятся формула количества {actual_cell} и поясняющий расчёт {display_cell}: '
        f"{inputs['actual_value']} и {inputs['display_value']} {position.unit}. "
        'Нужно согласовать исходные данные этих ячеек. Правильное количество по ПД этим сравнением не определяется.')
    if inputs['different_operands']:
        detail = '; '.join(f'{a} в {actual_cell}, {b} в {display_cell}' for a, b in inputs['different_operands'])
        message = f'Разные исходные числа в расчёте ВОР: {detail}. ' + message
    from normative_workbook_rate import analyze
    rate = analyze(position)
    if rate:
        message += f" Расход {rate['rate']} {rate['rate_unit']} указан в примечании; замечание относится к различным исходным числам, а не к наличию расхода или делителя 1000."
    return [dict(kind=PROOF,claim_id=f'workbook:{display_cell}:display-operand-mismatch',
        input_sha256=binding,inputs=inputs,message=message)]
