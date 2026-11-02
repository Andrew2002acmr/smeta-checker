"""Real PostgreSQL and worker boundary; model responses are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

import pytest

from analysis_replay import identity,digest
from audit_tasks import prepare_run,audit_position
from test_tasks import TaskPipelineTests as _Pipeline,_fake_vor_bytes,_fake_pdf_bytes


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    context,repo=_Pipeline()._make_context(str(tmp_path/'blobs'))
    vor,pdf=_fake_vor_bytes(),_fake_pdf_bytes()
    run,versions=_Pipeline()._make_run(repo,vor,pdf)
    yield context,repo,run,versions,vor,pdf
    repo._engine.dispose()


def test_new_run_reuses_full_preparation_and_matching_without_extractor_or_model(setup):
    ctx,repo,first,versions,vor,pdf=setup
    def prepare(run):
        return prepare_run(ctx,run.id,project='ИГДИ',vor_filename='vor.csv',vor_bytes=vor,
            pdf_files={'project.pdf':pdf},pdf_versions=versions,worker='pdf-test',dispatch=lambda *args:None)
    with patch('audit_tracing._configured_client',return_value=None):
        prepare(first)
        audit_position(ctx,first.id,0,worker='llm-A',dispatch=lambda *args:None)
    original=repo.load_position_job(first.id,0)
    record=repo.load_run(first.id)
    second=repo.create_run(record['project_id'],record['vor_version_id'],
        {'model':record['config']['model'],'pdf_versions':record['config']['pdf_versions'],'vor_filename':'vor.csv'},
        pdf_pins={name:v.id for name,v in versions.items()})
    with (patch('pdf_text.PDFTextSource.__init__',side_effect=AssertionError('No extraction/OCR')),
            patch('audit_tasks.traced_worker_check_row',side_effect=AssertionError('No model'))):
        prepare(second)
        result=audit_position(ctx,second.id,0,worker='llm-B',dispatch=lambda *args:None)
    copied=repo.load_position_job(second.id,0)
    assert result['reused']
    assert copied.payload['snapshot']==original.payload['snapshot']
    assert copied.result['checks']==original.result['checks']
    assert copied.result['requests']==0
    assert repo.load_run(second.id)['config']['preparation_reused_documents']==['project.pdf']


def test_concurrent_first_writers_publish_one_output_and_project_scope_is_enforced(setup):
    _,repo,run,_,_,_=setup
    record=repo.load_run(run.id)
    ident=identity('matching',record,{'same':'inputs'})
    def save(n):
        return repo.save_analysis_artifact(record['project_id'],ident,{'answer':n})
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(save,[1,2]))
    assert outputs[0]==outputs[1]
    other=repo.create_project('different project')
    assert repo.analysis_artifact(other.id,digest(ident)) is None
    bad=deepcopy(ident)
    bad['inputs']['same']='changed'
    assert repo.analysis_artifact(record['project_id'],digest(bad)) is None
    from sqlalchemy import update
    from storage_models import AnalysisArtifact
    with repo._engine.begin() as connection:
        connection.execute(update(AnalysisArtifact).where(AnalysisArtifact.key==digest(ident)).values(payload={'tampered':True}))
    with pytest.raises(ValueError,match='hash mismatch'):
        repo.analysis_artifact(record['project_id'],digest(ident))
