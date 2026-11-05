"""Standalone failed-page retries must preserve the same durable crop store."""
import hashlib
import json
import sys
from types import SimpleNamespace
from uuid import UUID

import pymupdf

from audit_core import PageTextIndex
from audit_snapshot import load_snapshot,store_json,store_snapshot
from scripts import reprepare_document as command
from storage_blobs import FileBlobStore


def test_retry_failed_page_keeps_crop_bytes_in_the_snapshot_store(tmp_path,monkeypatch):
    blobs=FileBlobStore(tmp_path/'blobs')
    version=UUID(int=1)
    run=UUID(int=2)
    with pymupdf.open()as pdf:
        pdf.new_page(width=100,height=100)
        raw=pdf.tobytes()
    original=blobs.put(raw)
    document=SimpleNamespace(id=version,kind='pdf',filename='p.pdf',blob_key=original.key,sha256=original.sha256)
    repo=SimpleNamespace(load_version=lambda _:document,
        load_run=lambda _,**kwargs:dict(config={'pdf_versions':{'p.pdf':str(version)}}))
    monkeypatch.setattr(command.AuditSettings,'load',lambda:SimpleNamespace(database_url='test',blob_store_url='test'))
    monkeypatch.setattr(command,'open_blob_store',lambda _:blobs)
    monkeypatch.setattr(command,'open_repository',lambda *a,**k:repo)
    monkeypatch.setattr(command,'prepared_identity',lambda *a:dict(stage='prepared_pdf',project_id='test',inputs={}))
    packed=dict(page=1,text='',errors=['scan_table_failed'],tables=[],table_rows=[],locators=[],
        quality={},geometry=dict(width=100,height=100,matrix=[1,0,0,1,0,0]),table_error='')
    native=dict(texts=[''],pdf_sha256=original.sha256,document_version_id=str(version),
        extraction_key='extract',ocr_settings_key='ocr')
    def prepare(context,*args,**kwargs):
        index=PageTextIndex([''],errors={0:'scan_table_failed'},tables=[[]],
            metadata=dict(preparation_complete=True,document_version_id=str(version)))
        state=dict(native=store_json(blobs,native),pages=[store_json(blobs,packed)],snapshot=store_snapshot(blobs,index))
        context.repository.update_run_config(run,dict(prepared_documents={'p.pdf':state},document_preparation_version=2))
        return {'p.pdf':state['snapshot']}
    monkeypatch.setattr(command,'prepare_documents',prepare)
    class Source:
        def __init__(self,pdf,*,store_image=None,**kwargs):
            assert callable(store_image),'Retries must receive the durable blob callback'
            self.store_image=store_image
        def prepare_page(self,page):
            crop=self.store_image(b'physical crop from retried page')
            return {**packed,'errors':[],'quality':{'retried_crop':dict(key=crop.key,sha256=crop.sha256)}}
        def close(self):
            pass
    monkeypatch.setattr('pdf_text.PDFTextSource',Source)
    monkeypatch.setattr(command,'store_prepared',lambda context,identity,state:state)
    output=tmp_path/'preparation'
    monkeypatch.setattr(sys,'argv',['reprepare','--version-id',str(version),'--template-run',str(run),
        '--out',str(output),'--retry-failed'])
    command.main()
    report=json.loads((output/'report.json').read_text(encoding='utf-8'))
    assert report['ready']and not report['errors']
    index=load_snapshot(blobs,report['snapshot'])
    crop=index.quality[0]['retried_crop']
    assert hashlib.sha256(blobs.get(crop['key'])).hexdigest()==crop['sha256']
