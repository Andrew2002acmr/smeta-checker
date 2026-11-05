"""A parent may have proved geometry while its technology still needs review."""
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch

import pytest

from audit_models import VorPosition,PositionSource
from normative_parameters import enrich
from normative_parent_basis import bind_parent_basis,GEOMETRY_TRACE
from normative_verdict import publish


def fixture():
    parent=VorPosition(index=0,number='1',volume='10',unit='м2',position_type='Работа',
        work_name='Устройство нижнего слоя основания, h=10 см',
        position_source=PositionSource(document_sha256='vor',format='xlsx',sheet='S',row=10,
            header_row=1,position_id='0'))
    child=parent.model_copy(update={'index':1,'number':'2','volume':'2','unit':'т',
        'work_name':'Щебёночно-песчаная смесь','position_type':'Материал'})
    scope='Нижний слой основания: площадь 10 м2, h=10 см.'
    source=dict(id='p',kind='project',origin='native',version_id='pdf',page=1,label='ПД',
        text=scope,rects=[[.1,.1,.5,.2]])
    geometry=dict(topic='Геометрия',state='confirmed',required=True,
        claim_ids=['quantity','geometry:thickness'],project_source_ids=['p'],normative_source_ids=[],explanation='Параметры ПД')
    technology=dict(topic='Технология',state='needs_facts',required=True,
        claim_ids=['normative:applicability'],project_source_ids=['p'],normative_source_ids=[],explanation='Технология ещё не проверена')
    basis=dict(pipeline_version=4,execution_state='complete',state='needs_facts',summary='Технология требует проверки',
        sources=[source],items=[geometry,technology],related_positions=[],parameter_proposals=[
            dict(parameter='quantity',operation='direct',source_id='p',value='10',unit='м2',
                quote='площадь 10 м2',scope_quote=scope,scope_state='verified'),
            dict(parameter='thickness',operation='direct',source_id='p',value='10',unit='см',
                quote='h=10 см',scope_quote=scope,scope_state='verified')])
    positions={0:parent,1:child}
    basis=enrich(parent,basis,positions,{})
    donor=dict(id='donor',kind='check',payload=dict(vor_version_id='vor',position_key='key-0',
        kb={'release':'kb'},position_check=basis,work_relations={}))
    record=dict(id='run',vor_version_id='vor',config={'pdf_versions':{'PD':'pdf'}},
        indexed_results=[dict(position_index=0,result=dict(status='needs_review',
            checks=[dict(claim_id='quantity',state='not_found',required=True)]))])
    own=dict(id='own',kind='normative',origin='native',page=1,label='Коэффициент',text='Коэффициент 1,26')
    result=dict(pipeline_version=4,execution_state='complete',state='needs_facts',summary='Нужно основание',
        sources=[own],related_positions=[],parameter_proposals=[dict(parameter='coefficient',operation='direct',
            source_id='own',value='1.26',unit='',quote='Коэффициент 1,26',scope_state='unresolved')],
        items=[dict(topic='Родитель',state='needs_facts',required=True,claim_ids=['derived:parent','quantity'],
            project_source_ids=[],normative_source_ids=[],explanation='Нужно основание работы'),
            dict(topic='Материал',state='needs_facts',required=True,claim_ids=['derived:rate','normative:applicability'],
            project_source_ids=[],normative_source_ids=['own'],explanation='Нужно проверить расход и применимость')])
    repository=Mock()
    repository.normative_reviews.return_value=[donor]
    context=SimpleNamespace(repository=repository)
    return donor,record,result,positions,context


def test_same_physical_parent_read_is_replaced_once_and_can_be_revalidated():
    donor,record,result,positions,context=fixture()
    basis=donor['payload']['position_check']
    result['sources'].append(deepcopy(basis['sources'][0]))
    result['parameter_proposals'].append(deepcopy(basis['parameter_proposals'][0]))
    original=deepcopy(result)
    out=invoke(donor,record,result,positions,context)
    quantities=[p for p in out['parameter_proposals'] if p['parameter']=='quantity']
    assert len(quantities)==1 and quantities[0]['source_id'].startswith('parent-')
    assert result==original
    again=invoke(donor,record,out,positions,context)
    assert len([p for p in again['parameter_proposals'] if p['parameter']=='quantity'])==1


def test_rectangle_order_does_not_create_a_second_physical_fact():
    donor,record,result,positions,context=fixture()
    basis=donor['payload']['position_check']
    basis['sources'][0]['rects'].append([.1,.3,.5,.4])
    donor['payload']['position_check']=enrich(positions[0],basis,positions,{})
    source=deepcopy(basis['sources'][0])
    source['rects'].reverse()
    result['sources'].append(source)
    result['parameter_proposals'].append(deepcopy(basis['parameter_proposals'][0]))
    out=invoke(donor,record,result,positions,context)
    assert len([p for p in out['parameter_proposals'] if p['parameter']=='quantity'])==1


@pytest.mark.parametrize('change',['rect','version','text','value','unit','unresolved'])
def test_equal_number_is_not_enough_to_replace_another_physical_fact(change):
    donor,record,result,positions,context=fixture()
    basis=donor['payload']['position_check']
    source=deepcopy(basis['sources'][0])
    source['id']='other'
    proposal=deepcopy(basis['parameter_proposals'][0])
    proposal['source_id']='other'
    if change=='rect':
        source['rects']=[[.6,.1,.9,.2]]
    if change=='version':
        source['version_id']='other-pdf'
    if change=='text':
        source['text']='Other construction: '+source['text']
    if change=='value':
        proposal['value']='11'
    if change=='unit':
        proposal['unit']='т'
    if change=='unresolved':
        proposal['scope_state']='unresolved'
    result['sources'].append(source)
    result['parameter_proposals'].append(proposal)
    out=invoke(donor,record,result,positions,context)
    assert proposal in out['parameter_proposals']


def invoke(donor,record,result,positions,context):
    with ExitStack() as stack:
        stack.enter_context(patch('normative_service.position_key',side_effect=lambda p:f'key-{p.index}'))
        stack.enter_context(patch('normative_service.profile_for',return_value='test'))
        stack.enter_context(patch('normative_service.corpus',return_value=SimpleNamespace(identity={'release':'kb'})))
        stack.enter_context(patch('audit_review_policy.review_source',side_effect=lambda raw,task:raw))
        return bind_parent_basis(context,record,result,positions,dict(parent={'index':0},children=[]),position=positions[1])


def geometry_cards(output,positions):
    checked=enrich(positions[1],output,positions,dict(parent={'index':0},children=[]))
    return checked,{c['id']:c for c in checked['parameter_cards'] if c['id'] in {'quantity','thickness'}}


def test_partial_reuse_confirms_geometry_but_keeps_whole_parent_and_rate_open():
    donor,record,result,positions,context=fixture()
    before=deepcopy((donor,result))
    output=invoke(donor,record,result,positions,context)
    checked,cards=geometry_cards(output,positions)
    assert all(c['state']=='confirmed' for c in cards.values())
    assert any(i['state']=='needs_facts' and 'derived:parent' in i['claim_ids'] for i in checked['items'])
    assert not any(i['state']=='confirmed' and 'derived:parent' in i['claim_ids'] for i in checked['items'])
    assert any(i['state']=='needs_facts' and 'derived:rate' in i['claim_ids'] for i in checked['items'])
    assert next(p for p in output['parameter_proposals'] if p['parameter']=='coefficient')==result['parameter_proposals'][0]
    assert not any(p['parameter']=='unit_scale' for p in output['parameter_proposals'])
    assert [s['id'] for s in output['sources'] if s['kind']=='normative']==['own']
    assert (donor,result)==before
    report=publish({'Статус аудита':'Требуется ручная проверка'},dict(checks=[]),'child',
        dict(stage='done',review={**checked,'position_key':'child'}),enabled=True,derived=True)
    assert report['Статус аудита']=='Требуется ручная проверка'
    assert output==invoke(donor,record,output,positions,context)


def test_only_confirmed_card_is_reused_and_retracted_geometry_is_removed_on_replay():
    donor,record,result,positions,context=fixture()
    output=invoke(donor,record,result,positions,context)
    donor['payload']['position_check']['parameter_cards'][1]['state']='needs_facts'
    revised=invoke(donor,record,output,positions,context)
    _,cards=geometry_cards(revised,positions)
    assert cards['quantity']['state']=='confirmed'
    assert cards['thickness']['state']=='needs_facts'
    assert not any(p['parameter']=='thickness' for p in revised['parameter_proposals'])
    trace=next(e for e in revised['evidence_trace'] if e['kind']==GEOMETRY_TRACE)
    assert trace['parameters']==['quantity']


@pytest.mark.parametrize('change',['pdf','vor','key','failed','pipeline'])
def test_partial_reuse_keeps_all_parent_snapshot_guards(change):
    donor,record,result,positions,context=fixture()
    data=donor['payload']
    if change=='pdf':
        data['position_check']['sources'][0]['version_id']='other'
    if change=='vor':
        data['vor_version_id']='other'
    if change=='key':
        data['position_key']='other'
    if change=='kb':
        data['kb']={'release':'other'}
    if change=='failed':
        data['position_check']['execution_state']='technical_failure'
    if change=='pipeline':
        data['position_check']['pipeline_version']=2
    assert invoke(donor,record,result,positions,context)==result


@pytest.mark.parametrize('change',['missing_anchor','invented_quote','invented_rect','scope','ocr','value'])
def test_saved_confirmed_flag_is_not_enough_without_current_literal_evidence(change):
    donor,record,result,positions,context=fixture()
    basis=donor['payload']['position_check']
    q=basis['parameter_cards'][0]
    if change=='missing_anchor':
        q['source_anchors']=[]
    if change=='invented_quote':
        q['source_anchors'][0]['quote']='Площадь другого участка 10 м2'
    if change=='invented_rect':
        q['source_anchors'][0]['rects']=[[.8,.8,.9,.9]]
    if change=='scope':
        basis['parameter_proposals'][0]['scope_state']='unresolved'
    if change=='ocr':
        basis['sources'][0].update(origin='ocr',numeric_verified=False)
    if change=='value':
        basis['parameter_proposals'][0]['value']='11'
    output=invoke(donor,record,result,positions,context)
    assert not any(p['parameter']=='quantity' for p in output['parameter_proposals'])


def test_child_conflicting_quantity_evidence_is_preserved_and_blocks_confirmation():
    donor,record,result,positions,context=fixture()
    other=deepcopy(donor['payload']['position_check']['sources'][0])
    other.update(id='conflicting',text=other['text'].replace('10 м2','11 м2'))
    result['sources'].append(other)
    result['parameter_proposals'].append(dict(parameter='quantity',operation='direct',source_id='conflicting',
        value='11',unit='м2',quote='площадь 11 м2',scope_quote=other['text'],scope_state='verified'))
    output=invoke(donor,record,result,positions,context)
    _,cards=geometry_cards(output,positions)
    assert any(p['source_id']=='conflicting' for p in output['parameter_proposals'])
    assert cards['quantity']['state']=='needs_facts'


def test_retracted_quantity_and_nested_parent_are_not_reused():
    donor,record,result,positions,context=fixture()
    output=invoke(donor,record,result,positions,context)
    basis=donor['payload']['position_check']
    for card in basis['parameter_cards']:
        if card['id'] in {'quantity','thickness'}:
            card['state']='needs_facts'
    revised=invoke(donor,record,output,positions,context)
    assert not any(p['parameter'] in {'quantity','thickness'} for p in revised['parameter_proposals'])
    assert not any(s['id'].startswith('parent-0:geometry:') for s in revised['sources'])
    assert not any(e.get('kind')==GEOMETRY_TRACE for e in revised['evidence_trace'])
    donor,record,result,positions,context=fixture()
    donor['payload']['work_relations']={'parent':{'index':1}}
    nested=invoke(donor,record,result,positions,context)
    assert not any(p['parameter'] in {'quantity','thickness'} for p in nested['parameter_proposals'])


def test_sum_reuse_copies_actual_operands_and_rejects_a_wrong_cached_sum():
    donor,record,result,positions,context=fixture()
    basis=donor['payload']['position_check']
    scope='Нижний слой основания: 4 + 6 = 10 м2'
    total=dict(id='sum',kind='project',origin='calculated_from_cells',version_id='pdf',page=1,
        label='ПД',text=scope,numeric_verified=True,unit_verified=True,unit='м2',rects=[])
    cells=[dict(id=f'cell-{value}',kind='project',origin='ocr',version_id='pdf',page=1,label='ПД',
        text='Нижний слой основания, м2\n'+str(value),numeric_verified=True,unit_verified=True,unit='м2',rects=[])
        for value in (4,6)]
    basis['sources'] += [total,*cells]
    basis['parameter_proposals'][0]=dict(parameter='quantity',operation='sum',source_id='sum',
        operand_source_ids=['cell-4','cell-6'],value='10',unit='м2',quote=scope,scope_quote=scope,scope_state='verified')
    basis['items'][0]['project_source_ids'].append('sum')
    donor['payload']['position_check']=enrich(positions[0],basis,positions,{})
    output=invoke(donor,record,result,positions,context)
    proposal=next(p for p in output['parameter_proposals'] if p['parameter']=='quantity')
    assert len(proposal['operand_source_ids'])==2
    assert all(any(s['id']==sid for s in output['sources']) for sid in proposal['operand_source_ids'])
    _,cards=geometry_cards(output,positions)
    assert cards['quantity']['state']=='confirmed'
    basis=donor['payload']['position_check']
    next(s for s in basis['sources'] if s['id']=='cell-6')['text']='Нижний слой основания, м2\n7'
    failed=invoke(donor,record,result,positions,context)
    assert not any(p['parameter']=='quantity' for p in failed['parameter_proposals'])
