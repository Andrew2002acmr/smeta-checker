"""Bounded follow-up receipts and dependency signals, without an LLM."""
from copy import deepcopy

from normative_evidence_followup import complete_followup, plan_for, preserve_completed
from normative_followup_outbox import geometry_revision
from test_normative_fact_states import fixture
from test_scoped_agent_evidence import payload


def test_search_plan_records_target_type_and_one_shared_budget():
    plan = plan_for(payload())
    assert plan["budget"] == dict(additional_model_calls_limit=3, max_actions=8, automatic_passes_limit=1)
    assert plan["completion_reason"] == "pending"
    assert plan["actions"][0]["goal"] == "card:quantity"
    assert plan["actions"][0]["expected_evidence_type"]
    assert plan["actions"][0]["budget"]["shared_pass"]


def test_search_completion_records_new_evidence_and_unresolved_obligation():
    data = payload()
    data["position_check"]["sources"] = [dict(id="old")]
    plan = plan_for(data)
    continuation = dict(review=dict(id="initial", payload=data), plan=plan)
    result = deepcopy(data["position_check"])
    result["sources"].append(dict(id="new"))
    result["parameter_cards"] = [dict(id="quantity", state="needs_facts", source_ids=["new"])]
    complete_followup(result, continuation)
    receipt = result["coverage"]["evidence_followup"]
    assert receipt["completion_reason"] == "completed_with_open_obligations"
    assert receipt["results"][0]["state"] == "needs_facts"
    assert receipt["results"][0]["new_source_ids"] == ["new"]
    assert receipt["automatic_retry_allowed"] is False
    assert plan_for(dict(position_check=result)) is None
    assert data["position_check"]["sources"] == [dict(id="old")]


def test_search_failure_preserves_facts_and_consumes_only_automatic_pass():
    data = payload()
    data["position_check"].update(sources=[dict(id="p", text="400 шт")],
        parameter_cards=[dict(id="quantity", state="confirmed", source_ids=["p"])],
        telemetry=dict(requests=2))
    data["position_check"]["evidence_trace"][0]["actions"] = [dict(
        key="claim:normative:applicability", family="norm_selection",
        action="retrieve_normative_or_project_basis", retry_fingerprint="norm")]
    continuation = dict(review=dict(id="initial", payload=data), plan=plan_for(data))
    result = preserve_completed(continuation,
        dict(summary="local injected technical failure", telemetry=dict(requests=1)))
    complete_followup(result, continuation)
    assert result["sources"] == data["position_check"]["sources"]
    assert result["parameter_cards"] == data["position_check"]["parameter_cards"]
    receipt = result["coverage"]["evidence_followup"]
    assert receipt["completion_reason"] == "technical_failure_initial_evidence_preserved"
    assert receipt["results"][0]["state"] == "technical_failure"
    assert plan_for(dict(position_check=result)) is None


def test_comparison_mismatch_is_a_completed_outcome_not_a_search_failure():
    data = payload()
    continuation = dict(review=dict(id="initial", payload=data), plan=plan_for(data))
    result = deepcopy(data["position_check"])
    result["parameter_cards"] = [dict(id="quantity", state="possible_mismatch", source_ids=["p"])]
    result["sources"] = [dict(id="p")]
    complete_followup(result, continuation)
    receipt = result["coverage"]["evidence_followup"]
    assert receipt["results"][0]["state"] == "possible_mismatch"
    assert receipt["completion_reason"] == "completed_with_validated_outcomes"


def geometry_payload():
    _, result = fixture("500")
    result.update(state="possible_mismatch", execution_state="complete")
    return dict(position_check=result, kb={}, vor_version_id="vor", position_key="same-position")


def test_mismatched_project_geometry_participates_in_revision_independently_of_comparison():
    data = geometry_payload()
    before = geometry_revision(data)
    matched = deepcopy(data)
    matched["position_check"]["parameter_cards"][0]["state"] = "confirmed"
    assert geometry_revision(matched) == before
    changed = deepcopy(data)
    changed["position_check"]["sources"][0]["text"] = changed["position_check"]["sources"][0]["text"].replace("400", "450")
    changed["position_check"]["parameter_proposals"][0].update(value="450", quote="количество 450 шт",
        scope_quote=changed["position_check"]["sources"][0]["text"])
    changed["position_check"]["parameter_cards"][0].update(found_value="450",
        source_anchors=[dict(source_id="p1", quote="количество 450 шт", rects=[])])
    assert geometry_revision(changed) != before


def test_unverified_scope_or_forged_anchor_does_not_become_geometry_evidence():
    data = geometry_payload()
    empty = deepcopy(data)
    empty["position_check"].update(parameter_cards=[], parameter_proposals=[], sources=[])
    for field in ("scope", "anchor", "quality"):
        damaged = deepcopy(data)
        if field == "scope":
            damaged["position_check"]["parameter_proposals"][0]["scope_state"] = "unresolved"
        elif field == "anchor":
            damaged["position_check"]["parameter_cards"][0]["source_anchors"][0]["quote"] = "invented"
        else:
            damaged["position_check"]["sources"][0].update(origin="ocr", numeric_verified=False)
        assert geometry_revision(damaged) == geometry_revision(empty)


def test_an_established_mismatch_does_not_block_search_for_other_missing_claims():
    data = payload()
    result = data["position_check"]
    result["state"] = "possible_mismatch"
    result["parameter_cards"] = [dict(id="quantity", state="possible_mismatch")]
    result["items"] = [dict(claim_ids=["normative:applicability"], state="needs_facts")]
    result["evidence_trace"][0]["actions"].append(dict(key="claim:normative:applicability",
        action="retrieve_normative_or_project_basis", retry_fingerprint="norm", family="norm_selection"))
    plan = plan_for(data)
    assert [a["key"] for a in plan["actions"]] == ["claim:normative:applicability"]
    result["items"][0]["state"] = "confirmed"
    assert plan_for(data) is None
