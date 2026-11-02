"""Independent workbook arithmetic proof; no semantic or normative conclusions."""
from copy import deepcopy

PROOF='workbook_arithmetic_proof_v1'


def _matches(proof):
    from normative_parameters import number,same_numeric_result
    from vor_formula import calculate_cell
    expression=proof.get('expression')
    dependencies=proof.get('dependencies')
    address=proof.get('cell')
    if (not isinstance(expression,str) or not expression.strip() or not isinstance(dependencies,list)
            or not isinstance(address,str) or not address or proof.get('cached_mismatch')
            or number(proof.get('declared_value')) is None):
        return False
    cells={}
    for dependency in dependencies:
        if (not isinstance(dependency,dict) or not isinstance(dependency.get('cell'),str)
                or not dependency['cell'] or dependency['cell'] in cells
                or dependency['cell']==address or number(dependency.get('value')) is None):
            return False
        cells[dependency['cell']]=dict(plain=number(dependency['value']),cached=None)
    cells[address]=dict(formula='='+expression.lstrip('='),cached=None)
    actual=calculate_cell(address,cells)
    return actual['status']=='calculated' and same_numeric_result(actual.get('value'),proof['declared_value'])


def record_arithmetic(position,result):
    """Only the current VOR row may supply its workbook:formula obligation."""
    result['evidence_trace']=[e for e in result.get('evidence_trace',[]) if e.get('kind')!=PROOF]
    if result.get('pipeline_version',0)<3 or result.get('execution_state')=='technical_failure':
        return
    calc=position.calculation or {}
    expression=calc.get('expression') or position.formula
    proof=dict(kind=PROOF,position_index=position.index,card_id=f'calculation:{position.index}',
        expression=expression,declared_value=position.volume,unit=position.unit,
        cell=calc.get('cell') or 'XFD1048576',cached_mismatch=bool(calc.get('cached_mismatch')),
        # Literal operands are parsed again from the expression itself; only
        # actual workbook cell dependencies are external inputs to arithmetic.
        dependencies=[dict(cell=d.get('cell'),value=d.get('value')) for d in calc.get('dependencies') or []
            if d.get('cell') or d.get('origin')!='formula_literal'])
    if _matches(proof):
        result['evidence_trace'].append(deepcopy(proof))


def arithmetic_claims(result):
    """Recalculate saved evidence too; a confirmed card flag is insufficient."""
    if result.get('pipeline_version',0)<3 or result.get('execution_state')=='technical_failure':
        return set()
    proofs=[e for e in result.get('evidence_trace',[]) if e.get('kind')==PROOF]
    if len(proofs)!=1:
        return set()
    proof=proofs[0]
    index=proof.get('position_index')
    if type(index) is not int or index<0 or proof.get('card_id')!=f'calculation:{index}':
        return set()
    cards=[c for c in result.get('parameter_cards',[]) if c.get('id')==proof['card_id']]
    if len(cards)!=1:
        return set()
    card=cards[0]
    if (card.get('state')!='confirmed' or card.get('role')!='calculation'
            or card.get('expression')!=proof.get('expression')
            or card.get('declared_value')!=proof.get('declared_value') or card.get('unit')!=proof.get('unit')
            or not _matches(proof)):
        return set()
    return {'workbook:formula'}
