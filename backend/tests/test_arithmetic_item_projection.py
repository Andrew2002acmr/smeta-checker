from copy import deepcopy

import pytest

from normative_verdict import readable_review
from test_normative_workbook_arithmetic import fixture, checked


def test_verified_formula_is_displayed_confirmed_without_closing_other_obligations():
    position, result = fixture()
    review = checked(position, result)
    review['items'].append(dict(state='needs_facts', claim_ids=['basis:consumption'],
        explanation='Плотность не подтверждена.', required=True))
    original = deepcopy(review)
    projected = readable_review(review)
    assert any(i['claim_ids']==['workbook:formula'] and i['state']=='confirmed' for i in projected['items'])
    assert projected['items'][-1]['state']=='needs_facts'
    assert projected['state']=='needs_facts'
    assert projected['sources']==review['sources']
    assert readable_review(projected)==projected
    assert review==original


@pytest.mark.parametrize('mutation', ['proof', 'card', 'mixed', 'wrong_formula'])
def test_projection_requires_independent_arithmetic_proof_and_exact_claim_scope(mutation):
    position, result = fixture()
    review = checked(position, result)
    if mutation=='proof':
        review['evidence_trace']=[]
    elif mutation=='card':
        next(c for c in review['parameter_cards'] if c['id']=='calculation:94')['state']='needs_facts'
    elif mutation=='mixed':
        next(i for i in review['items'] if i['claim_ids']==['workbook:formula'])['claim_ids'].append('quantity')
    else:
        next(e for e in review['evidence_trace'] if e['kind']=='workbook_arithmetic_proof_v1')['expression']='108+54'
    projected=readable_review(review)
    assert not any(e.get('kind')=='arithmetic_item_projection_v1' for e in projected.get('evidence_trace',[]))
