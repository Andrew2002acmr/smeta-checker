from copy import deepcopy
from typing import Any

import pytest

from normative_claim_memory import ClaimMemory
from normative_position_check import AgentAnswer, CheckedItem


def setup():
    sources: list[dict[str, Any]] = [
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


def alias_setup():
    memory,sources,item=setup()
    sources[1].update(source_id='document-sha',page=19,label='Norm code 01',kb_identity={'sha256':'pinned'})
    memory.remember(AgentAnswer(summary='First',items=[item]),sources,{'method'})
    sources.append({**deepcopy(sources[1]),'id':'retrieved-again'})
    return memory,sources,item.model_copy(update=dict(project_source_ids=[],normative_source_ids=['retrieved-again']),deep=True)


def test_exact_same_normative_payload_under_new_handle_keeps_scope_edge_only():
    memory,sources,later=alias_setup()
    original=deepcopy(sources)
    events=[]
    memory.restore(AgentAnswer(summary='Later',items=[later]),sources,events)
    assert later.project_source_ids==['p'] and later.state=='confirmed'
    assert later.normative_source_ids==['retrieved-again'] and sources==original
    assert events[0]['current_normative_source_sha256']['retrieved-again']


@pytest.mark.parametrize('field,value',[
    ('text','Work'),('page',20),('source_id','other-document'),('origin','ocr_unverified'),
    ('kb_identity',{'sha256':'other-release'}),('label','Other norm'),
    ('classification_claim_ids',['other:claim']),('page',None),
])
def test_same_text_or_prefix_with_different_provenance_or_permission_is_not_alias(field,value):
    memory,sources,later=alias_setup()
    sources[-1][field]=value
    before=deepcopy(later)
    memory.restore(AgentAnswer(summary='Later',items=[later]),sources,[])
    assert later==before


@pytest.mark.parametrize('state',['needs_facts','possible_mismatch'])
def test_alias_does_not_restore_a_conclusion_over_new_model_doubt(state):
    memory,sources,later=alias_setup()
    later.state=state
    before=deepcopy(later)
    memory.restore(AgentAnswer(summary='Later',items=[later]),sources,[])
    assert later==before


def test_duplicate_source_handle_fails_closed():
    memory,sources,later=alias_setup()
    sources.append(deepcopy(sources[0]))
    memory.restore(AgentAnswer(summary='Later',items=[later]),sources,[])
    assert not later.project_source_ids
