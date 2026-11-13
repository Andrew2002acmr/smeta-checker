"""Codex mode through HTTP and a real PostgreSQL queue, with synthetic documents."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from threading import Event
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from api import ApiContext, create_app
from scripts.codex_worker import _work_one
from test_durable_tasks import pipeline as pipeline
from workspace_api import _factor_positions


def test_coefficient_scope_keeps_other_workbook_sections_out():
    def position(index, section):
        return {'index': index, 'work_name': 'ЩПС', 'position_type': 'Материал', 'formula': '431.61*0.12*1.26',
                'comment': 'Купл-1,26', 'calculation': {'dependencies': [{'value': '1.26'}]},
                'position_source': {'sheet': 'ВР-6', 'sections': [{'address': section, 'text': 'Улица'}]}}

    record = {'config': {'position_inputs': [position(1, 'A17'), position(3, 'A17'), position(5, 'A52')]},
              'indexed_results': [{'position_index': index, 'result': {'group': f'Улица / Основание / Материал слоя {layer}'}}
                                  for index, layer in [(1, 1), (3, 2), (5, 1)]]}
    assert [item['index'] for item in _factor_positions(record, 1, False)] == [1]
    assert [item['index'] for item in _factor_positions(record, 1, True)] == [1, 3]


def test_codex_normative_profiles_pin_current_release_independently(monkeypatch, tmp_path):
    from codex_worker import pin_normative_release
    from storage_blobs import FileBlobStore

    blobs = FileBlobStore(tmp_path / 'blobs')
    for profile, variable, name in (
        ('roads', 'CODEX_AUDIT_NORMATIVE_DIR', 'ГЭСН 27'),
        ('electrical', 'CODEX_AUDIT_ELECTRICAL_NORMATIVE_DIR', 'ГЭСНм 08'),
    ):
        root = tmp_path / profile
        release = root / 'releases' / 'pinned'
        release.mkdir(parents=True)
        (root / 'CURRENT.json').write_text(json.dumps({'database': 'releases/pinned/knowledge.sqlite'}))
        payload = name.encode()
        (root / 'source.pdf').write_bytes(payload)
        (release / 'manifest.json').write_text(json.dumps({'sources': [{
            'designation': name, 'blob_path': 'source.pdf', 'sha256': sha256(payload).hexdigest(),
        }]}))
        monkeypatch.setenv(variable, str(root))
    monkeypatch.delenv('CODEX_AUDIT_NORMATIVE_MANIFEST', raising=False)
    monkeypatch.delenv('CODEX_AUDIT_ELECTRICAL_NORMATIVE_MANIFEST', raising=False)
    road = pin_normative_release(blobs, 'roads')
    electrical = pin_normative_release(blobs, 'electrical')
    assert road['release'] != electrical['release']
    assert [item['filename'] for item in electrical['documents']] == ['ГЭСНм 08.pdf']


def _codex_run(client, project_id, vor_id, pdf_id, *, subagents=True):
    created = client.post("/api/v1/audits", json={
        "project_id": str(project_id), "vor_version_id": str(vor_id),
        "pdf_version_ids": [str(pdf_id)], "mode": "codex", "codex_subagents": subagents,
    })
    assert created.status_code == 202, created.text
    return created.json()["run_id"]


def _client(pipeline):
    context = ApiContext(
        settings=pipeline.context.settings, repository=pipeline.repo,
        blob_store=pipeline.context.blob_store,
    )
    return TestClient(create_app(context))


def test_codex_queue_persists_conclusions_and_exports(pipeline, monkeypatch, tmp_path):
    p = pipeline
    monkeypatch.setenv("CODEX_AUDIT_ENABLED", "1")
    monkeypatch.setenv("CODEX_AUDIT_NORMATIVE_DIR", str(tmp_path))
    (tmp_path / "normative.pdf").write_bytes(p.pdf)
    client = _client(p)
    pdf_id = p.versions["project.pdf"].id
    run_id = _codex_run(client, p.run.project_id, p.run.vor_version_id, pdf_id)
    root = f"/api/v1/audits/{run_id}"
    queued = client.get(f"{root}/progress").json()
    assert queued["mode"] == "codex"
    assert queued["codex_stage"] == "queued"
    assert queued["total"] == 1
    assert p.repo.reserve_deliveries() == []

    def synthetic_cli_output(context, record, *, cancelled, publish):
        assert not cancelled()
        assert publish("running", {"input_tokens": None})
        position = record["config"]["position_inputs"][0]
        norm = record["config"]["codex_normative"]["documents"][0]
        empty = {
            "status": "not_applicable", "parameter": "Scope", "vor_value": None,
            "source_value": None, "unit": None, "calculation": None, "deviation": None,
            "conclusion": "Outside scope", "sources": [], "missing_facts": None,
        }
        conclusion = {
            "position_index": position["index"], "sheet": position["position_source"]["sheet"],
            "row": position["position_source"]["row"], "work_name": position["work_name"],
            "group": "Cable",
            "project": {**empty, "status": "confirmed", "parameter": "Cable length",
                        "vor_value": "45", "source_value": "45", "unit": "m",
                        "sources": [{"document_id": str(pdf_id), "kind": "project",
                                     "page": 1, "sheet": None, "row": None, "quote": "Copper cable 45 m."}]},
            "normative": {**empty, "sources": [{"document_id": norm["id"], "kind": "normative",
                                               "page": 1, "sheet": None, "row": None, "quote": "Copper cable 45 m."}]},
            "quantities": empty,
            "parameter_findings": [{"direction": "project", "finding": {**empty,
                "status": "needs_facts", "parameter": "Installation condition",
                "missing_facts": {"required": "Installation plan", "reason": "Verify route", "established": "Length"},
            }}],
        }
        from codex_contract import AuditConclusion, result_metrics

        validated = AuditConclusion.model_validate({
            "project_id": str(record["project_id"]), "vor_version_id": str(record["vor_version_id"]),
            "positions": [conclusion],
        })
        return [conclusion], {
            "metrics": result_metrics(validated), "events": [],
            "model": "synthetic-test-model", "cli_version": "synthetic-test-version",
        }

    monkeypatch.setattr("codex_worker.execute_codex_run", synthetic_cli_output)
    assert _work_one(p.context, Event())
    assert p.repo.run_status(p.run.id) == "running"
    summary = client.get(f"{root}/summary").json()
    assert summary["run"]["status"] == "done"
    assert summary["run"]["codex_metrics"]["processed_positions"] == 1
    assert summary["run"]["direction_counts"]["project"]["needs_facts"] == 1
    card_response = client.get(f"{root}/positions/0/workspace")
    assert card_response.status_code == 200, card_response.text
    card = card_response.json()
    assert card["row"]["codex_conclusion"]["project"]["status"] == "confirmed"
    assert card["evidence"][0]["version_id"] == str(pdf_id)
    norm_id = card["row"]["codex_conclusion"]["normative"]["sources"][0]["document_id"]
    assert client.get(f"{root}/codex-sources/{norm_id}").content == p.pdf
    clarification = f'{root}/positions/0/codex-clarification'
    assert client.post(clarification, json={'basis': 'Project supplement', 'finding_index': 0}).status_code == 409
    child = client.post(clarification, json={'basis': 'Project supplement', 'finding_index': 3})
    assert child.status_code == 200, child.text
    assert client.post(clarification, json={'basis': 'Project supplement', 'finding_index': 3}).json() == child.json()
    child_id = UUID(child.json()['run_id'])
    saved = p.repo.load_run(child_id, include_results=False)['config']
    assert saved['codex_finding_clarification']['requested_basis'] == 'Installation plan'
    assert saved['codex_finding_clarification']['parameter'] == 'Installation condition'
    assert len(saved['position_inputs']) == 1
    assert p.repo.run_pinned_versions(child_id) == p.repo.run_pinned_versions(UUID(run_id))
    assert p.repo.cancel_codex_run(child_id)
    for artifact in ("word", "metrics.json", "performance_report.md", "audit_results.json", "execution_events.jsonl"):
        assert client.get(f"{root}/reports/{artifact}").status_code == 200
    other_id = _codex_run(client, p.run.project_id, p.run.vor_version_id, pdf_id, subagents=False)
    assert _work_one(p.context, Event())
    comparison = client.get(f"{root}/codex-comparison", params={"other_run_id": other_id})
    assert comparison.status_code == 200, comparison.text
    assert [run["subagents"] for run in comparison.json()["runs"]] == [True, False]
    assert all(run["verified_sample_quality"] is None for run in comparison.json()["runs"])
    different_id = _codex_run(client, p.run.project_id, p.run.vor_version_id, pdf_id, subagents=False)
    p.repo.update_run_config(UUID(different_id), {"codex_normative": {
        "release": "different", "documents": [{"sha256": "another digest"}],
    }})
    p.repo.finish_run(UUID(different_id), "done")
    assert client.get(f"{root}/codex-comparison", params={"other_run_id": different_id}).status_code == 409


def test_codex_cancel_and_resume_keep_the_pins(pipeline, monkeypatch, tmp_path):
    p = pipeline
    monkeypatch.setenv("CODEX_AUDIT_ENABLED", "1")
    monkeypatch.setenv("CODEX_AUDIT_NORMATIVE_DIR", str(tmp_path))
    (tmp_path / "normative.pdf").write_bytes(p.pdf)
    client = _client(p)
    run_id = _codex_run(client, p.run.project_id, p.run.vor_version_id, p.versions["project.pdf"].id)
    root = f"/api/v1/audits/{run_id}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: p.repo.claim_codex_run(lease_seconds=60), range(2)))
    claim, = [result for result in claims if result is not None]
    rid, lease, _ = claim
    assert rid == UUID(run_id)
    assert client.post(f"{root}/cancel").json()["status"] == "cancelled"
    assert not p.repo.heartbeat_codex_run(rid, lease, lease_seconds=60)
    assert client.post(f"{root}/resume").status_code == 409
    assert p.repo.complete_codex_run(
        rid, lease, results=[], error="cancelled", metadata={}, artifact_payloads=lambda *_: {},
    )
    original = p.repo.load_run(rid)["config"]["codex_normative"]
    assert client.post(f"{root}/resume").json()["codex_stage"] == "queued"
    assert p.repo.load_run(rid)["config"]["codex_normative"] == original
    with pytest.raises(KeyError):
        p.repo.load_position_job(rid, 0)
