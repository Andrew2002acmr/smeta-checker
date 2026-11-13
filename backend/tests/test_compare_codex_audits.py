import copy
from types import SimpleNamespace

import pytest

from scripts.compare_codex_audits import compare_runs, markdown


def _conclusion(status="confirmed"):
    return {
        "status": status,
        "parameter": "Area",
        "vor_value": "10",
        "source_value": "10",
        "unit": "m2",
        "calculation": None,
        "deviation": "0",
        "conclusion": "Finding",
        "sources": [{
            "document_id": "design", "kind": "project", "page": 1,
            "sheet": None, "row": None, "quote": "Area 10 m2",
        }],
        "missing_facts": None,
    }


def _run(mode, normative_hash="b" * 64):
    multi = mode == "codex_multi"
    return {
        "id": mode, "status": "done", "project_id": "project", "vor_version_id": "vor",
        "vor_sha256": "a" * 64,
        "config": {
            "mode": "codex", "codex_subagents": multi, "model": "model", "codex_cli_version": "1",
            "codex_normative": {"release": "test", "documents": [{"sha256": normative_hash}]},
            "codex_metrics": {"total_duration_ms": 700 if multi else 1000, "subagents_started": 3 if multi else 0},
            "position_inputs": [{"index": 0, "position_source": {"sheet": "VOR", "row": 1}}],
        },
        "indexed_results": [{"position_index": 0, "result": {
            "schema_version": "codex-conclusion-v1", "position_index": 0, "sheet": "VOR",
            "row": 1, "work_name": "Layer", "group": "Pavement",
            "project": _conclusion("confirmed" if multi else "mismatch"),
            "normative": _conclusion("not_applicable"),
            "quantities": _conclusion("not_applicable"),
        }}],
    }


@pytest.fixture
def compared_runs():
    return {mode: _run(mode) for mode in ("codex_multi", "codex_single")}


@pytest.fixture
def repository():
    return SimpleNamespace(run_pinned_versions=lambda _: {"design.pdf": "design"})


def test_report_distinguishes_throughput_from_independent_quality(compared_runs, repository):
    reference = {
        "project_id": "project", "vor_version_id": "vor", "reviewer": "Verifier",
        "samples": [{
            "position_index": 0, "direction": "project", "expected_status": "confirmed",
            "basis": "Page 1 of the pinned drawing",
            "run_checks": {"codex_multi": {"citations_correct": [True, False, None]}},
        }],
    }
    report = compare_runs(repository, compared_runs, reference)
    assert report["runs"]["codex_multi"]["subagents_launched"] == 3
    assert report["runs"]["codex_multi"]["cost"] is None
    assert report["reference_quality"]["codex_multi"]["confirmation_precision"]["share"] == 1
    assert report["reference_quality"]["codex_multi"]["correct_citation_samples"]["share"] == 0.5
    assert report["reference_quality"]["codex_single"]["false_mismatches"] == 1
    assert report["reference_quality"]["codex_single"]["confirmation_precision"]["share"] is None
    assert "-30.0%" in markdown(report)
    assert compare_runs(repository, compared_runs)["reference_quality"] is None


@pytest.mark.parametrize("change,pattern", [
    (lambda record: record.update(vor_version_id="different"), "различаются"),
    (lambda record: record["config"].update(model="different"), "одинаковая модель"),
    (lambda record: record["config"]["codex_normative"]["documents"][0].update(sha256="c" * 64), "различаются"),
    (lambda record: record["config"]["position_inputs"][0].update(index=2), "неполный"),
    (lambda record: record["config"]["position_inputs"][0]["position_source"].update(sheet="Other"), "различаются"),
    (lambda record: record['config']['codex_metrics'].update(audit_protocol_version='other'), 'версия методики'),
    (lambda record: record['config'].update(codex_coefficient_clarification={'basis': 'New input'}), 'уточнения'),
])
def test_report_refuses_noncomparable_runs(change, pattern, compared_runs, repository):
    change(compared_runs["codex_single"])
    with pytest.raises(ValueError, match=pattern):
        compare_runs(repository, compared_runs)


def test_report_refuses_existing_pipeline_without_pinned_normative_release(compared_runs, repository):
    existing = copy.deepcopy(compared_runs["codex_single"])
    existing["config"] = {"position_inputs": existing["config"]["position_inputs"]}
    compared_runs["existing"] = existing
    with pytest.raises(ValueError, match="закреплённого нормативного выпуска"):
        compare_runs(repository, compared_runs)
