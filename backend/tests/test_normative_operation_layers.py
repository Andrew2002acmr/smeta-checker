import json
from copy import deepcopy
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_operation_layers import apply_guard, findings


def fixture():
    data=json.loads((Path(__file__).parent/'fixtures/operation-layer10.json').read_text(encoding='utf-8'))
    return VorPosition.model_validate(data['position']), data['result']


def test_actual_single10cm_operation_cannot_keep_confirmed_applicability():
    position,result=fixture()
    original=deepcopy(result)
    assert findings(position,result,{}, {},result['parameter_cards'])
    assert apply_guard(position,result,{}, {},result['parameter_cards'])
    assert not any(i['state']=='confirmed' and 'normative:applicability' in i.get('claim_ids',[]) for i in result['items'])
    assert result['parameter_cards']==original['parameter_cards']
    assert result['parameter_proposals']==original['parameter_proposals']
    assert result['sources']==original['sources']
    assert result['state']=='needs_facts'
    once=deepcopy(result)
    apply_guard(position,result,{}, {},result['parameter_cards'])
    assert result==once


@pytest.mark.parametrize('height,blocked',[('8',False),('8.01',True),('5',False)])
def test_boundary_is_per_declared_operation_not_combined_project_layer(height,blocked):
    position,result=fixture()
    position.work_name=position.work_name.replace('10 см',height+' см')
    result['related_positions']=[dict(work='Другая операция h=5 см',quantity=position.volume,unit=position.unit)]
    assert bool(findings(position,result,{}, {},result['parameter_cards']))==blocked


@pytest.mark.parametrize('mutation',['pdf','family','truncated','ocr','ambiguous_height','negated'])
def test_condition_must_be_complete_and_bound_to_selected_source(mutation):
    position,result=fixture()
    clause=next(s for s in result['sources'] if '1.27.37' in s.get('text',''))
    if mutation=='pdf':
        clause['source_id']='other-pdf'
    elif mutation=='family':
        clause['label']='ФЕР27'
    elif mutation=='truncated':
        clause['text']=clause['text'].split('с корректировкой')[0]
    elif mutation=='ocr':
        clause['origin']='ocr_unverified'
    elif mutation=='negated':
        clause['text']=clause['text'].replace('Нормами табл.', 'Не нормами табл.')
    else:
        position.work_name+='; соседний слой h=5 см'
    assert not findings(position,result,{}, {},result['parameter_cards'])
