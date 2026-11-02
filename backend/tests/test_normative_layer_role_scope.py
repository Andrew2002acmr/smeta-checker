"""A physical middle layer is not the single-layer branch of a cited clause."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_conditional_selection import (apply_guard, condition_scope_matches, feedback,
    repair_candidates, selection_findings)
from normative_layer_conditions import TOPIC, layer_condition_proof
from normative_local_checks import layer_group
from normative_parameters import enrich


CASES=json.loads((Path(__file__).parent/'fixtures'/'layer-role-v11-live.json').read_text())


def actual(index):
    case=next(c for c in CASES if c['index']==index)
    return VorPosition.model_validate(case['position']),deepcopy(case['review'])


@pytest.mark.parametrize('index',[0,2])
def test_actual_lower_24_uses_lower_branch_and_preserves_confirmed_evidence(index):
    position,result=actual(index)
    original=deepcopy(result)
    proof=layer_condition_proof(position,result,layer_group(position,result['related_positions']))
    assert proof
    assert 'При толщине нижнего слоя' in proof['explanation']
    assert 'При толщине однослойного' not in proof['explanation']
    assert selection_findings(position,result,{index:position},{},cards=result['parameter_cards'])==[]
    checked=enrich(position,result,{index:position},{})
    assert checked['state']=='confirmed'
    assert all(item['state']=='confirmed' for item in checked['items']
        if 'normative:applicability' in item.get('claim_ids',[]))
    assert result==original


def test_actual_middle_20_cannot_be_green_from_threshold_alone():
    position,result=actual(4)
    original=deepcopy(result)
    assert layer_condition_proof(position,result,layer_group(position,result['related_positions'])) is None
    findings=selection_findings(position,result,{4:position},{},cards=result['parameter_cards'])
    assert findings and all(f['reason']=='construction_role_unresolved' for f in findings)
    assert all(f['allowed_base_codes']==[] and f['layer_role']=='средн' for f in findings)
    assert repair_candidates(position,result,{4:position},{})==[]
    assert any('Одного порога толщины' in line for line in feedback(position,result,{4:position},{}))
    checked=enrich(position,result,{4:position},{})
    assert checked['state']=='needs_facts'
    assert not any(i['state']=='possible_mismatch' for i in checked['items'])
    assert all(i['state']=='needs_facts' for i in checked['items']
        if 'normative:applicability' in i.get('claim_ids',[]))
    assert not any(i.get('topic')==TOPIC for i in checked['items'])
    for kind in ('quantity','thickness'):
        before=next(c for c in result['parameter_cards'] if c['id']==kind)
        after=next(c for c in checked['parameter_cards'] if c['id']==kind)
        assert after['state']=='confirmed'
        assert (after['found_value'],after['found_unit'],after['source_ids'])==(before['found_value'],before['found_unit'],before['source_ids'])
    assert checked['sources']==original['sources'] and result==original
    repeated=enrich(position,checked,{4:position},{})
    assert repeated==checked


@pytest.mark.parametrize('cited',[False,True])
def test_single_branch_cannot_close_layer_proof_through_either_source_route(cited):
    from test_normative_referenced_layer_condition import fixture
    position,result,group=fixture()
    result['sources'][1]['text']=result['sources'][1]['text'].replace('нижнего слоя','однослойного')
    if cited:
        result['items'][0]['normative_source_ids'].append('n')
    assert layer_condition_proof(position,result,group) is None


@pytest.mark.parametrize('scope,role,single,expected',[
    ('При толщине нижнего слоя основания','нижн',False,True),
    ('При толщине верхнего слоя основания','верхн',False,True),
    ('При толщине однослойного основания',None,True,True),
    ('При толщине однослойного основания','средн',False,False),
    ('При толщине однослойного основания','нижн',False,False),
    ('При толщине однослойного основания',None,False,False),
    ('При толщине нижнего слоя основания','средн',False,False),
    ('При толщине верхнего слоя основания','нижн',False,False),
    ('При толщине основания',None,False,False),
    ('При толщине однослойного основания нижнего слоя','нижн',True,False),
])
def test_branch_requires_positive_matching_physical_role(scope,role,single,expected):
    assert condition_scope_matches(scope,dict(role=role,single_layer=single)) is expected


@pytest.mark.parametrize('change',['wrong_pdf','unreadable','other_material','unbound_selection'])
def test_negative_scope_guard_does_not_bind_unrelated_clauses(change):
    position,result=actual(4)
    clauses=[s for s in result['sources'] if s.get('kind')=='normative' and 'При толщине однослойного' in s.get('text','')]
    assert clauses
    for source in clauses:
        if change=='wrong_pdf':
            source['source_id']='unrelated-pdf'
        elif change=='unreadable':
            source['origin']='ocr_unreviewed'
        elif change=='other_material':
            source['text']=source['text'].replace('щебеночно-песчаных','асфальтобетонных')
    if change=='unbound_selection':
        for item in result['items']:
            if 'normative:applicability' in item.get('claim_ids',[]):
                item['normative_source_ids']=[]
    assert not any(f['reason']=='construction_role_unresolved' for f in
        selection_findings(position,result,{4:position},{},cards=result['parameter_cards']))


def test_scope_unknown_does_not_erase_an_existing_conflict():
    position,result=actual(4)
    item=next(i for i in result['items'] if 'normative:applicability' in i.get('claim_ids',[]))
    item['state']='possible_mismatch'
    original=deepcopy(item)
    assert apply_guard(position,result,{4:position},{},cards=result['parameter_cards'])
    assert result['state']=='possible_mismatch'
    assert item==original
