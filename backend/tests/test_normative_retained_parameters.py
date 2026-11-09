"""Omitted replay literals survive without selecting or certifying scope."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from analysis_replay import digest
from normative_agent import EvidenceRequest
from normative_evidence_followup import resume_evidence
from normative_position_check import AgentAnswer, retain_literal_parameters
from normative_service import position_key
from normative_source_identity import shared_source
from test_normative_agent import sid, toolkit
from test_scoped_agent_evidence import position


def observation(value="400", source_id="p1", scope_state="unresolved"):
    text = f"Конструкция К-1, количество {value} шт"
    source = dict(id=source_id, kind="project", origin="native", version_id="version", page=1, text=text)
    proposal = dict(parameter="quantity", source_id=source_id, value=value, unit="шт",
        quote=f"количество {value} шт", scope_quote=text, scope_state=scope_state)
    return source, proposal


def tools_for(proposals):
    return SimpleNamespace(parameter_evidence={digest(p): p for p in proposals}, events=[],
        execute=Mock(side_effect=AssertionError("Retention cannot acquire evidence")))


def test_digest_keyed_omission_retains_all_conflicting_unknown_scope_candidates():
    a, first = observation()
    b, second = observation("500", "p2")
    tools = tools_for([first, second])
    before = deepcopy([first, second])
    answer = AgentAnswer(summary="Different aspect", items=[])
    retain_literal_parameters(answer, [a, b], tools)
    assert [p.value for p in answer.parameters] == ["400", "500"]
    assert all(p.scope_state == "unresolved" for p in answer.parameters)
    assert [first, second] == before
    tools.execute.assert_not_called()


def test_a_current_unresolved_read_or_explicit_selection_is_not_overwritten():
    a, old = observation(scope_state="verified")
    b, selected = observation("500", "p2")
    answer = AgentAnswer(summary="New scope uncertainty", items=[], parameters=[selected])
    before = deepcopy(answer)
    retain_literal_parameters(answer, [a, b], tools_for([old]))
    assert answer == before
    answer.parameters[0].scope_state = "verified"
    before = deepcopy(answer)
    retain_literal_parameters(answer, [a, b], tools_for([old]))
    assert answer == before


def test_new_scope_dispute_keeps_the_literal_without_restoring_ownership():
    source, old = observation(scope_state="verified")
    answer = AgentAnswer(summary="Scope disputed", items=[dict(topic="Принадлежность", state="needs_facts",
        explanation="Другой участок не исключён", claim_ids=["quantity"], project_source_ids=["p1"],
        normative_source_ids=[])])
    retain_literal_parameters(answer, [source], tools_for([old]))
    assert answer.parameters[0].value == "400"
    assert answer.parameters[0].scope_state == "unresolved"
    assert answer.items[0].state == "needs_facts"


@pytest.mark.parametrize("change", ["quote", "quality", "alias", "source_kind", "unit"])
def test_saved_bad_literals_are_not_restored(change):
    source, old = observation()
    if change == "quote":
        old["quote"] = "invented 400 шт"
    elif change == "quality":
        source.update(origin="ocr", numeric_verified=False)
    elif change == "alias":
        source = shared_source(source, 0)
        old["source_id"] = source["id"]
        source["page"] = 2
    elif change == "source_kind":
        source["kind"] = "normative"
    else:
        old["unit"] = "т"
    answer = AgentAnswer(summary="Unrelated fix", items=[])
    retain_literal_parameters(answer, [source], tools_for([old]))
    assert not answer.parameters


@pytest.mark.parametrize("scope_state", ["verified", "unresolved"])
def test_actual_resume_memory_restores_omitted_sum_without_an_llm(scope_state):
    donor = toolkit()
    donor.execute(EvidenceRequest(tool="read_table", table_id="t1"))
    computed = donor.execute(EvidenceRequest(tool="sum_cells", operand_ids=[sid("c1"), sid("c2")], unit="м2"))
    proposal = dict(parameter="quantity", operation="sum", operand_source_ids=[sid("c1"), sid("c2")],
        source_id=computed["source_id"], value=computed["value"], unit="м2",
        quote=computed["equation"], scope_quote=computed["equation"], scope_state=scope_state)
    p = position()
    data = dict(position_key=position_key(p), vor_version_id="vor", kb=dict(id="kb"),
        position_check=dict(sources=deepcopy(donor.sources), parameter_proposals=[proposal]))
    fresh = toolkit()
    fresh.sources.clear()
    resume_evidence(fresh, dict(review=dict(id="r", payload=data), plan=dict(actions=[])),
        dict(vor_version_id="vor", config=dict(pdf_versions={"PD": fresh.version})), p, dict(id="kb"))
    assert list(fresh.parameter_evidence) != ["quantity"]
    answer = AgentAnswer(summary="Normative condition remains open", items=[])
    retain_literal_parameters(answer, fresh.sources, fresh)
    assert len(answer.parameters) == 1
    assert answer.parameters[0].operation == "sum"
    assert answer.parameters[0].value == "69.12"
    assert answer.parameters[0].scope_state == scope_state


def test_duplicate_memory_keys_do_not_duplicate_the_same_observation():
    source, proposal = observation()
    tools = tools_for([proposal])
    tools.parameter_evidence["quantity"] = deepcopy(proposal)
    answer = AgentAnswer(summary="Other aspect", items=[])
    retain_literal_parameters(answer, [source], tools)
    assert len(answer.parameters) == 1
