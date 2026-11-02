"""Real read-only corpus + isolated PostgreSQL; no paid model or network calls."""
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from uuid import UUID

import pytest
from normative_corpus import Corpus, condition
import test_api as fixtures
import test_workspace as workspace_fixtures

ROOT = Path(__file__).resolve().parents[3] / "knowledge-base/electrical-norms"


def test_three_valued_rules_and_unit_scale():
    kb = Corpus(ROOT)
    facts = dict(placement="laid_pipes_blocks_boxes", voltage_kv="10", mass_kg_per_m="1.8", quantity="250", unit="м", height_m="12")
    hits = kb.search("08-02-148-02", facts=facts)
    norm = hits[0]
    assert norm["code"] == "08-02-148-02"
    assert all(h["family"] == "ГЭСНм" and h["code"].startswith("08-") for h in hits)
    unit = next(c for c in norm["checks"] if c["label"] == "Единица ВОР")
    assert unit["norm_quantity"] == "2.5"
    assert any(r["state"] == "needs_facts" and "included_height_exceeded" in r["missing_facts"] for r in norm["rules"])
    assert condition({"field": "included_height_exceeded", "op": "eq", "value": True}, {})[0] is None
    facts.update(unit="км", quantity="0.25", included_height_exceeded=False)
    norm = kb.search("08-02-148-02", facts=facts)[0]
    assert Decimal(next(c for c in norm["checks"] if c["label"] == "Единица ВОР")["norm_quantity"]) == Decimal("2.5")
    assert all(r["state"] == "not_applicable" for r in norm["rules"] if "высоте" in r["title"])
    with kb.connect() as db:
        with pytest.raises(Exception, match="readonly"):
            db.execute("DELETE FROM sources")
    with pytest.raises(ValueError, match="Изменился"):
        Corpus(ROOT, expected_sha="0" * 64)


def test_current_pointer_cannot_escape_root(tmp_path):
    (tmp_path / "CURRENT.json").write_text(json.dumps(dict(database="../escape.sqlite")))
    with pytest.raises(ValueError, match="путь"):
        Corpus(tmp_path)


@pytest.mark.parametrize("stage", ["normative-query-plan", "normative-rank"])
def test_context_prompt_masking(stage):
    import audit_tracing as tracing
    token = tracing._mask.set(True)
    try:
        result = tracing._maybe_redact(stage, "input", [{"role": "user", "content": "project text"}])
        assert result == [{"role": "user", "content": "[redacted prompt]"}]
    finally:
        tracing._mask.reset(token)


class NormativeApiTests(unittest.TestCase):
    _open_app = fixtures.ApiTests._open_app
    _upload = fixtures.ApiTests._upload
    _upload_pair = fixtures.ApiTests._upload_pair
    _project_id = fixtures.ApiTests._project_id
    _run = workspace_fixtures.WorkspaceTests._run

    def setUp(self):
        from unittest.mock import patch
        self.env = patch.dict("os.environ", {"NORMATIVE_KB_ROOT": str(ROOT)})
        self.env.start()
        self.addCleanup(self.env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.client, self.app = self._open_app(tmp.name)
        vor, pdf = self._upload_pair(self.client)
        self.run = self._run(self.client, self.app, vor, pdf)
        self.base = f"/api/v1/audits/{self.run}/positions/0/normative"
        self.request = dict(query="08-02-148-02", facts=dict(unit="м", quantity="250", placement="laid_pipes_blocks_boxes", voltage_kv="10", mass_kg_per_m="1.8", height_m="12"))

    def test_search_selection_pdf_and_immutable_machine_result(self):
        repo = self.app.state.context.repository
        original = repo.load_run(UUID(self.run))["indexed_results"]
        response = self.client.post(self.base + "/search", json=self.request)
        self.assertEqual(response.status_code, 200, response.text)
        search = response.json()
        norm = search["payload"]["candidates"][0]
        self.assertEqual(norm["code"], "08-02-148-02")
        self.assertTrue(search["payload"]["requires_engineer_review"])
        self.assertTrue(search["payload"]["kb"]["sha256"])
        selected = self.client.post(self.base + "/selection", json=dict(search_id=search["id"], norm_ids=[norm["id"]], comment="Сверено инженером"))
        self.assertEqual(selected.status_code, 200, selected.text)
        state = self.client.get(self.base).json()
        self.assertEqual(state["selection"]["payload"]["selected_ids"], [norm["id"]])
        self.assertEqual(repo.load_run(UUID(self.run))["indexed_results"], original)
        citation = norm["citations"][0]
        pdf = self.client.get(self.base + f"/{search['id']}/sources/{citation['source_id']}", headers={"Range": "bytes=0-1023"})
        self.assertEqual(pdf.status_code, 206, pdf.text[:100] if pdf.status_code != 206 else "")
        self.assertTrue(pdf.content.startswith(b"%PDF-"))
        other = self.client.get(self.base.replace("/positions/0/", "/positions/9/") + f"/{search['id']}/sources/{citation['source_id']}")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(self.client.post(self.base + "/selection", json=dict(search_id=search["id"], norm_ids=["forged"])).status_code, 409)
        invalid = {**self.request, "facts": {"voltage_kv": "NaN"}}
        self.assertEqual(self.client.post(self.base + "/search", json=invalid).status_code, 422)

    def test_candidate_mismatch_does_not_become_a_project_error(self):
        request = {**self.request, "facts": {**self.request["facts"], "mass_kg_per_m": "8"}}
        response = self.client.post(self.base + "/search", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["payload"]["candidates"][0]["status"], "possible_mismatch")
        self.app.state.context.settings.api_token = "test-only"
        self.assertEqual(self.client.get(self.base).status_code, 401)

    def test_context_expands_renamed_query_and_keeps_only_real_candidates(self):
        from types import SimpleNamespace
        kb = Corpus(ROOT)
        norm_id = kb.search("08-02-148-02")[0]["id"]
        calls = []

        def complete(**kwargs):
            calls.append(kwargs)
            content = (dict(interpretation="Кабельная линия в трубах", queries=["08-02-148-02"],
                missing_facts=["Проверить способ прокладки по ПД"]) if len(calls) == 1 else
                dict(recommendations=[dict(id="invented-source", reason="Не должно попасть в результат"),
                    dict(id=norm_id, reason="Норма для кабеля в проложенных трубах; сверить условия и редакцию.")]))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120))

        context = self.app.state.context
        context.settings.llm_api_key = "synthetic-context-key"
        context.settings.llm_model = "synthetic-model"
        context.settings.llm_base_url = "https://anymodel.org/v1"
        context.settings.allowed_llm_hosts = ("anymodel.org",)
        from dataclasses import replace
        self.app.state.context = replace(context, client_factory=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)), close=lambda: None))
        response = self.client.post(self.base + "/search", json={**self.request, "query": "Хочу провести линию до щита", "mode": "context"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["payload"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(call["messages"][1]["content"]) <= 60000 for call in calls))
        supplied = json.loads(calls[0]["messages"][1]["content"])
        self.assertIn("Copper cable", supplied["vor"]["work"])
        self.assertEqual(supplied["question"], "Хочу провести линию до щита")
        self.assertEqual(result["candidates"][0]["code"], "08-02-148-02")
        self.assertEqual(result["candidates"][0]["checks"][-1]["norm_quantity"], "2.5")
        self.assertEqual(result["facts"]["quantity"], "250")
        self.assertEqual(result["context_analysis"]["usage"]["total"], 240)
        self.assertEqual(result["context_analysis"]["requests"], 2)
        self.assertTrue(result["context_analysis"]["warnings"])
        self.assertTrue(result["candidates"][0]["context_reason"])
        self.assertEqual(result["mode"], "context")
        self.assertEqual(self.client.get(self.base).json()["latest_search"]["payload"]["context_analysis"], result["context_analysis"])

    def test_context_provider_failure_preserves_local_search_without_retries(self):
        from types import SimpleNamespace
        context = self.app.state.context
        context.settings.llm_api_key = "synthetic-context-key"
        context.settings.llm_model = "synthetic-model"
        context.settings.llm_base_url = "https://anymodel.org/v1"
        context.settings.allowed_llm_hosts = ("anymodel.org",)
        calls = []

        def fail(**kwargs):
            calls.append(kwargs)
            raise TimeoutError("Do not expose provider details")

        from dataclasses import replace
        self.app.state.context = replace(context, client_factory=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)), close=lambda: None))
        response = self.client.post(self.base + "/search", json={**self.request, "mode": "context"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["payload"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["candidates"][0]["code"], "08-02-148-02")
        self.assertTrue(result["context_analysis"]["warnings"])
        self.assertNotIn("Do not expose", response.text)
