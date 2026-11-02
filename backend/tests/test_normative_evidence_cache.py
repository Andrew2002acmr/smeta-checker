from copy import deepcopy

import pytest

from normative_evidence_cache import cache_identity, source_fingerprint


def test_only_explicit_pure_projection_modules_are_exempt_from_source_contract(tmp_path):
    for name in ['normative_verdict.py','normative_workbook_arithmetic.py','normative_agent.py','pdf_tables.py']:
        (tmp_path/name).write_text('original',encoding='utf-8')
    before=source_fingerprint(tmp_path)
    (tmp_path/'normative_verdict.py').write_text('new verdict',encoding='utf-8')
    (tmp_path/'normative_workbook_arithmetic.py').write_text('new arithmetic',encoding='utf-8')
    assert source_fingerprint(tmp_path)==before
    (tmp_path/'normative_agent.py').write_text('new source selection',encoding='utf-8')
    assert source_fingerprint(tmp_path)!=before
    (tmp_path/'normative_agent.py').write_text('original',encoding='utf-8')
    (tmp_path/'pdf_tables.py').write_text('new recognition',encoding='utf-8')
    assert source_fingerprint(tmp_path)!=before


def test_cache_survives_verdict_version_only_and_does_not_modify_original_binding():
    binding=dict(project_id='project',algorithm='old',inputs=dict(pdf='v1',vor='v1',kb='k1'))
    original=deepcopy(binding)
    first=cache_identity(binding,{'source_pipeline':'same'})
    assert cache_identity({**binding,'algorithm':'new'},{'source_pipeline':'same'})==first
    assert binding==original


@pytest.mark.parametrize('field',['pdf','vor','kb','human_revision','model','related_rows','request'])
def test_any_changed_evidence_dependency_invalidates_cache(field):
    binding=dict(project_id='project',algorithm='old',inputs={field:'old'})
    first=cache_identity(binding,{'source_pipeline':'same'})
    binding['inputs'][field]='new'
    assert cache_identity(binding,{'source_pipeline':'same'})!=first


def test_source_generation_change_and_project_boundary_invalidate_cache():
    binding=dict(project_id='project',algorithm='old',inputs={})
    first=cache_identity(binding,{'source_pipeline':'one'})
    assert cache_identity(binding,{'source_pipeline':'two'})!=first
    assert cache_identity({**binding,'project_id':'other'},{'source_pipeline':'one'})!=first
