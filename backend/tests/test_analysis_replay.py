"""Run-independent stage identity and deterministic immutable replay."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from analysis_replay import (digest,identity,matching_identity,store_matching,replay_matching,
    normative_identity,store_normative,replay_normative,prepared_identity,DEFAULT_QUESTION)
from workspace_models import NormativePositionCheckRequest


class MemoryArtifacts:
    supports_analysis_replay=True
    def __init__(self):
        self.values={}
    def analysis_artifact(self,project,key):
        item=self.values.get((str(project),key))
        return deepcopy(item) if item else None
    def save_analysis_artifact(self,project,ident,value):
        return deepcopy(self.values.setdefault((str(project),digest(ident)),dict(identity=deepcopy(ident),payload=deepcopy(value)))['payload'])


@pytest.fixture
def sample():
    repo=MemoryArtifacts()
    credentials=SimpleNamespace(provider='mock',base_url='https://example.invalid',model_name='model-A',folder='')
    settings=SimpleNamespace(llm_credentials=lambda:credentials)
    ctx=SimpleNamespace(repository=repo,settings=settings,blob_store=object())
    record=dict(id='run-A',project_id='project',vor_version_id='vor',config=dict(pdf_bindings={},position_inputs=[{'index':0}]),
        indexed_results=[dict(position_index=0,result={'status':'ОК','checks':[]})])
    payload=dict(document_preparation_version=1,snapshot={'snapshot_sha256':'pdf-snapshot'},
        pdf_versions={'doc.pdf':'version'},vor_context={'sha256':'all-vor-rows'},work_name='Test work',request_id='request-A')
    repo.load_position_job=lambda *args:SimpleNamespace(payload=payload)
    return ctx,record,payload,credentials


def test_two_independent_runs_use_same_matching_artifact_and_keep_source_proof(sample):
    ctx,record,payload,_=sample
    raw=dict(status='ОК',checks=[{'claim_id':'quantity'}],requests=3,usage={'total':100},trace_id='original',evidence=[{'quote':'12 м'}])
    store_matching(ctx,record,payload,raw)
    other=dict(record,id='run-B')
    second=dict(payload,request_id='request-B')
    with patch('audit_snapshot.load_snapshot',return_value=SimpleNamespace(pending_ocr=[],metadata={'preparation_complete':True})),\
            patch('normative_context._ask',side_effect=AssertionError('No new LLM')):
        a=replay_matching(ctx,other,second)
        b=replay_matching(ctx,dict(record,id='run-C'),second)
    assert a==b
    assert a['checks']==raw['checks'] and a['evidence']==raw['evidence']
    assert a['requests']==0 and a['usage']=={}
    assert raw['requests']==3 and a['replay']['origin']['run_id']=='run-A'


@pytest.mark.parametrize('change',['vor','pdf','binding','model','provider','code','context','project'])
def test_changed_input_invalidates_matching_replay(sample,change):
    ctx,record,payload,credentials=sample
    original=matching_identity(ctx,record,payload)
    if change=='vor':
        record['vor_version_id']='new-vor'
    if change=='pdf':
        payload['snapshot']['snapshot_sha256']='changed'
    if change=='binding':
        record['config']['pdf_bindings']={'reference':'other.pdf'}
    if change=='model':
        credentials.model_name='model-B'
    if change=='provider':
        credentials.base_url='https://other.invalid'
    if change=='context':
        payload['vor_context']['sha256']='changed-linked-row'
    if change=='project':
        record['project_id']='other-project'
    if change=='code':
        with patch('analysis_replay.algorithm',return_value='changed-code'):
            assert matching_identity(ctx,record,payload)!=original
    else:
        assert matching_identity(ctx,record,payload)!=original


def test_failed_or_incomplete_technical_matching_is_not_cached(sample):
    ctx,record,payload,_=sample
    store_matching(ctx,record,payload,{'execution_errors':['provider failed']})
    assert ctx.repository.values=={}


def test_prepared_document_key_covers_bytes_version_and_ocr_configuration(sample):
    _,record,_,_=sample
    first=prepared_identity(record,b'PDF-A','version-A')
    assert prepared_identity(dict(record,id='different-run'),b'PDF-A','version-A')==first
    assert prepared_identity(record,b'PDF-B','version-A')!=first
    assert prepared_identity(record,b'PDF-A','version-B')!=first
    with patch('pdf_text.PDFTextSource._settings_key_for',return_value='other-ocr-settings'):
        assert prepared_identity(record,b'PDF-A','version-A')!=first


def test_normative_replay_is_bound_to_kb_human_corrections_related_rows_and_question(sample):
    ctx,record,payload,_=sample
    request=NormativePositionCheckRequest()
    basis=[]
    kb={'sha256':'kb-A','release_id':'release'}
    with patch('normative_service.locate_position',return_value=object()),\
            patch('normative_service.corpus',side_effect=lambda **kw:SimpleNamespace(identity=kb)),\
            patch('human_basis.list_basis',side_effect=lambda *args:basis):
        original=normative_identity(ctx,record,0,request)
        assert normative_identity(ctx,dict(record,id='run-B'),0,NormativePositionCheckRequest(question=DEFAULT_QUESTION))==original
        basis.append({'quote':'unchanged','reused':False,'outdated':False})
        local=normative_identity(ctx,record,0,request)
        basis[0]['reused']=True
        assert normative_identity(ctx,dict(record,id='run-B'),0,request)==local
        basis.clear()
        basis.append({'quote':'Human correction'})
        assert normative_identity(ctx,record,0,request)!=original
        basis.clear()
        kb['sha256']='kb-B'
        assert normative_identity(ctx,record,0,request)!=original
        kb['sha256']='kb-A'
        record['config']['position_inputs'].append({'index':1,'formula':'changed'})
        assert normative_identity(ctx,record,0,request)!=original
        record['config']['position_inputs'].pop()
        record['config']['normative_corpora']={'version':{'snapshot_sha256':'repaired-grid','corpus_id':'new-cells'}}
        assert normative_identity(ctx,record,0,request)!=original
        record['config'].pop('normative_corpora')
        assert normative_identity(ctx,record,0,request)==original
        assert normative_identity(ctx,record,0,NormativePositionCheckRequest(question='Different task'))!=original
        ctx.repository.analysis_human_revision=lambda *args:'parent-corrected'
        assert normative_identity(ctx,record,0,request)!=original


def test_normative_hit_revalidates_saved_proof_for_new_run_without_model(sample):
    ctx,record,_,_=sample
    saved=dict(id='review',payload={'position_check':{'execution_state':'complete','coverage':{},'sources':[{'id':'cell'}]},'work_item_id':'run-A:0'})
    with patch('normative_service.locate_position',return_value=object()),\
            patch('normative_service.corpus',return_value=SimpleNamespace(identity={'sha256':'kb'})),\
            patch('human_basis.list_basis',return_value=[]):
        store_normative(ctx,record,0,NormativePositionCheckRequest(),saved)
        with patch('normative_position_check.validate_agent_saved',side_effect=lambda *args,**kw: {'row':args[3],'register':kw['register']}) as validate:
            hit=replay_normative(ctx,dict(record,id='run-B'),0,NormativePositionCheckRequest(),'test')
    assert validate.call_count==1 and not hit['register']
    assert hit['row']['payload']['work_item_id']=='run-B:0'
    assert hit['row']['payload']['position_check']['sources']==[{'id':'cell'}]
    assert saved['payload']['work_item_id']=='run-A:0'


def test_first_committed_output_wins_for_identical_stage_identity(sample):
    ctx,record,_,_=sample
    ident=identity('matching',record,{'inputs':'same'})
    first=ctx.repository.save_analysis_artifact(record['project_id'],ident,{'answer':'A'})
    second=ctx.repository.save_analysis_artifact(record['project_id'],ident,{'answer':'B'})
    assert first==second=={'answer':'A'}
