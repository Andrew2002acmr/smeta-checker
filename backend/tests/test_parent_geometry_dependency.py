from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_parent_basis import verified_geometry_dependency, DEPENDENCY_TRACE
from normative_verdict import publish, apply_parent_verdicts, readable_review
from test_normative_parent_geometry import fixture, invoke, geometry_cards


def prepared():
    donor,record,result,positions,context=fixture()
    result=invoke(donor,record,result,positions,context)
    checked,_=geometry_cards(result,positions)
    return donor,record,checked,positions,context


def test_project_dependency_is_verified_while_parent_norm_and_child_rate_stay_open():
    donor,_,checked,positions,_=prepared()
    assert donor['payload']['position_check']['state']=='needs_facts'
    assert verified_geometry_dependency(checked,positions[1])
    assert any('derived:rate' in i['claim_ids'] and i['state']=='needs_facts' for i in checked['items'])
    assert publish({'Статус аудита':'Требуется ручная проверка'}, {}, 'child',
        dict(stage='done',review={**checked,'position_key':'child'}),
        enabled=True,derived=True,position=positions[1])['Статус аудита']=='Требуется ручная проверка'
    original=deepcopy(checked)
    visible=readable_review(checked)
    assert any(i['claim_ids']==['derived:parent'] and i['state']=='confirmed' for i in visible['items'])
    assert any('derived:rate' in i['claim_ids'] and i['state']=='needs_facts' for i in visible['items'])
    assert checked==original


@pytest.mark.parametrize('change',['source','proposal','unit','card','child','proof','missing_thickness'])
def test_changed_or_incomplete_facts_cannot_establish_parent_dependency(change):
    _,_,checked,positions,_=prepared()
    child=positions[1]
    event=next(e for e in checked['evidence_trace'] if e.get('kind')==DEPENDENCY_TRACE)
    if change=='source':
        next(s for s in checked['sources'] if s['kind']=='project')['text']+=' changed'
    if change=='proposal':
        next(p for p in checked['parameter_proposals'] if p['parameter']=='quantity')['value']='11'
    if change in {'unit','card'}:
        card=next(c for c in checked['parameter_cards'] if c['id']=='quantity')
        card.update(found_unit='т') if change=='unit' else card.update(state='needs_facts')
    if change=='child':
        child=child.model_copy(update={'volume':'99'})
    if change=='proof':
        event['proof']['facts']['quantity']['value']='99'
    if change=='missing_thickness':
        checked['parameter_cards']=[c for c in checked['parameter_cards'] if c['id']!='thickness']
    assert not verified_geometry_dependency(checked,child)


def test_geometry_is_retracted_if_parent_document_binding_changes():
    donor,record,checked,positions,context=prepared()
    donor['payload']['vor_version_id']='different'
    revised=invoke(donor,record,checked,positions,context)
    assert not any(e.get('kind')==DEPENDENCY_TRACE for e in revised['evidence_trace'])
    assert not any(p.get('source_id','').startswith('parent-0:geometry:')
        for p in revised['parameter_proposals'])


def test_normative_release_does_not_block_project_facts_or_transfer_norms():
    donor,record,result,positions,context=fixture()
    donor['payload']['kb']={'release':'other-normative-edition'}
    original=deepcopy(donor)
    result=invoke(donor,record,result,positions,context)
    checked,_=geometry_cards(result,positions)
    assert verified_geometry_dependency(checked,positions[1])
    assert [s['id'] for s in checked['sources'] if s['kind']=='normative']==['own']
    assert not any(p['parameter']=='unit_scale' for p in checked['parameter_proposals'])
    assert donor==original


def test_only_proved_geometry_dependency_bypasses_whole_parent_badge():
    parent=SimpleNamespace(position_index=0,queue='review',related_work=None)
    child=SimpleNamespace(position_index=1,queue='ok',report_status='ОК',problem='',
        related_work=dict(index=0,number='1'))
    result=apply_parent_verdicts([parent,deepcopy(child)],enabled=True,running=False)
    assert result[1].queue=='review'
    result=apply_parent_verdicts([parent,child],enabled=True,running=False,verified_dependencies={1})
    assert result[1].queue=='ok'


def test_satisfying_independent_rate_and_norm_can_complete_child_of_partial_parent():
    _,_,checked,positions,_=prepared()
    project=next(s['id'] for s in checked['sources'] if s['kind']=='project')
    checked['sources'].append(dict(id='norm',kind='normative',origin='native',text='Норма расхода'))
    for item in checked['items']:
        if set(item.get('claim_ids',[])) & {'derived:rate','normative:applicability'}:
            item.update(state='confirmed',project_source_ids=[project],normative_source_ids=['norm'])
    # The test material has no formula. Its own independent checks are explicit;
    # the unknown coefficient proposal is not an arithmetic proof or a rate.
    report=publish({'Статус аудита':'ОК'}, {}, 'child',
        dict(stage='done',review={**checked,'position_key':'child'}),enabled=True,
        derived=True,position=positions[1])
    assert report['Статус аудита']=='ОК'


def test_transferred_fact_retains_unequal_area_assembly_for_independent_replay():
    from normative_parameters import enrich
    from test_normative_layer_assembly import fixture as layer_fixture
    donor,record,result,_,context=fixture()
    first,peer=layer_fixture()
    child=first.model_copy(deep=True)
    child.index=1
    child.position_type='Материал'
    child.work_name='Щебёночно-песчаная смесь'
    child.unit='м3'
    child.volume='11'
    child.position_source.row=11
    child.position_source.position_id='material'
    positions={0:first,1:child,2:peer}
    quantity='Нижний слой основания из ЩПС: технологический слой1, h=11 см, площадь 80 м2'
    thickness='Тип III — Конструкция съездов\nНижний слой основания из ЩПС h=22 см'
    basis=dict(pipeline_version=4,execution_state='complete',items=[],related_positions=[],
        sources=[dict(id=key,kind='project',origin='native',version_id='pdf',page=page,
            label='ПД',text=text) for key,text,page in [('q',quantity,1),('h',thickness,2)]],
        parameter_proposals=[dict(parameter=kind,source_id=key,value=value,unit=unit,
            quote=quote,scope_quote=scope,scope_state='verified',operation='direct')
            for kind,key,value,unit,quote,scope in [
                ('quantity','q','80','м2','площадь 80 м2',quantity),
                ('thickness','h','22','см','h=22 см',thickness)]])
    donor['payload']['position_check']=enrich(first,basis,positions,{})
    output=invoke(donor,record,result,positions,context)
    checked,_=geometry_cards(output,positions)
    assert verified_geometry_dependency(checked,child)
    event=next(e for e in checked['evidence_trace'] if e.get('kind')==DEPENDENCY_TRACE)
    assert {p['index'] for p in event['proof']['assembly_positions']}=={0,1,2}
