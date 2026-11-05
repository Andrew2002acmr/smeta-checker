"""A proved area input must also discharge its identical formula operand."""
from copy import deepcopy

import pytest

from audit_models import VorPosition
from normative_parameters import build_cards
from normative_workbook_rate import annotate


def fixture():
    work=VorPosition(index=6,work_name='Земля растительная для газонов',unit='м3',volume='64',
        position_type='Материал',comment='Расход 20 м3/100м2',formula='=320*20/100')
    sources=[dict(id='area',kind='project',label='ПД',version_id='pd',page=4,origin='native',
        text='Газоны. Площадь 320 м2'),dict(id='volume',kind='project',label='ПД',version_id='pd',page=4,
        origin='native',text='Газоны. Объем растительного слоя 64 м3')]
    proposals=[dict(parameter='quantity',value=value,unit=unit,source_id=sid,
        quote=text,scope_quote=text,scope_state='verified',operation='direct',operand_source_ids=[])
        for sid,value,unit,text in [('area','320','м2',sources[0]['text']),('volume','64','м3',sources[1]['text'])]]
    result=dict(pipeline_version=20,sources=sources,parameter_proposals=proposals,
        items=[dict(claim_ids=['input:area'],state='confirmed',project_source_ids=['area']),
            dict(claim_ids=['quantity'],state='confirmed',project_source_ids=['volume'])])
    result['parameter_cards']=build_cards(work,result,{6:work},{})
    return work,result


def test_independent_area_and_output_supply_a_geometric_rate_not_norm_applicability():
    work,result=fixture()
    before=deepcopy(result)
    annotate(work,result,{6:work})
    card=next(c for c in result['parameter_cards'] if c['id']=='factor:6:320')
    assert card['state']=='confirmed' and card['source_ids']==['area']
    assert card['found_value']=='320' and card['found_unit']=='м2'
    rate=next(c for c in result['parameter_cards'] if c['id']=='factor:6:20')
    assert rate['state']=='confirmed' and set(rate['source_ids'])=={'area','volume'}
    assert result['evidence_trace'][0]['geometric_rate_basis']['norm_applicability_verified'] is False
    assert result['sources']==before['sources'] and result['parameter_proposals']==before['parameter_proposals']


@pytest.mark.parametrize('change',['wrong_value','unresolved_scope','fabricated_quote','unverified_ocr','wrong_unit'])
def test_stale_green_input_area_does_not_bypass_literal_revalidation(change):
    work,result=fixture()
    assert next(c for c in result['parameter_cards'] if c['id']=='input:area')['state']=='confirmed'
    proposal=result['parameter_proposals'][0]
    if change=='wrong_value':
        proposal['value']='321'
    elif change=='unresolved_scope':
        proposal['scope_state']='unresolved'
    elif change=='fabricated_quote':
        result['sources'][0]['text']='Газоны. Площадь 999 м2'
    elif change=='unverified_ocr':
        result['sources'][0].update(origin='ocr',numeric_verified=False)
    else:
        proposal['unit']='м3'
    annotate(work,result,{6:work})
    assert next(c for c in result['parameter_cards'] if c['id']=='factor:6:320')['state']=='needs_facts'


@pytest.mark.parametrize('change',['valid','no_output','wrong_area','other_pdf','unresolved_scope',
    'unverified_ocr','fabricated_quote','wrong_output_unit','wrong_formula'])
def test_geometric_rate_rebuilds_both_independent_physical_facts(change):
    from normative_workbook_rate import project_rate_proof
    work,result=fixture()
    if change=='no_output':
        result['parameter_proposals']=result['parameter_proposals'][:1]
    elif change=='wrong_area':
        result['parameter_proposals'][0]['value']='321'
    elif change=='other_pdf':
        result['sources'][1]['version_id']='another'
    elif change=='unresolved_scope':
        result['parameter_proposals'][1]['scope_state']='unresolved'
    elif change=='unverified_ocr':
        result['sources'][1].update(origin='ocr',numeric_verified=False)
    elif change=='fabricated_quote':
        result['sources'][1]['text']='Газоны. Объем 99 м3'
    elif change=='wrong_output_unit':
        result['parameter_proposals'][1]['unit']='т'
    elif change=='wrong_formula':
        work.formula='=320*21/100'
    proof=project_rate_proof(work,result)
    assert bool(proof)==(change=='valid')


def test_pd_only_rate_item_requires_both_rebuilt_source_roots_and_not_a_normative_claim():
    from normative_workbook_rate import project_consumption_supported
    from normative_verdict import source_supported
    work,result=fixture()
    item=dict(state='confirmed',claim_ids=['basis:comment:consumption:1'],
        project_source_ids=['area','volume'],normative_source_ids=[])
    args=dict(position=work,parameters=result['parameter_proposals'],items=result['items'])
    assert source_supported(item) and project_consumption_supported(item,result['sources'],**args)
    item['project_source_ids']=['area']
    assert not project_consumption_supported(item,result['sources'],**args)
    item['claim_ids']=['normative:applicability']
    assert not source_supported(item)


def test_norm_measure_uses_typed_area_input_without_certifying_application():
    work,result=fixture()
    result['sources'].append(dict(id='norm',kind='normative',origin='native',
        label='ГЭСН 47 · 47-01-046-03',text='Измеритель: 100 м2'))
    result['parameter_proposals'].append(dict(parameter='unit_scale',source_id='norm',value='100',unit='м2',
        quote='Измеритель: 100 м2',scope_state='verified',scope_quote='Измеритель: 100 м2'))
    result['items'].append(dict(claim_ids=['normative:applicability'],state='needs_facts',
        project_source_ids=['area','volume'],normative_source_ids=['norm']))
    cards=build_cards(work,result,{work.index:work},{})
    scale=next(c for c in cards if c['id']=='unit_scale:norm')
    assert scale['state']=='confirmed' and scale['quantity_role']=='calculation_input'
    assert scale['unit']=='м2' and scale['declared_value']=='320'
    assert result['items'][-1]['state']=='needs_facts'


def test_project_only_rate_survives_item_validation_only_with_rebuilt_evidence():
    from normative_position_check import AgentAnswer,validate_items
    work,result=fixture()
    item=dict(topic='Расход',state='confirmed',claim_ids=['basis:comment:consumption:1'],
        explanation='Геометрический расход текущего слоя.',project_source_ids=['area','volume'],normative_source_ids=[])
    answer=AgentAnswer(summary='Геометрия',items=[item])
    checked=validate_items(answer.items,result['sources'],position=work,parameters=result['parameter_proposals'])
    # Both selected facts must themselves remain scoped to the current subject.
    assert checked[0]['state']=='confirmed'
    checked=validate_items(answer.items,result['sources'],position=work,parameters=[])
    assert checked[0]['state']=='needs_facts'
