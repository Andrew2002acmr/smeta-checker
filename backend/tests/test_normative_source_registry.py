from copy import deepcopy

import pytest

from normative_source_registry import merge
from normative_source_registry import full_text_handle
from normative_evidence_followup import preserve_completed


def test_extended_normative_text_gets_new_identity_without_rewriting_old_proof():
    old='A'*2800
    full=old+' Additional condition'
    sources=[dict(id='agent:n:x',text=old)]
    handle=full_text_handle('agent:n:x',full)
    merge(sources,[dict(id=handle,text=full)])
    assert sources[0]['text']==old and len(sources)==2
    assert handle==full_text_handle('agent:n:x',full)
    assert handle!=full_text_handle('agent:n:x',old)


def test_resumed_context_does_not_duplicate_same_normative_sources():
    source = dict(
        id="methodology:p1", kind="normative", text="Literal full clause", page=1
    )
    sources = [source]
    merge(sources, [deepcopy(source), dict(id="p", kind="project", text="Geometry")])
    assert len(sources) == 2 and sources[0] == source


def test_same_handle_with_other_literal_source_is_rejected():
    with pytest.raises(ValueError, match="identity conflict"):
        merge([dict(id="n", text="First")], [dict(id="n", text="Other")])


def test_failed_additional_pass_preserves_verified_facts_without_promoting_verdict():
    previous = dict(
        execution_state="complete",
        state="needs_facts",
        pipeline_version=6,
        items=[dict(state="confirmed", claim_ids=["quantity"])],
        parameter_cards=[dict(id="quantity", state="confirmed")],
        sources=[dict(id="p", text="80 м2")],
        telemetry={"requests": 3},
        coverage={},
    )
    frozen = deepcopy(previous)
    failed = dict(
        summary="Technical search failure",
        telemetry={"requests": 1},
        coverage={"step": 2},
    )
    result = preserve_completed(
        dict(review=dict(id="initial", payload={"position_check": previous})), failed
    )
    assert (
        result["items"] == previous["items"]
        and result["parameter_cards"] == previous["parameter_cards"]
    )
    assert result["state"] == "needs_facts" and result["execution_state"] == "complete"
    assert (
        result["coverage"]["evidence_followup_failure"]["execution_state"]
        == "technical_failure"
    )
    assert (
        result["coverage"]["evidence_followup_failure"]["original_review_id"]
        == "initial"
    )
    assert result["telemetry"]["requests"] == 1 and previous == frozen


def test_failed_initial_answer_cannot_be_treated_as_completed_evidence():
    with pytest.raises(ValueError, match="Completed initial"):
        preserve_completed(
            dict(
                review={
                    "payload": {
                        "position_check": {"execution_state": "technical_failure"}
                    }
                }
            ),
            {},
        )
