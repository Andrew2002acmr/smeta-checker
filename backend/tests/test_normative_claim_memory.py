from copy import deepcopy

import pytest

from normative_claim_memory import ClaimMemory
from normative_position_check import AgentAnswer, CheckedItem


def setup():
    sources = [
        dict(id="p", kind="project", origin="native", text="Project construction"),
        dict(id="n", kind="normative", origin="native", text="Work norm"),
    ]
    item = CheckedItem(
        topic="Method",
        state="confirmed",
        claim_ids=["method"],
        explanation="Normed method for project work",
        project_source_ids=["p"],
        normative_source_ids=["n"],
    )
    memory = ClaimMemory()
    memory.remember(AgentAnswer(summary="First", items=[item]), sources, {"method"})
    return memory, sources, item


def test_confirmed_same_claim_retains_previously_validated_scope_citation():
    memory, sources, item = setup()
    later = item.model_copy(update={"project_source_ids": []}, deep=True)
    answer = AgentAnswer(summary="Later", items=[later])
    events = []
    memory.restore(answer, sources, events)
    assert later.project_source_ids == ["p"] and later.state == "confirmed"
    assert events[0]["current_original"]["project_source_ids"] == []
    assert events[0]["source_sha256"]


@pytest.mark.parametrize(
    "change", ["uncertain", "negative", "other_norm", "changed_source", "other_claim"]
)
def test_new_doubt_conflict_or_changed_provenance_is_never_overridden(change):
    memory, sources, item = setup()
    later = item.model_copy(update={"project_source_ids": []}, deep=True)
    if change == "uncertain":
        later.state = "needs_facts"
    if change == "negative":
        later.state = "possible_mismatch"
    if change == "other_norm":
        later.normative_source_ids = ["other"]
    if change == "changed_source":
        sources[0]["text"] = "Other construction"
    if change == "other_claim":
        later.claim_ids = ["normative:applicability"]
    before = deepcopy(later)
    memory.restore(AgentAnswer(summary="Later", items=[later]), sources, [])
    assert later == before
