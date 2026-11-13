from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch

import pytest

from audit_models import VorPosition,PositionSource
from normative_layer_reuse import reuse_layer_check
from normative_parameters import enrich


def fixture():
    def position(index,height,stage):
        return VorPosition(index=index,number=str(index+1),volume='42.5',unit='м2',position_type='Работа',
            work_name=f'Устройство нижнего слоя основания, h={height} см (двухслойного основания - слой{stage})',
            comment='При толщине основания более 15 см его нормировать в два слоя',
            position_source=PositionSource(document_sha256='vor',format='xlsx',sheet='S',row=10+index,header_row=1,position_id=str(index)))
    a,b=position(0,10,1),position(2,14,2)
    sources=[dict(id='p',kind='project',origin='native',version_id='pdf',page=1,label='ПД',
        text='Нижний слой основания: площадь 42,5 м2, h=24 см.'),
        dict(id='n',kind='normative',origin='native',page=2,label='Норма',
        text='При толщине основания более 15 см его нормировать в два слоя.')]
    related=[dict(index=0,row=10,work=a.work_name,quantity=a.volume,unit=a.unit)]
    proof=dict(pipeline_version=3,execution_state='complete',state='needs_facts',sources=sources,summary='Проверено',
        related_positions=related,evidence_trace=[],parameter_proposals=[
            dict(parameter='quantity',operation='direct',source_id='p',value='42.5',unit='м2',
                quote='площадь 42,5 м2',scope_quote='Нижний слой основания: площадь 42,5 м2, h=24 см.',scope_state='verified'),
            dict(parameter='thickness',operation='direct',source_id='p',value='24',unit='см',
                quote='h=24 см',scope_quote='Нижний слой основания: площадь 42,5 м2, h=24 см.',scope_state='verified')],
        items=[dict(topic='Основание',state='confirmed',required=True,claim_ids=['presence','quantity','unit','method','normative:applicability'],
            project_source_ids=['p'],normative_source_ids=['n'],explanation='Источник проверен')])
    proof=enrich(b,proof,{0:a,2:b},{})
    donor=dict(id='donor',kind='check',payload=dict(vor_version_id='vor',position_key='key-2',position_check=proof,kb={'sha256':'kb'}))
    stored=dict(status='needs_review',checks=[dict(claim_id='quantity',state='not_found',required=True)])
    record=dict(id='run',vor_version_id='vor',config=dict(pdf_versions={'document':'pdf'}),
        indexed_results=[dict(position_index=i,result=deepcopy(stored)) for i in (0,2)])
    repo=Mock()
    repo.normative_reviews.return_value=[donor]
    repo.save_normative_review.side_effect=lambda run,index,kind,payload,actor:dict(id='saved',payload=payload)
    return a,b,donor,record,SimpleNamespace(repository=repo)


def invoke(a,b,record,context,extra=(),links=None):
    with ExitStack() as stack:
        stack.enter_context(patch('workspace_service.inputs_for_run',return_value=[a,b,*extra]))
        stack.enter_context(patch('vor_material_links.material_links',return_value=links or {}))
        stack.enter_context(patch('normative_service.profile_for',return_value='test'))
        stack.enter_context(patch('normative_service.corpus',return_value=SimpleNamespace(identity={'sha256':'kb'})))
        stack.enter_context(patch('audit_review_policy.review_source',side_effect=lambda raw,task:raw))
        stack.enter_context(patch('normative_service.position_key',side_effect=lambda p:f'key-{p.index}'))
        stack.enter_context(patch('normative_service.project_context',return_value=(a,SimpleNamespace(model_dump=lambda:{}),{},[])))
        return reuse_layer_check(context,record,0,'test')


def test_verified_work_basis_is_reused_without_copying_material_factors_or_mutating_donor():
    a,b,donor,record,context=fixture()
    original=deepcopy(donor)
    saved=invoke(a,b,record,context)
    assert saved is not None
    proof=saved['payload']['position_check']
    assert proof['state']=='confirmed'
    assert proof['telemetry']['requests']==0
    assert proof['evidence_trace'][0]['review_id']=='donor'
    assert all(p['parameter']!='coefficient' for p in proof['parameter_proposals'])
    assert next(c for c in proof['parameter_cards'] if c['id']=='thickness')['state']=='confirmed'
    assert donor==original


@pytest.mark.parametrize('change',['comment','quantity','pdf','vor','kb','failed','cycle','extra_claim'])
def test_scope_revision_or_unresolved_target_claim_prevents_reuse(change):
    a,b,donor,record,context=fixture()
    if change=='comment':
        a.comment+=' Другое условие.'
    if change=='quantity':
        a.volume='43'
    if change=='pdf':
        record['config']['pdf_versions']={'document':'other-pdf'}
    if change=='vor':
        donor['payload']['vor_version_id']='other-vor'
    if change=='kb':
        donor['payload']['kb']={'sha256':'other-kb'}
    if change=='failed':
        donor['payload']['position_check']['execution_state']='technical_failure'
    if change=='cycle':
        donor['payload']['position_check']['evidence_trace']=[dict(kind='reused_layer_basis')]
    if change=='extra_claim':
        record['indexed_results'][0]['result']['checks'].append(dict(claim_id='attribute:other',required=True,state='not_found'))
    assert invoke(a,b,record,context) is None
    context.repository.save_normative_review.assert_not_called()


def test_different_child_materials_prevent_work_basis_reuse():
    a,b,donor,record,context=fixture()
    child1=a.model_copy(update={'index':1,'work_name':'Смесь 0/31,5','volume':'7','unit':'м3'})
    child2=b.model_copy(update={'index':3,'work_name':'Смесь 0/63','volume':'7','unit':'м3'})
    assert invoke(a,b,record,context,extra=(child1,child2),links={1:{'index':0},3:{'index':2}}) is None
    context.repository.save_normative_review.assert_not_called()
