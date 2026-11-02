"""Isolated position checks use real norms, synthetic project/model data."""
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
import unittest

from normative_corpus import Corpus
from normative_position_check import _excerpt, validate_items, CheckedItem
import test_normative_integration as fixtures

ROAD = Path(__file__).resolve().parents[3] / "knowledge-base/road-norms"


def test_road_profile_is_separate_and_technical_clause_is_searchable():
    kb = Corpus(ROAD)
    hits = kb.search("27-04-003-06")
    assert hits[0]["code"] == "27-04-003-06"
    assert all(v["family"] == "ГЭСН" and v["code"].startswith(("27-", "47-")) for v in hits)
    assert not kb.search("08-02-148-02")
    clause = kb.search("1.27.18", kind="ALL")[0]
    assert clause["label"] == "1.27.18"
    assert "18 см" in clause["text"]
    assert clause["citations"][0]["physical_page"] == 2
    material = kb.search("ГОСТ Р 70458-2022 щебеночно песчаные смеси", kind="ALL")
    assert material and all(v["designation"] == "ГОСТ Р 70458-2022" for v in material)
    assert not kb.search("ГОСТ Р 999999-2099 смесь", kind="ALL")


def test_excerpt_limits_keep_header_and_footer():
    value = "HEADER" + "x"*10000 + "FOOTER"
    for size in (800, 1600, 2800):
        result = _excerpt(value, size)
        assert len(result) <= size
        assert result.startswith("HEADER") and result.endswith("FOOTER")


def test_project_binding_is_explicit_and_does_not_change_other_projects():
    from normative_service import profile_for
    mapping = json.loads((ROAD / "project-bindings.json").read_text(encoding="utf-8"))
    with patch.dict(os.environ, {"NORMATIVE_ROAD_KB_ROOT": str(ROAD)}):
        assert profile_for(dict(project_id=next(iter(mapping)))) == "roads"
        assert profile_for(dict(project_id="unbound-test-project")) == "electrical"


def test_unknown_source_cannot_confirm_a_check():
    item = CheckedItem(topic="quantity", state="confirmed", explanation="test", project_source_ids=["p1"], normative_source_ids=["invented"])
    result = validate_items([item], [dict(id="p1", kind="project"), dict(id="n1", kind="normative")])
    assert result[0]["state"] == "needs_facts"
    assert result[0]["normative_source_ids"] == []


class PositionCheckTests(unittest.TestCase):
    _open_app = fixtures.NormativeApiTests._open_app
    _upload = fixtures.NormativeApiTests._upload
    _upload_pair = fixtures.NormativeApiTests._upload_pair
    _project_id = fixtures.NormativeApiTests._project_id
    _run = fixtures.NormativeApiTests._run
    setUp = fixtures.NormativeApiTests.setUp
    def test_background_layer_calls_model_once_and_preserves_matching(self):
        from normative_background import review_position
        from normative_position_check import CheckAnswer
        repo = self.app.state.context.repository
        context = self.app.state.context
        context.settings.llm_api_key = 'synthetic-only'
        context.settings.llm_model = 'synthetic-model'
        context.settings.llm_base_url = 'https://anymodel.org/v1'
        context.settings.allowed_llm_hosts = ('anymodel.org',)
        context = replace(context, client_factory=lambda **_: SimpleNamespace(close=lambda: None))
        before = repo.load_run(UUID(self.run))['indexed_results']
        repo.update_run_config(UUID(self.run), {'normative_after_match': True})
        response = CheckAnswer(summary='Synthetic automatic normative review', items=[CheckedItem(
            topic='Source check', state='needs_facts', explanation='Synthetic response only.', project_source_ids=['p1'], normative_source_ids=['n0'])])
        with patch.dict(os.environ, {'NORMATIVE_ROAD_KB_ROOT': str(ROAD)}), patch('normative_service.profile_for', return_value='roads'), patch('normative_position_check._ask', return_value=response) as ask:
            first = review_position(context, UUID(self.run), 0)
            assert first['review_id']
            assert review_position(context, UUID(self.run), 0) == {'skipped': 'already_reviewed'}
            assert ask.call_count == 1
        assert repo.load_run(UUID(self.run))['indexed_results'] == before

    def test_only_one_position_is_reviewed_and_batch_result_survives(self):
        repo = self.app.state.context.repository
        before = repo.load_run(UUID(self.run))["indexed_results"]
        calls = []

        def complete(**kwargs):
            calls.append(kwargs)
            data = json.loads(kwargs["messages"][1]["content"])
            norm = next(s for s in data["sources"] if s["kind"] == "normative")
            answer = dict(summary="Синтетическая проверка одной позиции; исходный прогон сохранён.", items=[dict(topic="Площадь", state="confirmed", explanation="Сверено по переданным источникам.", project_source_ids=["p1"], normative_source_ids=[norm["id"]])])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))], usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150))

        context = self.app.state.context
        context.settings.llm_api_key = "synthetic-key"
        repo.update_run_config(UUID(self.run), {'normative_after_match': True})
        pending = self.client.get(f'/api/v1/audits/{self.run}/rows').json()['rows'][0]
        assert pending['queue'] == 'pending'
        assert self.client.get(f'/api/v1/audits/{self.run}/reports/word').status_code == 409
        context.settings.llm_model = "synthetic-model"
        context.settings.llm_base_url = "https://anymodel.org/v1"
        context.settings.allowed_llm_hosts = ("anymodel.org",)
        self.app.state.context = replace(context, client_factory=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)), close=lambda: None))
        with patch.dict(os.environ, {"NORMATIVE_ROAD_KB_ROOT": str(ROAD)}), patch("normative_service.profile_for", return_value="roads"):
            result = self.client.post(self.base + "/check", json=dict(pages=[1], question="Проверить 1.27.18"))
            assert result.status_code == 200, result.text
            data = result.json()
            assert data["kind"] == "check"
            check = data["payload"]["position_check"]
            assert len(calls) == 1
            assert check["telemetry"]["requests"] == 0  # The versioned local follow-up adds no paid call.
            raw_id = UUID(check['telemetry']['warnings'][0].rsplit(': ',1)[1].rstrip('.'))
            raw = repo.normative_review(UUID(self.run),0,raw_id)['payload']['position_check']
            assert raw['telemetry']['requests'] == 1
            assert raw['telemetry']['usage']['total'] == 150
            assert check["items"][0]["state"] == "confirmed"
            assert self.client.get(self.base).json()["latest_check"]["id"] == data["id"]
            published = self.client.get(f'/api/v1/audits/{self.run}/rows').json()['rows'][0]
            assert published['queue'] == 'review'  # A generic confirmed aspect cannot clear all claims.
            assert self.client.get(f'/api/v1/audits/{self.run}/reports/word').status_code == 200
            assert repo.load_run(UUID(self.run))["indexed_results"] == before
            assert self.client.post(self.base + "/check", json=dict(pages=[9999])).status_code == 409
            assert len(calls) == 1
            from normative_background import review_position
            repo.update_run_config(UUID(self.run), {"normative_after_match": True})
            assert review_position(self.app.state.context, UUID(self.run), 0) == {"skipped": "already_reviewed"}
            assert len(calls) == 1
