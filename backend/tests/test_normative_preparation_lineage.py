"""Only archived, byte-verified restarts may refresh a queued legacy source."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import UUID

import pymupdf
import pytest

from analysis_replay import digest,prepared_inputs
from audit_core import PageTextIndex
from audit_snapshot import store_json,store_snapshot,load_json
from document_preparation import VERSION
from normative_agent import EvidenceTools
from normative_preparation_lineage import transition
from storage_blobs import FileBlobStore


@pytest.fixture
def lineage(tmp_path):
    blobs=FileBlobStore(tmp_path/'blobs')
    with pymupdf.open()as document:
        document.new_page(width=100,height=200).insert_text((10,30),'Source text')
        raw=document.tobytes()+b'\n% original upload envelope\n'
    with pymupdf.open(stream=raw,filetype='pdf')as document:
        canonical=hashlib.sha256(document.tobytes(no_new_id=True)).hexdigest()
    original=blobs.put(raw)
    assert canonical!=original.sha256
    version=UUID(int=211)
    project=UUID(int=212)
    expected=prepared_inputs(raw,version)
    geometry={'0':dict(width=100,height=200,matrix=[1,0,0,1,0,0])}
    metadata=dict(document_version_id=str(version),preparation_version=VERSION,
        preparation_complete=True,page_geometry=geometry,
        **{k:expected[k]for k in ('pdf_sha256','extraction_key','ocr_settings_key')})
    new_index=PageTextIndex(('Source text',),tables=([],),metadata=deepcopy(metadata))
    old_meta=dict(deepcopy(metadata),pdf_sha256=canonical,extraction_key='legacy-extraction',ocr_settings_key='legacy-ocr')
    old_index=PageTextIndex(('Source text',),tables=([],),metadata=old_meta)
    old_ref=store_snapshot(blobs,old_index)
    new_ref=store_snapshot(blobs,new_index)
    old_native=dict(pdf_sha256=canonical,extraction_key='legacy-extraction',ocr_settings_key='legacy-ocr',
                    table_key='legacy-table',document_version_id=str(version))
    old_state=dict(version=VERSION,version_id=str(version),page_count=1,
        native=store_json(blobs,old_native),snapshot=old_ref,pages=[])
    new_native={k:expected[k]for k in ('pdf_sha256','extraction_key','ocr_settings_key','table_key')}
    new_native['document_version_id']=str(version)
    new_state=dict(version=VERSION,version_id=str(version),page_count=1,
        native=store_json(blobs,new_native),snapshot=new_ref,pages=[])
    history=dict(document_name='project.pdf',reason='saved_preparation_contract_changed',
        reasons=['native_pdf_sha256_changed'],previous_state=deepcopy(old_state),
        required_contract=expected,previous_normative_corpus=old_ref)
    history['id']=digest(history)
    payload=dict(pdf_name='project.pdf',pdf_versions={'project.pdf':str(version)},snapshot=old_ref)
    job=SimpleNamespace(payload=payload)
    version_ref=SimpleNamespace(id=version,kind='pdf',filename='project.pdf',sha256=original.sha256,blob_key=original.key)
    repo=SimpleNamespace(supports_analysis_replay=False,load_version=lambda _:version_ref,
        version_project_id=lambda _:project,load_position_job=lambda *args:job,
        list_position_jobs=lambda *args,**kwargs:[job])
    record=dict(id=UUID(int=213),project_id=project,config=dict(pdf_versions=payload['pdf_versions'],
        normative_corpora={str(version):new_ref},prepared_documents={'project.pdf':new_state},
        document_preparation_version=VERSION,preparation_history=[history]))
    return SimpleNamespace(context=SimpleNamespace(repository=repo,blob_store=blobs),record=record,
        payload=payload,version=version,old=old_index,new=new_index,new_ref=new_ref,
        entry=history,document=version_ref,old_state=old_state,new_state=new_state)


def invoke(value):
    return transition(value.context,value.record,value.payload,'project.pdf',value.version,
        value.old,value.new,value.new_ref)


def test_complete_history_refreshes_only_new_acquisition_without_mutating_job_or_review(lineage):
    value=lineage
    previous=deepcopy((value.payload,value.record,value.old.to_snapshot()))
    tools=EvidenceTools(value.context,value.record,0,[],None,{})
    assert tools.index.metadata['pdf_sha256']==value.document.sha256
    event=next(e for e in tools.events if e['kind']=='prepared_source_lineage_transition_v1')
    assert event['old_pdf_sha256']==value.old.metadata['pdf_sha256']
    assert event['new_pdf_sha256']==value.document.sha256
    assert event['saved_agent_answers_reusable']is False
    assert event['scope']=='fresh_source_acquisition_only'
    assert tools.snapshot_refs==[value.new_ref]  # No old table fallback.
    assert (value.payload,value.record,value.old.to_snapshot())==previous


@pytest.mark.parametrize('damage',['no_history','history_digest','archived_native_hash','archived_settings',
    'old_snapshot_ref','job_pin','current_pin','document_hash','document_project','document_filename',
    'new_native','required_contract','new_metadata_version','new_metadata_incomplete','old_blob_hash'])
def test_incomplete_or_changed_lineage_never_bypasses_the_strict_pdf_guard(lineage,damage):
    value=lineage
    blobs=value.context.blob_store
    if damage=='no_history':
        value.record['config']['preparation_history']=[]
    elif damage=='history_digest':
        value.entry['id']='forged'
    elif damage in {'archived_native_hash','archived_settings'}:
        native=load_json(blobs,value.entry['previous_state']['native'])
        native['pdf_sha256'if damage=='archived_native_hash'else'extraction_key']='different'
        value.entry['previous_state']['native']=store_json(blobs,native)
    elif damage=='old_snapshot_ref':
        value.entry['previous_state']['snapshot']=value.new_ref
        value.entry['previous_normative_corpus']=value.new_ref
    elif damage=='job_pin':
        value.payload['pdf_versions']={'project.pdf':str(UUID(int=99))}
    elif damage=='current_pin':
        value.record['config']['pdf_versions']={'project.pdf':str(UUID(int=99))}
    elif damage=='document_hash':
        value.document.sha256='0'*64
    elif damage=='document_project':
        value.context.repository.version_project_id=lambda _:UUID(int=99)
    elif damage=='document_filename':
        value.document.filename='another.pdf'
    elif damage=='new_native':
        native=load_json(blobs,value.new_state['native'])
        native['table_key']='other-settings'
        value.new_state['native']=store_json(blobs,native)
    elif damage=='required_contract':
        value.entry['required_contract']['pdf_sha256']='0'*64
    elif damage in {'new_metadata_version','new_metadata_incomplete'}:
        if damage=='new_metadata_version':
            value.new.metadata['document_version_id']=str(UUID(int=99))
        else:
            value.new.metadata['preparation_complete']=False
        value.new_ref=store_snapshot(blobs,value.new)
        value.new_state['snapshot']=value.new_ref
        value.record['config']['normative_corpora'][str(value.version)]=value.new_ref
    elif damage=='old_blob_hash':
        (blobs._root/value.payload['snapshot']['snapshot_key']).write_bytes(b'corrupted old snapshot')
    if damage!='history_digest':
        value.entry['id']=digest({k:v for k,v in value.entry.items()if k!='id'})
    with pytest.raises(ValueError,match='new_run_required'):
        invoke(value)


def test_no_lineage_still_fails_in_the_actual_main_tools_constructor(lineage):
    lineage.record['config']['preparation_history']=[]
    with pytest.raises(ValueError,match='new_run_required'):
        EvidenceTools(lineage.context,lineage.record,0,[],None,{})
