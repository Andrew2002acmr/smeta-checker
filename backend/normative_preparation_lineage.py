"""Permit fresh source acquisition across an explicitly archived CPU restart.

This is not an agent-answer replay policy. Old jobs, reviews and snapshots stay
immutable; a new acquisition may consume only the fully verified new corpus.
"""
import hashlib
from copy import deepcopy
import re
from uuid import UUID

from analysis_replay import digest
from audit_snapshot import load_json,load_snapshot
from document_preparation import VERSION
from pdf_text import SOURCE_BYTES_CONTRACT

ERROR='Prepared normative corpus belongs to another PDF; new_run_required: '


def transition(context,record,payload,filename,version,old_index,new_index,new_ref):
    """Return a trace event only for one complete, content-checked lineage."""
    def require(condition,reason):
        if not condition:
            raise ValueError(reason)
    try:
        version=str(version)
        config=record['config']
        require((config.get('pdf_versions')or{}).get(filename)==version,'current PDF pin mismatch')
        require(payload.get('pdf_name')==filename and (payload.get('pdf_versions')or{}).get(filename)==version,
                'original job filename/version pin mismatch')
        require((config.get('normative_corpora')or{}).get(version)==new_ref,'new corpus reference mismatch')
        document=context.repository.load_version(UUID(version))
        require(str(document.id)==version and document.kind=='pdf' and document.filename==filename,
                'registered PDF identity mismatch')
        require(str(context.repository.version_project_id(UUID(version)))==str(record['project_id']),
                'registered PDF project mismatch')
        require(hashlib.sha256(context.blob_store.get(document.blob_key)).hexdigest()==document.sha256,
                'registered original PDF bytes hash mismatch')
        state=(config.get('prepared_documents')or{}).get(filename)or{}
        require(state.get('version')==VERSION and state.get('version_id')==version
                and state.get('snapshot')==new_ref,'new preparation state/reference mismatch')
        require(load_snapshot(context.blob_store,new_ref).to_snapshot()==new_index.to_snapshot(),
                'new acquisition corpus was substituted')
        native=load_json(context.blob_store,state['native'])
        require(native.get('document_version_id')==version,'new native document version mismatch')
        require(new_index.metadata.get('document_version_id')==version
                and new_index.metadata.get('preparation_version')==VERSION
                and new_index.metadata.get('preparation_complete') is True and not new_index.pending_ocr
                and state.get('page_count')==len(new_index.texts)>0,'new corpus is not fully prepared')
        require(all((new_index.metadata.get('page_geometry')or{}).get(str(p))for p in range(len(new_index.texts))),
                'new corpus geometry is incomplete')
        valid=[]
        for entry in config.get('preparation_history')or[]:
            previous=entry.get('previous_state')or{}
            if (entry.get('document_name')!=filename or previous.get('version_id')!=version
                    or payload.get('snapshot')not in [previous.get('snapshot'),entry.get('previous_normative_corpus')]):
                continue
            require(entry.get('id')==digest({k:v for k,v in entry.items()if k!='id'}),
                    'preparation history integrity mismatch')
            require(entry.get('reason')=='saved_preparation_contract_changed' and entry.get('reasons'),
                    'preparation history reason missing')
            expected=entry.get('required_contract')or{}
            require(set(expected)=={'pdf_sha256','source_bytes_contract','preparation_version','version_id',
                    'extraction_key','ocr_settings_key','table_key'},'required source contract incomplete')
            require(expected['source_bytes_contract']==SOURCE_BYTES_CONTRACT
                    and expected['preparation_version']==VERSION and expected['version_id']==version,
                    'required source contract/version mismatch')
            require(expected['pdf_sha256']==document.sha256==new_index.metadata.get('pdf_sha256'),
                    'new original PDF hash mismatch')
            require(all(native.get(key)==expected[key]for key in
                    ('pdf_sha256','extraction_key','ocr_settings_key','table_key')),
                    'new native source/settings mismatch')
            require(all(new_index.metadata.get(key)==expected[key]for key in
                    ('pdf_sha256','extraction_key','ocr_settings_key')),
                    'new snapshot source/settings mismatch')
            original=load_snapshot(context.blob_store,payload['snapshot'])
            require(original.to_snapshot()==old_index.to_snapshot(),'original job corpus was substituted')
            old_native=load_json(context.blob_store,previous['native'])
            require(isinstance(old_native.get('pdf_sha256'),str)
                    and re.fullmatch(r'[0-9a-f]{64}',old_native['pdf_sha256']),
                    'archived PDF hash is not a source digest')
            require(all(isinstance(old_native.get(key),str)and old_native[key]
                    and old_native[key]==old_index.metadata.get(key)for key in
                    ('pdf_sha256','extraction_key','ocr_settings_key')),
                    'archived native/snapshot source/settings mismatch')
            require(bool(old_native.get('table_key')),'archived native table contract missing')
            require(old_native.get('document_version_id',version)==version
                    and old_index.metadata.get('document_version_id',version)==version,
                    'archived document version mismatch')
            valid.append(entry)
        require(len(valid)==1,'complete unambiguous preparation lineage unavailable')
        entry=valid[0]
        return dict(kind='prepared_source_lineage_transition_v1',history_id=entry['id'],
            document_version_id=version,filename=filename,
            old_pdf_sha256=old_index.metadata['pdf_sha256'],new_pdf_sha256=new_index.metadata['pdf_sha256'],
            old_snapshot=deepcopy(payload['snapshot']),new_snapshot=deepcopy(new_ref),
            scope='fresh_source_acquisition_only',saved_agent_answers_reusable=False,
            legacy_snapshot_fallback_disabled=True)
    except (ValueError,KeyError,TypeError,AttributeError,OSError)as exc:
        raise ValueError(ERROR+str(exc))from exc
