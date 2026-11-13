import json
from copy import deepcopy
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_construction_scope import repair_thickness
from normative_parameters import build_cards


def fixture():
    data=json.loads((Path(__file__).parent/'fixtures/strengthening-scope22.json').read_text(encoding='utf-8'))
    return VorPosition.model_validate(data['position']),data['result']


def test_actual_street_work_uses_explicit_lower_strengthening_layer_not_widening_middle_layer():
    position,result=fixture()
    original=deepcopy(result)
    before=next(c for c in build_cards(position,result,{position.index:position},{}) if c['id']=='thickness')
    assert before['state']=='needs_facts'
    assert repair_thickness(position,result)
    proposal=next(p for p in result['parameter_proposals'] if p['parameter']=='thickness')
    assert proposal['source_id']=='agent:p49:0' and proposal['value']=='7'
    assert 'Нижний слой покрытия (слой усиления)' in proposal['scope_quote']
    after=next(c for c in build_cards(position,result,{position.index:position},{}) if c['id']=='thickness')
    assert after['state']=='confirmed'
    assert result['sources']==original['sources']
    assert result['items']==original['items']
    assert result['state']==original['state']
    assert next(c for c in build_cards(position,result,{position.index:position},{}) if c['id']=='quantity')['state']=='needs_facts'
    once=deepcopy(result)
    assert not repair_thickness(position,result)
    assert result==once


@pytest.mark.parametrize('change',['different_pdf','duplicate_construction','wrong_material',
    'wrong_height','minimum_height','wrong_work_height','no_purpose','no_street','other_vor_section','basis_work','unverified_ocr'])
def test_ambiguous_or_incompatible_construction_does_not_gain_scope(change):
    position,result=fixture()
    target=next(s for s in result['sources'] if s['id']=='agent:p49:0')
    if change=='different_pdf':
        target['version_id']='other'
    elif change=='duplicate_construction':
        result['sources'].append({**deepcopy(target),'id':'another-construction','page':99})
    elif change=='wrong_material':
        target['text']=target['text'].replace('А22Нт','А22Нл')
    elif change=='wrong_height':
        target['text']=target['text'].replace('h=7 см','h=9 см')
    elif change=='minimum_height':
        target['text']=target['text'].replace('h=7 см','hmin=7 см')
    elif change=='wrong_work_height':
        position.work_name=position.work_name.replace('7 см','9 см')
    elif change=='no_purpose':
        target['text']=target['text'].replace('(слой усиления)','')
    elif change=='no_street':
        position.position_source.sections=[]
    elif change=='other_vor_section':
        position.work_name+=' для ровиков уширения'
    elif change=='basis_work':
        position.work_name='Устройство нижнего слоя основания из ЩПС h=7 см'
    else:
        target['origin']='ocr_unverified'
    original=deepcopy(result)
    assert not repair_thickness(position,result)
    assert result==original


def test_repeated_same_page_excerpt_is_not_an_independent_conflicting_construction():
    position,result=fixture()
    target=next(s for s in result['sources'] if s['id']=='agent:p49:0')
    result['sources'].append({**deepcopy(target),'id':'same-page-excerpt'})
    assert repair_thickness(position,result)
