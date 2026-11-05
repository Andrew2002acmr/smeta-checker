"""Persisted navigation receipts remain addressable after a follow-up resume."""
from copy import deepcopy
import json

import pytest

from normative_agent import EvidenceRequest
from normative_evidence_followup import resume_evidence
from normative_service import position_key
from test_normative_agent import toolkit
from test_scoped_agent_evidence import position


def resume_with(receipts):
    p = position()
    tools = toolkit()
    tools.sources.clear()
    data = dict(position_key=position_key(p), vor_version_id="vor", kb=dict(id="kb"),
        position_check=dict(sources=[], parameter_proposals=[], evidence_trace=receipts))
    resume_evidence(tools, dict(review=dict(id="initial", payload=data), plan=dict(actions=[])),
        dict(vor_version_id="vor", config=dict(pdf_versions={"PD": tools.version})), p, dict(id="kb"))
    return tools


def receipt():
    donor = toolkit()
    original = dict(engineering_entities=dict(rows=[dict(id=str(i), description="navigation " * 40) for i in range(30)]))
    reply = donor._archive_tool_result(original)
    return dict(request=dict(tool="search_project", query="construction"), result=reply), original


def test_real_resume_restores_full_archive_and_paginated_read_receipts():
    event, original = receipt()
    frozen = deepcopy(event)
    fresh = resume_with([event])
    handle = event["result"]["result_handle"]
    assert fresh.result_archive[handle] == original
    parts, offset = [], 0
    while True:
        reply = fresh.execute(EvidenceRequest(tool="read_source", source_id=handle, offset=offset))
        assert reply["navigation_only"] is True and reply["document_evidence"] is False
        parts.append(reply["text"])
        if reply["next_offset"] is None:
            break
        offset = reply["next_offset"]
    assert "".join(parts) == json.dumps(original, ensure_ascii=False)
    assert handle not in {s["id"] for s in fresh.sources}
    assert fresh.coverage["resumed_tool_results"]["imported"] == 1
    assert event == frozen
    # Even without the in-memory cache, the copied validated receipt survives.
    fresh.result_archive.clear()
    reply = fresh.execute(EvidenceRequest(tool="read_source", source_id=handle))
    assert "error" not in reply


@pytest.mark.parametrize("change", ["payload", "handle", "partial_read"])
def test_resume_does_not_restore_forged_or_partial_result_handles(change):
    event, _ = receipt()
    handle = event["result"]["result_handle"]
    if change == "payload":
        event["result"]["engineering_entities"]["rows"][0]["description"] = "changed"
    elif change == "handle":
        event["result"]["result_handle"] = "tool-result:" + "0" * 32
        handle = event["result"]["result_handle"]
    else:
        event["result"] = dict(result_handle=handle, text="partial", offset=0, next_offset=3000)
    fresh = resume_with([event])
    assert not fresh.result_archive
    assert fresh.execute(EvidenceRequest(tool="read_source", source_id=handle))["error"] == "unknown_tool_result"
    assert fresh.coverage["resumed_tool_results"]["rejected"] == 1


def test_duplicate_receipts_restore_one_archive_without_an_extra_tool_call():
    event, _ = receipt()
    fresh = resume_with([event, deepcopy(event)])
    assert len(fresh.result_archive) == 1
    assert len([e for e in fresh.events if e.get("kind") == "resumed_tool_result_v1"]) == 1
