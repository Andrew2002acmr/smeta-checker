"""Explicit prior runs supply rechecked project literals, never prior verdicts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call
from uuid import UUID

import pytest

from normative_fact_ledger import shared
from normative_parameters import build_cards, enrich
from normative_scope_graph import scope
from normative_service import position_key
from test_current_material_geometry import current_fixture

CURRENT = "11111111-1111-4111-8111-111111111111"
PRIOR = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def fixture() -> tuple[SimpleNamespace, dict, dict, dict, dict, dict]:
    record, raw, positions, _ = current_fixture()
    record.update(id=CURRENT, project_id="project")
    record["config"].update(position_inputs=[p.model_dump(mode="json") for p in positions.values()],
        normative_fact_source_run_ids=[PRIOR], normative_kb_pin={"release": "new"})
    previous = deepcopy(record)
    previous.update(id=PRIOR)
    previous["config"]["normative_fact_source_run_ids"] = []
    previous["config"]["normative_kb_pin"] = {"release": "old"}
    relation = dict(parent={"index": 0}, children=[])
    checked = enrich(positions[1], raw, positions, relation)
    checked["items"].append(dict(topic="Old norm verdict", state="confirmed", required=True,
        claim_ids=["normative:applicability"], project_source_ids=["p"], normative_source_ids=["own"],
        explanation="Old verdict is not evidence"))
    row = dict(id="old-review", kind="check", payload=dict(vor_version_id="vor",
        position_key=position_key(positions[1]), kb={"release": "old"}, position_check=checked,
        work_relations=relation))
    graph = dict(scope_id=scope(positions[1]), positions=[dict(index=i, scope_id=scope(p), work=p.work_name)
        for i, p in positions.items()])
    repo = Mock()
    repo.load_run.return_value = previous
    repo.normative_reviews.side_effect = lambda run, index: [row] if str(run) == PRIOR and index == 1 else []
    return SimpleNamespace(repository=repo), record, previous, positions, graph, row


def test_external_own_position_reuses_literals_without_any_parent_review():
    context, record, previous, positions, graph, row = fixture()
    before = deepcopy(row)
    result = shared(context, record, positions, 1, graph)
    assert [c["proposal"]["parameter"] for c in result["candidates"]] == ["quantity", "thickness"]
    assert {c["owner_position"] for c in result["candidates"]} == {1}
    assert {c["source_run_id"] for c in result["candidates"]} == {PRIOR}
    assert all(c["proposal"]["scope_state"] == c["engineer_scope_for_current_position"] == "unresolved"
        for c in result["candidates"])
    assert all(s["kind"] == "project" for s in result["sources"])
    assert not {"items", "state", "parameter_cards", "kb"} & result.keys()
    context.repository.load_run.assert_called_once_with(UUID(PRIOR))
    assert not any(str(c.args[0]) == PRIOR and c.args[1] == 0
        for c in context.repository.normative_reviews.call_args_list[:2])
    assert row == before
    # Re-importing a literal does not establish its scope for a fresh answer.
    rebuilt = build_cards(positions[1], dict(pipeline_version=24, sources=result["sources"], items=[],
        parameter_proposals=[c["proposal"] for c in result["candidates"]]), positions,
        dict(parent={"index": 0}))
    assert all(c["state"] == "needs_facts" for c in rebuilt)


@pytest.mark.parametrize("change", ["quality", "scope", "value", "quote", "source", "pin"])
def test_green_cards_cannot_override_changed_literal_quality_or_parameters(change):
    context, record, _, positions, graph, row = fixture()
    r = row["payload"]["position_check"]
    if change == "quality":
        next(s for s in r["sources"] if s["id"] == "p").update(origin="ocr", numeric_verified=False)
    elif change == "pin":
        next(s for s in r["sources"] if s["id"] == "p")["version_id"] = "wrong-pdf"
    else:
        for p in r["parameter_proposals"]:
            if p["parameter"] not in {"quantity", "thickness"}:
                continue
            if change == "scope":
                p["scope_state"] = "unresolved"
            elif change == "value":
                p["value"] = "999"
            elif change == "quote":
                p["quote"] = "invented 999"
        if change == "source":
            next(s for s in r["sources"] if s["id"] == "p")["text"] = "Другой участок 999м3"
    assert all(c["state"] == "confirmed" for c in r["parameter_cards"] if c["id"] in {"quantity", "thickness"})
    bundle = shared(context, record, positions, 1, graph)
    assert not bundle["sources"] and not bundle["candidates"]


@pytest.mark.parametrize("change", ["project", "vor", "pdf", "bank", "missing_bank"])
def test_run_binding_mismatch_rejects_before_any_review_import(change):
    context, record, previous, positions, graph, _ = fixture()
    if change == "project":
        previous["project_id"] = "another-project"
    elif change == "vor":
        previous["vor_version_id"] = "another-vor"
    elif change == "pdf":
        previous["config"]["pdf_versions"] = {"PD": "another-pdf"}
    elif change == "bank":
        previous["config"]["position_inputs"][1]["volume"] = "999"
    else:
        record["config"].pop("position_inputs")
        previous["config"].pop("position_inputs")
    with pytest.raises(ValueError):
        shared(context, record, positions, 1, graph)
    context.repository.normative_reviews.assert_not_called()


def test_changed_normative_pin_does_not_transfer_norms_or_prior_confirmed_verdict():
    context, record, previous, positions, graph, _ = fixture()
    assert record["config"]["normative_kb_pin"] != previous["config"]["normative_kb_pin"]
    bundle = shared(context, record, positions, 1, graph)
    assert bundle["candidates"] and all(s["kind"] == "project" for s in bundle["sources"])
    assert all(c["proposal"]["parameter"] in {"quantity", "thickness"} for c in bundle["candidates"])
    assert "Old norm verdict" not in str(bundle)
    assert "normative_source_ids" not in str(bundle)


def test_current_calls_then_all_external_own_positions_before_external_other_nodes():
    context, record, previous, positions, graph, row = fixture()
    other = deepcopy(previous)
    other["id"] = OTHER
    second = deepcopy(row)
    second["id"] = "other-review"
    for s in second["payload"]["position_check"]["sources"]:
        if s["kind"] == "project":
            s["page"] = 2
    record["config"]["normative_fact_source_run_ids"] = [PRIOR, OTHER]
    context.repository.load_run.side_effect = lambda uid: {PRIOR: previous, OTHER: other}[str(uid)]
    # A fully usable other-node donor must not consume the budget before the
    # explicit second prior run's own-position evidence has been considered.
    parent_row = deepcopy(row)
    parent_row["payload"]["position_key"] = position_key(positions[0])
    parent_row["payload"]["work_relations"] = {}
    context.repository.normative_reviews.side_effect = lambda run, index: (
        [] if str(run) == CURRENT else [second if str(run) == OTHER else row] if index == 1 else [parent_row])
    bundle = shared(context, record, positions, 1, graph)
    assert len(bundle["candidates"]) == 4 and bundle["truncated"]
    assert [c["source_run_id"] for c in bundle["candidates"]] == [PRIOR, PRIOR, OTHER, OTHER]
    assert all(c["owner_position"] == 1 for c in bundle["candidates"])
    assert context.repository.normative_reviews.call_args_list == [call(CURRENT, 0), call(PRIOR, 1), call(OTHER, 1)]


@pytest.mark.parametrize("configured", ["not-a-list", [PRIOR] * 4])
def test_prior_run_list_remains_explicit_and_bounded(configured):
    context, record, _, positions, graph, _ = fixture()
    record["config"]["normative_fact_source_run_ids"] = configured
    with pytest.raises(ValueError):
        shared(context, record, positions, 1, graph)
    context.repository.load_run.assert_not_called()
    context.repository.normative_reviews.assert_not_called()


def test_without_config_the_repository_is_not_searched_for_prior_runs():
    context, record, _, positions, graph, _ = fixture()
    record["config"].pop("normative_fact_source_run_ids")
    assert not shared(context, record, positions, 1, graph)["candidates"]
    context.repository.load_run.assert_not_called()


def test_current_run_sources_have_priority_and_the_existing_four_candidate_cap():
    context, record, _, positions, graph, row = fixture()
    another = positions[0].model_copy(deep=True, update={"index": 2})
    another.position_source.row = 12
    positions[2] = another
    graph["positions"].append(dict(index=2, scope_id=scope(another), work=another.work_name))
    bank = [p.model_dump(mode="json") for p in positions.values()]
    record["config"]["position_inputs"] = bank
    context.repository.load_run.return_value["config"]["position_inputs"] = deepcopy(bank)
    def reviews(run, index):
        donor = deepcopy(row)
        donor["payload"]["position_key"] = position_key(positions[index])
        donor["payload"]["work_relations"] = {}
        return [donor]
    context.repository.normative_reviews.side_effect = reviews
    bundle = shared(context, record, positions, 1, graph)
    assert len(bundle["candidates"]) == 4 and bundle["truncated"]
    assert all(c["source_run_id"] == CURRENT for c in bundle["candidates"])
    assert context.repository.normative_reviews.call_args_list == [call(CURRENT, 0), call(CURRENT, 2)]


def test_external_own_sources_follow_current_sources_before_external_neighbours():
    context, record, _, positions, graph, row = fixture()
    parent = deepcopy(row)
    parent["payload"]["position_key"] = position_key(positions[0])
    parent["payload"]["work_relations"] = {}
    context.repository.normative_reviews.side_effect = lambda run, index: [parent] if str(run) == CURRENT else [row]
    bundle = shared(context, record, positions, 1, graph)
    assert [c["source_run_id"] for c in bundle["candidates"]] == [CURRENT, CURRENT, PRIOR, PRIOR]
    assert [c["owner_position"] for c in bundle["candidates"]] == [0, 0, 1, 1]
    assert context.repository.normative_reviews.call_args_list == [call(CURRENT, 0), call(PRIOR, 1)]


def test_empty_position_banks_cannot_authorize_external_reuse():
    context, record, previous, positions, graph, _ = fixture()
    record["config"]["position_inputs"] = []
    previous["config"]["position_inputs"] = []
    with pytest.raises(ValueError, match="complete position input bank"):
        shared(context, record, positions, 1, graph)
    context.repository.normative_reviews.assert_not_called()
