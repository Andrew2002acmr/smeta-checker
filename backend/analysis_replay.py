"""Versioned stage artifacts: immutable inputs, first committed output wins.

This is reproducible replay, not a promise that fresh LLM sampling is
deterministic. Cache hits still run the current numerical/citation guards.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

DEFAULT_QUESTION = (
    'Следующий слой после сопоставления ПД и ВОР: проверить нормативное основание, состав работ, '
    'измеритель, связанные расчётные строки, коэффициенты и условия. Не считать уточнения размеров, '
    'технологическое разбиение или отсутствие отдельной строки расчётного материала самостоятельной ошибкой.'
)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()


def enabled(context):
    return getattr(context.repository,'supports_analysis_replay',False) is True


@lru_cache(maxsize=3)
def algorithm(stage):
    root=Path(__file__).resolve().parent
    patterns=('pdf_*.py','ocr_*.py','header_ocr_model.py','document_preparation.py','analysis_replay.py') if stage=='prepared_pdf' else (
        'audit_*.py','vor_*.py','analysis_replay.py',*(() if stage=='matching' else ('normative_*.py','human_basis.py','workspace_service.py','pdf_page_routing.py','pdf_drawing_readings.py')))
    paths={p for pattern in patterns for p in root.glob(pattern)}
    paths.update(p for p in (root/'uv.lock',) if p.exists())
    parts={p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in sorted(paths)}
    if stage=='prepared_pdf':
        from header_ocr_model import configured
        model=configured()
        parts['header_model_weights']=model['sha256'] if model else 'not_installed'
        from pdf_paired_models import signature
        parts['paired_model_weights']=signature() or 'not_installed'
    return digest(parts)


def model_identity(settings):
    credentials=settings.llm_credentials()
    return {k:getattr(credentials,k,'') for k in ('provider','base_url','model_name','folder')}


def identity(stage,record,inputs,*,model=None):
    return deepcopy(dict(schema=1,stage=stage,project_id=str(record['project_id']),algorithm=algorithm(stage),inputs=inputs,
        **({'model':model} if model is not None else {})))


def get(context,ident):
    if not enabled(context):
        return None
    saved=context.repository.analysis_artifact(ident['project_id'],digest(ident))
    if saved is not None and saved['identity']!=ident:
        raise ValueError('Reusable artifact inputs do not match')
    return saved['payload'] if saved else None


def put(context,ident,value):
    if not enabled(context):
        return value
    return context.repository.save_analysis_artifact(ident['project_id'],ident,value)


def prepared_inputs(data,version):
    """Explicit raw-source contract shared by preparation and cache lookup."""
    from pdf_text import PDFTextSource,TABLE_OUTPUT,MAX_TABLE_CELL_CHARS,SOURCE_BYTES_CONTRACT
    from document_preparation import VERSION
    return dict(pdf_sha256=hashlib.sha256(bytes(data)).hexdigest(),
        source_bytes_contract=SOURCE_BYTES_CONTRACT,
        preparation_version=VERSION,
        version_id=str(version),extraction_key=PDFTextSource._extraction_key_for(TABLE_OUTPUT),
        ocr_settings_key=PDFTextSource._settings_key_for('rus+eng',200,12000000,4096),
        table_key=hashlib.sha256((PDFTextSource._table_key_for(MAX_TABLE_CELL_CHARS)+':full-document-v1').encode()).hexdigest())


def prepared_identity(record,data,version):
    return identity('prepared_pdf',record,prepared_inputs(data,version))


def store_prepared(context,ident,state):
    from audit_snapshot import load_json,load_snapshot
    native=load_json(context.blob_store,state['native'])
    index=load_snapshot(context.blob_store,state['snapshot'])
    if not index.metadata.get('preparation_complete') or index.pending_ocr:
        raise ValueError('Incomplete document cannot enter reusable artifact storage')
    from pdf_header_preparation import pending_header_pages
    from pdf_geometry_contract import pending_geometry_pages
    if pending_geometry_pages(index):
        raise ValueError('Unvalidated table geometry cannot enter reusable artifact storage')
    if pending_header_pages(index):
        raise ValueError('Unfinished table headers cannot enter reusable artifact storage')
    from pdf_numeric_preparation import pending_numeric_pages
    if pending_numeric_pages(index):
        raise ValueError('Unfinished numeric cells cannot enter reusable artifact storage')
    from pdf_header_lines import pending_pages
    if pending_pages(index):
        raise ValueError('Unfinished semantic headers cannot enter reusable artifact storage')
    if (index.metadata.get('document_version_id')!=ident['inputs']['version_id']
            or native.get('pdf_sha256')!=index.metadata.get('pdf_sha256')):
        raise ValueError('Reusable preparation belongs to another document')
    # Historical artifacts explicitly bound canonical serialization and upload
    # bytes as two domains. New producer contracts keep exact original bytes;
    # a legacy snapshot cannot be relabelled as that stronger source contract.
    from pdf_text import SOURCE_BYTES_CONTRACT
    contract=ident['inputs'].get('source_bytes_contract')
    if contract is not None and (contract!=SOURCE_BYTES_CONTRACT
            or native.get('pdf_sha256')!=ident['inputs']['pdf_sha256']):
        raise ValueError('Prepared snapshot does not match original PDF bytes contract')
    if any(native.get(k)!=ident['inputs'][k] for k in ('extraction_key','ocr_settings_key','table_key')):
        raise ValueError('Prepared document processing settings changed')
    return put(context,ident,dict(state=state))['state']


def replay_prepared(context,ident):
    """Reuse raw OCR across validation-code changes, with unchanged extraction contracts.

    PDF bytes, document version, engine/language/settings and table schema
    must all match. New validators consume the immutable raw readings; this
    never reuses a normative verdict or asserts that all cells are verified.
    """
    cached=get(context,ident)
    if cached is not None:
        return cached
    lookup=getattr(context.repository,'compatible_prepared_artifact',None)
    if not callable(lookup):
        return None
    old=lookup(ident['project_id'],ident['inputs'])
    if old is None:
        return None
    if not isinstance(old,dict):
        raise ValueError('Invalid preparation artifact')
    previous=old['identity']
    if (previous.get('stage')!='prepared_pdf' or previous.get('project_id')!=ident['project_id']
            or previous.get('inputs')!=ident['inputs']):
        raise ValueError('Raw preparation contracts do not match')
    state=dict(old['payload']['state'],reused_from_artifact=digest(previous),
        preparation_reuse_basis='same_source_and_extraction_contract')
    # The preparation barrier may still need to complete deferred headers.
    # Do not commit a legacy snapshot under the new algorithm before that step.
    return dict(state=state)


def matching_identity(context,record,payload):
    # Request/trace identity is intentionally excluded; row, all VOR context,
    # bindings and the exact prepared snapshot remain part of the key.
    if not payload.get('snapshot') or not payload.get('document_preparation_version'):
        return None
    inputs=dict(vor_version_id=str(record['vor_version_id']),
        position={k:v for k,v in payload.items() if k not in {'request_id','replay'}},
        bindings=record['config'].get('pdf_bindings') or {})
    return identity('matching',record,inputs,model=model_identity(context.settings))


def matching_facts(result):
    return {k:v for k,v in result.items() if k not in {'requests','usage','trace_id','trace_url','estimated_cost','replay'}}


def materialize_matching(artifact,key):
    result=deepcopy(artifact['result'])
    result.update(requests=0,usage={},trace_id=None,trace_url=None,estimated_cost=None,
        replay=dict(key=key,stage='matching',origin=artifact['origin']))
    return result


def replay_matching(context,record,payload):
    if not enabled(context):
        return None
    ident=matching_identity(context,record,payload)
    artifact=get(context,ident) if ident else None
    if not artifact:
        return None
    from audit_snapshot import load_snapshot
    index=load_snapshot(context.blob_store,payload['snapshot'])
    if index.pending_ocr or not index.metadata.get('preparation_complete'):
        raise ValueError('Reusable matching requires a complete document snapshot')
    return materialize_matching(artifact,digest(ident))


def store_matching(context,record,payload,result):
    if not enabled(context) or result.get('technical') or result.get('execution_errors'):
        return result
    ident=matching_identity(context,record,payload)
    if ident is None:
        return result
    origin=dict(run_id=str(record['id']),position_index=(payload.get('position_source') or {}).get('row'))
    saved=put(context,ident,dict(result={k:v for k,v in result.items() if k!='replay'},origin=origin))
    return saved['result'] if saved['origin']==origin else materialize_matching(saved,digest(ident))


def normative_identity(context,record,index,payload):
    import normative_service as service
    from human_basis import list_basis
    try:
        job=context.repository.load_position_job(record['id'],index)
    except KeyError:
        return None
    matching=matching_identity(context,record,job.payload)
    if matching is None:
        return None
    position=service.locate_position(context,record,index)
    raw=next(r['result'] for r in record['indexed_results'] if r['position_index']==index)
    human=[{k:v for k,v in row.items() if k not in {'reused','outdated'}} for row in list_basis(context,record,position)]
    revision=getattr(context.repository,'analysis_human_revision',None)
    human_revision=revision(record['project_id'],record['vor_version_id'],list((record['config'].get('pdf_versions') or {}).values())) if revision else ''
    inputs=dict(matching=digest(matching),matching_facts=digest(matching_facts(raw)),
        coefficient_policy=record.get('config', {}).get('normative_coefficient_policy', 'estimator_value_normative_conditions'),
        kb=service.corpus(pin=record['config'].get('normative_kb_pin'),profile=service.profile_for(record)).identity,
        # The matching job stays frozen when table preparation is corrected.
        # Normative retrieval, however, reads this newer document snapshot.
        # Reusing an answer from the old grid would silently undo that repair.
        prepared_corpora=digest(record['config'].get('normative_corpora') or {}),
        positions=digest(record['config'].get('position_inputs') or []),
        human_basis=digest(human),human_revision=human_revision,
        request=dict(question=payload.question or DEFAULT_QUESTION,pages=list(payload.pages),pdf_version_id=payload.pdf_version_id))
    return identity('normative',record,inputs,model=model_identity(context.settings))


def replay_normative(context,record,index,payload,actor):
    if record.get('config',{}).get('normative_fresh_model') is True:
        return None  # Independent model sampling; prepared PDF reuse is unaffected.
    if not enabled(context):
        return None
    ident=normative_identity(context,record,index,payload)
    artifact=get(context,ident) if ident else None
    if not artifact:
        if ident is not None:
            from normative_evidence_cache import replay_evidence
            return replay_evidence(context,record,index,ident,actor)
        return None
    from normative_position_check import validate_agent_saved
    row=dict(id=artifact['origin']['review_id'],payload=deepcopy(artifact['snapshot']))
    row['payload']['work_item_id']=f"{record['id']}:{index}"
    row['payload']['position_check'].setdefault('coverage',{})['replay']=dict(key=digest(ident),origin=artifact['origin'])
    return validate_agent_saved(context,record,index,row,actor,register=False)


def store_normative(context,record,index,payload,saved):
    if record.get('config',{}).get('normative_fresh_model') is True:
        return saved  # Never replace a new sampled answer with the first cached one.
    if not enabled(context) or saved['payload']['position_check'].get('execution_state')!='complete':
        return saved
    ident=normative_identity(context,record,index,payload)
    if ident is None:
        return saved
    from normative_evidence_cache import store_evidence_index
    store_evidence_index(context,record,index,saved,ident)
    artifact=put(context,ident,dict(snapshot=saved['payload'],origin=dict(run_id=str(record['id']),review_id=saved['id'])))
    if artifact['origin']['review_id']==saved['id']:
        return saved
    return replay_normative(context,record,index,payload,'artifact-replay')


def register_validated(context,record,index,saved):
    """Adopt an explicitly revalidated existing answer, not an unversioned OK."""
    if not enabled(context):
        return saved
    if record['config'].get('model')!=context.settings.llm_credentials().model_name:
        return saved
    from workspace_models import NormativePositionCheckRequest
    import normative_service as service
    position=service.locate_position(context,record,index)
    query=saved['payload'].get('query','')
    question='' if query in {position.work_name,DEFAULT_QUESTION} else query
    try:
        job=context.repository.load_position_job(record['id'],index)
    except KeyError:
        return saved
    raw=next(r['result'] for r in record['indexed_results'] if r['position_index']==index)
    store_matching(context,record,job.payload,raw)
    return store_normative(context,record,index,NormativePositionCheckRequest(question=question),saved)
