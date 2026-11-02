"""A selected, typed resource rate proves its multiplier, not the whole VOR."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import resource_sources
from normative_parameters import build_cards,enrich
from normative_resource_consumption import consumption_proof,EVENT,PROOF


def fixture(rate='1.03',unit='т',base='т',name='Эмульсия битумно-дорожная'):
    norm=dict(id='norm-123',entity_kind='norm',code='27-06-035-01',designation='Pinned norm',
        unit_raw=base,extraction_status='extracted_native_unreviewed',checks=[],rules=[],resources=[dict(
            code='01.2.03.07-0023',name=name,unit=unit,quantity=rate,raw_quantity=rate,
            quantity_kind='numeric',status='extracted_native_unreviewed',
            citations=[dict(source_id='pdf',page=60)])])
    trace=[]
    resource=resource_sources(norm,'n',evidence_trace=trace)
    result=dict(pipeline_version=4,state='needs_facts',sources=[
        dict(id='n',kind='normative',label='Pinned norm · '+norm['code'],origin='native',
            source_id='pdf',page=60,text='Selected scope'),
        dict(id='p',kind='project',label='PD',origin='native',page=1,text='Work'),*resource],
        evidence_trace=trace,parameter_proposals=[],related_positions=[],items=[dict(
            topic='Applicability',claim_ids=['normative:applicability'],state='confirmed',
            project_source_ids=['p'],normative_source_ids=['n'],explanation='Scope established')])
    owner=SimpleNamespace(index=0,number='1',work_name='Подгрунтовочные работы',unit='т',volume='0.067048',
        position_type='Работа',position_source=SimpleNamespace(row=107),formula='',calculation={},comment='')
    work=SimpleNamespace(index=1,number='2',work_name='Битумная эмульсия ЭБДК Б',unit='т',volume='0.34402',
        position_type='Материал',position_source=SimpleNamespace(row=108),formula='0.334*1.03',
        calculation={'expression':'0.334*1.03'},comment='Коэффициент расхода 1,03')
    return work,owner,result


def proof(work,owner,result,value='1.03',expression=None):
    return consumption_proof(work,owner,result,value,expression or work.formula,arithmetic_confirmed=True)


def test_typed_resource_proves_rate_without_confirming_wrong_base_or_mutating_input():
    work,owner,result=fixture()
    original=deepcopy(result)
    output=enrich(work,result,{0:owner,1:work},{'parent':{'index':0}})
    cards={c['id']:c for c in output['parameter_cards']}
    rate=cards['factor:1:1.03']
    assert rate['state']=='confirmed' and rate['label']=='Нормативный расход'
    assert rate['role']=='coefficient' and rate['found_unit']=='т/т'
    assert cards['factor:1:0.334']['state']=='needs_facts'
    assert cards['quantity']['declared_value']=='0.067048' and cards['quantity']['state']=='needs_facts'
    assert result==original and output['state']=='needs_facts'
    applied=[e for e in output['evidence_trace'] if e['kind']==PROOF]
    assert len(applied)==1 and applied[0]['resource_code']=='01.2.03.07-0023'
    assert len([e for e in enrich(work,output,{0:owner,1:work},{'parent':{'index':0}})['evidence_trace']
                if e['kind']==PROOF])==1


def test_resource_metadata_does_not_change_strict_public_source_shape():
    _,_,result=fixture()
    from workspace_models import NormativeCheckSource
    for source in result['sources']:
        NormativeCheckSource.model_validate(source)
    assert result['evidence_trace'][0]['kind']==EVENT
    assert result['evidence_trace'][0]['resources'][0]['quantity_kind']=='numeric'


def test_general_rate_uses_measure_and_units_not_one_hardcoded_factor():
    work,owner,result=fixture('20','кг','100 кг','Бетон тяжелый')
    owner.unit=work.unit='кг'
    work.work_name='Бетон тяжелый'
    work.comment=''
    work.formula='10*0.2'
    selected=proof(work,owner,result,'0.2')
    assert selected and selected['proof']['base_quantity']=='100'
    assert selected['proof']['quantity']=='20' and selected['card']['found_value']=='0.2'


@pytest.mark.parametrize('change',[
    lambda w,o,r:r['items'][0].update(state='needs_facts'),
    lambda w,o,r:r['items'][0].update(normative_source_ids=[]),
    lambda w,o,r:r['sources'][0].update(label='Other norm · 27-06-036-01'),
    lambda w,o,r:r['sources'][0].update(source_id='another-edition'),
    lambda w,o,r:r['sources'][-1].update(origin='ocr_unreviewed'),
    lambda w,o,r:r['sources'][-1].update(text='Invented resource text'),
    lambda w,o,r:r['evidence_trace'][0].update(norm_measure='100 м2'),
    lambda w,o,r:r['evidence_trace'][0].update(checks=[{'state':'unknown'}]),
    lambda w,o,r:r['evidence_trace'][0].update(rules=[{'state':'needs_facts'}]),
    lambda w,o,r:r['evidence_trace'][0]['resources'][0].update(quantity_kind='project',raw_quantity='П'),
    lambda w,o,r:r['evidence_trace'][0]['resources'][0].update(quantity='0',raw_quantity='0'),
    lambda w,o,r:r['evidence_trace'][0]['resources'][0].update(status='ocr_unreviewed'),
    lambda w,o,r:setattr(w,'work_name','Бетон тяжелый'),
    lambda w,o,r:setattr(o,'unit','л'),
    lambda w,o,r:setattr(w,'comment','Купл-1,03'),
])
def test_wrong_scope_missing_conditions_unverified_text_and_density_do_not_confirm(change):
    work,owner,result=fixture()
    change(work,owner,result)
    assert proof(work,owner,result) is None


@pytest.mark.parametrize('expression',['0.334/1.03','0.334+1.03','0.334*1.03*1.03','ROUND(0.334*1.03,3)'])
def test_rate_must_be_one_multiplier_of_independently_checked_formula(expression):
    work,owner,result=fixture()
    assert proof(work,owner,result,expression=expression) is None


@pytest.mark.parametrize('alternative_rate',['1.07','П'])
def test_numerical_match_cannot_select_between_two_compatible_resource_rows(alternative_rate):
    work,owner,result=fixture()
    event=result['evidence_trace'][0]
    alternative={**event['resources'][0],'resource_code':'01.2.03.07-9999',
        'quantity':None if alternative_rate=='П' else alternative_rate,'raw_quantity':alternative_rate,
        'quantity_kind':'project' if alternative_rate=='П' else 'numeric',
        'quote':'01.2.03.07-9999 Эмульсия битумно-дорожная; т: '+alternative_rate}
    event['resources'].append(alternative)
    result['sources'][-1]['text']+='\n'+alternative['quote']
    assert proof(work,owner,result) is None


def test_named_material_grade_cannot_be_replaced_by_same_family():
    work,owner,result=fixture(name='Битумная эмульсия ЭБДК А')
    assert proof(work,owner,result) is None


def test_unlinked_material_and_unchecked_arithmetic_cannot_use_resource_proof():
    work,owner,result=fixture()
    assert proof(work,work,result) is None
    assert consumption_proof(work,owner,result,'1.03',work.formula,arithmetic_confirmed=False) is None
    assert all(c['state']!='confirmed' for c in build_cards(work,result,{1:work},{}) if c['id']=='factor:1:1.03')
