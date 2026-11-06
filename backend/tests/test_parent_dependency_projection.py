"""Actual PostgreSQL projection must preserve independently replayable facts."""
from normative_parent_basis import verified_geometry_dependency
from storage_blobs import FileBlobStore
from storage_db import open_repository
from test_storage import _postgres_url
from test_parent_geometry_dependency import prepared


def test_compact_result_keeps_parent_fact_proof_and_its_geometry_sources(tmp_path):
    _,_,review,positions,_=prepared()
    review['evidence_trace'].append(dict(kind='unneeded_tool_snapshot',large='omitted'))
    repo=open_repository(_postgres_url(),blob_store=FileBlobStore(tmp_path))
    try:
        project=repo.create_project('Independent parent facts projection')
        department_id = repo.list_departments(project.id)[0]["id"]
        vor=repo.add_document_version(project.id,b'isolated workbook',filename='vor.xlsx',kind='vor',new_document=True,department_id=department_id)
        run=repo.create_run(project.id,vor.id,{})
        repo.save_normative_review(run.id,1,'check',dict(position_key='key',position_check=review),'test')
        compact=repo.normative_validation_context(run.id)[1]['review']
        assert verified_geometry_dependency(review,positions[1])
        assert verified_geometry_dependency(compact,positions[1])
        # Geometry replay must retain the child's own unresolved rate inputs.
        # Dropping that coefficient/source would hide an independent obligation.
        assert compact['parameter_proposals'] == review['parameter_proposals']
        assert {p['parameter'] for p in compact['parameter_proposals']} == {'quantity','thickness','coefficient'}
        coefficient = next(p for p in compact['parameter_proposals'] if p['parameter'] == 'coefficient')
        assert coefficient['scope_state'] == 'unresolved'
        assert next(s for s in compact['sources'] if s['id'] == coefficient['source_id'])['kind'] == 'normative'
        assert any('derived:rate' in i['claim_ids'] and i['state']=='needs_facts' for i in compact['items'])
        assert not any(e.get('kind')=='unneeded_tool_snapshot' for e in compact['evidence_trace'])
    finally:
        repo._engine.dispose()
