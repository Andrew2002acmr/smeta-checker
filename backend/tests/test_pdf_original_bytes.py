"""Byte-exact original PDF identity across the streamed CPU preparation path."""
import hashlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pymupdf
import pytest

from pdf_text import PDFTextSource
from document_preparation import prepare_documents
from audit_snapshot import load_snapshot
from storage_blobs import FileBlobStore


def pdf(label='original'):
    with pymupdf.open() as doc:
        page=doc.new_page()
        page.insert_text((40,60),(label+' source engineering text. ')*8)
        raw=doc.tobytes()
    # A legal trailing comment proves why serialization is not upload identity.
    return raw+b'\n% original byte envelope\n'


@pytest.fixture
def native_only(monkeypatch):
    def native(source, selected=None):
        return tuple(p.get_text()for p in source._doc),{},[],set()
    monkeypatch.setattr(PDFTextSource,'_read_native',native)


@pytest.mark.parametrize('explicit',[True,False])
def test_streamed_document_keeps_exact_upload_for_hash_and_private_ocr_file(tmp_path,native_only,explicit):
    raw=pdf()
    expected=hashlib.sha256(raw).hexdigest()
    with pymupdf.open(stream=raw,filetype='pdf')as doc:
        assert hashlib.sha256(doc.tobytes(no_new_id=True)).hexdigest()!=expected
        assert PDFTextSource._pdf_sha256(doc)==expected
        source=PDFTextSource(doc,cache_dir=tmp_path/'cache',**({'source_bytes':raw}if explicit else{}))
        path=Path(source._tmp_path)
        assert path.read_bytes()==raw
        assert source.native_state()['pdf_sha256']==expected
        assert doc.stream==raw and not doc.is_dirty
        source.close()
        assert not path.exists()


@pytest.mark.parametrize('change',['other_same_pages','modified_document','empty','wrong_type'])
def test_explicit_bytes_cannot_rebind_another_or_modified_document(tmp_path,native_only,change):
    raw=pdf()
    with pymupdf.open(stream=raw,filetype='pdf')as doc:
        supplied=raw
        if change=='other_same_pages':
            supplied=pdf('different')
        if change=='modified_document':
            doc[0].insert_text((40,100),'Changed after parsing')
        if change=='empty':
            supplied=b''
        if change=='wrong_type':
            supplied='not bytes'
        with pytest.raises(ValueError,match='Original PDF bytes'):
            PDFTextSource(doc,cache_dir=tmp_path/'cache',source_bytes=supplied)


def test_file_backed_document_rejects_foreign_explicit_bytes(tmp_path,native_only):
    raw=pdf()
    path=tmp_path/'original.pdf'
    path.write_bytes(raw)
    with pymupdf.open(path)as doc:
        with pytest.raises(ValueError,match='differ'):
            PDFTextSource(doc,cache_dir=tmp_path/'cache',source_bytes=pdf('other'))
        with closing(PDFTextSource(doc,cache_dir=tmp_path/'cache',source_bytes=raw))as source:
            assert source.native_state()['pdf_sha256']==hashlib.sha256(raw).hexdigest()


def test_legacy_serialization_snapshot_cannot_be_relinked_to_original_upload(tmp_path,native_only):
    raw=pdf()
    with pymupdf.open(stream=raw,filetype='pdf')as doc:
        with closing(PDFTextSource(doc,cache_dir=tmp_path/'cache',source_bytes=raw,full_document=True))as source:
            native=source.native_state()
        native['pdf_sha256']=hashlib.sha256(doc.tobytes(no_new_id=True)).hexdigest()
        with pytest.raises(ValueError,match='another PDF or extractor configuration'):
            PDFTextSource(doc,cache_dir=tmp_path/'cache',source_bytes=raw,native_state=native,full_document=True)


def test_main_document_preparation_preserves_upload_identity_in_durable_snapshot(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw=pdf()
    version=UUID(int=4242)
    class Repository:
        supports_analysis_replay=False
        def __init__(self):self.config={}
        def load_run(self,*args,**kwargs):return dict(config=self.config)
        def update_run_config(self,run,patch):self.config.update(patch)
    repo=Repository()
    blobs=FileBlobStore(tmp_path/'blobs')
    context=SimpleNamespace(repository=repo,blob_store=blobs)
    snapshots=prepare_documents(context,UUID(int=1),{'pd.pdf':raw},{'pd.pdf':SimpleNamespace(id=version)},
        checkpoint=lambda:None,cancelled=lambda:False)
    index=load_snapshot(blobs,snapshots['pd.pdf'])
    assert index.metadata['pdf_sha256']==hashlib.sha256(raw).hexdigest()
    assert index.metadata['document_version_id']==str(version)
    assert index.metadata['preparation_complete']
    assert index.metadata['page_geometry']['0']['width']>0


def test_current_preparation_cache_rejects_a_relabelled_legacy_snapshot(tmp_path):
    from analysis_replay import store_prepared
    from audit_snapshot import store_json,store_snapshot
    from audit_core import PageTextIndex
    from pdf_text import SOURCE_BYTES_CONTRACT
    blobs=FileBlobStore(tmp_path/'blobs')
    settings=dict(extraction_key='current',ocr_settings_key='current',table_key='current')
    legacy_native=dict(pdf_sha256='canonical-serialization',**settings)
    legacy_index=PageTextIndex(('Text',),metadata=dict(preparation_complete=True,
        document_version_id='v',pdf_sha256=legacy_native['pdf_sha256']))
    state=dict(native=store_json(blobs,legacy_native),snapshot=store_snapshot(blobs,legacy_index))
    ident=dict(project_id='project',inputs=dict(version_id='v',pdf_sha256='original-upload',
        source_bytes_contract=SOURCE_BYTES_CONTRACT,**settings))
    context=SimpleNamespace(blob_store=blobs,repository=SimpleNamespace(supports_analysis_replay=False))
    with pytest.raises(ValueError,match='original PDF bytes contract'):
        store_prepared(context,ident,state)
    # Keep old immutable history readable in its original explicit two domains.
    ident['inputs'].pop('source_bytes_contract')
    assert store_prepared(context,ident,state)==state


@pytest.mark.parametrize('legacy',['partial_native','completed_native_snapshot','missing_geometry'])
def test_explicit_preparation_resume_archives_legacy_state_and_runs_fresh_cpu(tmp_path,monkeypatch,legacy):
    from copy import deepcopy
    from audit_snapshot import load_json,store_json,store_snapshot
    from analysis_replay import prepared_inputs
    monkeypatch.chdir(tmp_path)
    raw=pdf()
    version=UUID(int=4243)
    class Repository:
        supports_analysis_replay=False
        def __init__(self):self.config={}
        def load_run(self,*args,**kwargs):return dict(config=self.config)
        def update_run_config(self,run,patch):self.config.update(deepcopy(patch))
    repo=Repository()
    blobs=FileBlobStore(tmp_path/'blobs')
    context=SimpleNamespace(repository=repo,blob_store=blobs)
    def prepare():
        return prepare_documents(context,UUID(int=2),{'pd.pdf':raw},{'pd.pdf':SimpleNamespace(id=version)},
            checkpoint=lambda:None,cancelled=lambda:False)
    prepare()
    state=deepcopy(repo.config['prepared_documents']['pd.pdf'])
    native=load_json(blobs,state['native'])
    index=load_snapshot(blobs,state['snapshot'])
    if legacy=='missing_geometry':
        index.metadata['page_geometry']={}
    else:
        native['extraction_key']='legacy-canonical-serialization'
        index.metadata['extraction_key']=native['extraction_key']
        state['native']=store_json(blobs,native)
    state['snapshot']=store_snapshot(blobs,index)
    if legacy=='partial_native':
        state.pop('snapshot')
    old_state=deepcopy(state)
    old_native=blobs.get(state['native']['key'])
    old_snapshot=blobs.get(state['snapshot']['snapshot_key'])if state.get('snapshot')else None
    repo.config.update(prepared_documents={'pd.pdf':state},working_conclusion='do not rewrite')
    count=[]
    real=PDFTextSource.prepare_page
    def counted(source,page):
        count.append(page)
        return real(source,page)
    monkeypatch.setattr(PDFTextSource,'prepare_page',counted)
    refs=prepare()
    assert count==[0]  # Genuine producer, not a relabelled legacy snapshot.
    assert repo.config['documents_prepared']
    assert repo.config['working_conclusion']=='do not rewrite'
    history=repo.config['preparation_history']
    assert len(history)==1 and history[0]['previous_state']==old_state
    assert history[0]['reason']=='saved_preparation_contract_changed'
    assert history[0]['required_contract']==prepared_inputs(raw,version)
    result=load_snapshot(blobs,refs['pd.pdf'])
    assert result.metadata['pdf_sha256']==hashlib.sha256(raw).hexdigest()
    assert result.metadata['page_geometry']['0']['width']>0
    assert blobs.get(old_state['native']['key'])==old_native
    if old_snapshot:
        assert blobs.get(old_state['snapshot']['snapshot_key'])==old_snapshot
        assert refs['pd.pdf']!=old_state['snapshot']
    assert prepare()==refs
    assert count==[0]and repo.config['preparation_history']==history
