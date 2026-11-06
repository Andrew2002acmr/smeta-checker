"""Offline cross-stage contracts, with synthetic evidence and no live model.

These tests exercise boundaries, not engineering accuracy or model quality.
No project row, named road standard, coefficient value or manufacturer is an
oracle. Domain adapters are disabled in the fact-flow contract below.
"""

from contextlib import closing
from copy import deepcopy
from unittest.mock import Mock
from typing import Any

import pymupdf
import pytest

from audit_core import PageTextIndex
from audit_models import VorPosition, PositionSource, SourceHeading
from normative_agent import EvidenceRequest, compact, run
from normative_embedded_context import packing_record
from normative_evidence_plan import build_work_plan, pending_actions
from normative_fact_ledger import build as fact_ledger
from normative_parameters import enrich
from normative_position_check import AgentAnswer
from normative_retrieval import DocumentSearch
from normative_verdict import finalize_state
from pdf_text import PDFTextSource
from test_normative_agent import toolkit


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def prohibited(*args, **kwargs):
        pytest.fail("Pipeline contract tests must not open a network connection")

    monkeypatch.setattr("socket.create_connection", prohibited)
    monkeypatch.setattr("socket.socket.connect", prohibited)


def open_review(sources) -> dict[str, Any]:
    return dict(
        sources=sources,
        items=[
            dict(
                claim_ids=["normative:applicability"],
                state="needs_facts",
                project_source_ids=[],
                normative_source_ids=["rule"],
                explanation="The scope relation still needs validation",
            )
        ],
        parameter_cards=[],
        coverage={},
    )


def normative_task(review):
    return next(
        a
        for a in build_work_plan({}, review, {})["actions"]
        if a["key"] == "claim:normative:applicability"
    )


def record(sources, *, sent=(), state="completed", partial=False) -> dict[str, Any]:
    supplied = dict(
        sources=[
            dict(
                s,
                text=s["text"][:12] if partial else s["text"],
                excerpt_truncated=partial,
                next_offset=12 if partial else None,
            )
            for s in sources
            if s["id"] in sent
        ]
    )
    value = packing_record(supplied, sources, "fixture-context")
    value["response_state"] = state
    return value


@pytest.mark.parametrize(
    ("state", "sent", "partial", "expected", "action"),
    [
        ("completed", (), False, "not_sent", "read_registered_source"),
        ("completed", ("rule",), True, "partial_text", "read_registered_source"),
        ("completed", ("rule",), False, "full_text", "validate_candidate"),
        ("prepared", ("rule",), False, "not_measured", "validate_candidate"),
        ("provider_failure", ("rule",), False, "not_measured", "validate_candidate"),
        ("invalid_schema", ("rule",), False, "not_measured", "validate_candidate"),
    ],
)
def test_registry_context_and_plan_are_distinct_stages(
    state, sent, partial, expected, action
):
    sources = [
        dict(
            id="rule",
            kind="normative",
            source_id="fictional-rule",
            page=1,
            origin="native",
            label="Fixture regulation",
            text="For substrate class Z use the specified process. Other substrates require a separate basis.",
        )
    ]
    review = open_review(sources)
    review["coverage"]["model_context_packing"] = [
        record(sources, sent=sent, state=state, partial=partial)
    ]
    original = deepcopy(review)
    plan = build_work_plan(dict(work_name="Wall coating", unit="м2"), review, {})
    task = next(
        a for a in plan["actions"] if a["key"] == "claim:normative:applicability"
    )
    assert task["model_delivery"]["source_states"] == {"rule": expected}
    assert task["action"] == action
    assert not task["model_delivery"]["comprehension_established"]
    assert not plan["policy"]["manual_missing_document_established"]
    assert review == original and review["items"][0]["state"] == "needs_facts"
    if action == "read_registered_source":
        assert task["read_requests"] == [
            dict(tool="read_source", source_id="rule", offset=12 if partial else 0)
        ]


def test_late_context_loss_is_not_hidden_by_an_earlier_complete_context():
    sources = [
        dict(
            id="rule", kind="normative", text="Complete condition with a qualification."
        )
    ]
    review = open_review(sources)
    review["coverage"]["model_context_packing"] = [
        record(sources, sent=("rule",)),
        record(sources),
    ]
    task = normative_task(review)
    assert task["action"] == "read_registered_source"
    assert task["model_delivery"]["source_states"]["rule"] == "not_sent"


def test_open_obligations_explain_missing_binding_from_the_verification_contract():
    from test_normative_context_packing import CAPTURES

    position = dict(CAPTURES[0]["metadata"]["position"])
    position["work_name"] = position["work"]
    equipment = "method:comment:equipment:1"
    note = "attribute:comment:separate_notes"
    matching = dict(checks=[dict(claim_id=claim, state="unknown") for claim in (equipment, note)])
    review = open_review([])
    review["parameter_cards"] = [dict(id="thickness", label="Толщина слоя", state="needs_facts")]
    plan = build_work_plan(position, review, matching)
    actions = {action["key"]: action for action in plan["actions"]}
    machine = actions[f"claim:{equipment}"]
    assert machine["state"] == "no_bound_evidence"
    assert machine["verification"]["property"] == "equipment_function"
    assert "функция или класс машины" in machine["reason"]
    assert "уплотнен" in machine["reason"].casefold()
    assert "обоснование совместимости с условиями ПД" in actions[f"claim:{note}"]["reason"]
    assert "Толщина слоя" in actions["card:thickness"]["reason"]
    assert not plan["policy"]["manual_missing_document_established"]


def test_legacy_changed_or_missing_sources_never_acquire_delivery_proof():
    sources = [
        dict(
            id="rule",
            kind="normative",
            text="Frozen literal condition",
            version_id="version-a",
        )
    ]
    review = open_review(sources)
    review["coverage"]["model_context_packing"] = [
        dict(supplied_source_ids=["rule"], truncated_previews=[])
    ]
    assert (
        normative_task(review)["model_delivery"]["source_states"]["rule"]
        == "not_measured"
    )
    review["coverage"]["model_context_packing"] = [record(sources, sent=("rule",))]
    sources[0]["version_id"] = "version-b"
    assert (
        normative_task(review)["model_delivery"]["source_states"]["rule"]
        == "current_evidence_not_measured"
    )
    review["sources"] = []
    task = normative_task(review)
    assert task["state"] == "source_reference_invalid"
    assert task["model_delivery"]["source_states"]["rule"] == "invalid_reference"


def test_same_delivery_with_new_stage_name_does_not_create_a_retry():
    sources = [dict(id="rule", kind="normative", text="A literal generic condition.")]
    review = open_review(sources)
    review["coverage"]["model_context_packing"] = [record(sources)]
    first = build_work_plan({}, review, {})
    tried = [a["retry_fingerprint"] for a in first["actions"]]
    review["coverage"]["model_context_packing"][0]["stage"] = "another-call-number"
    assert pending_actions(build_work_plan({}, review, {}), tried) == []
    review["coverage"]["model_context_packing"] = [record(sources, sent=("rule",))]
    assert pending_actions(build_work_plan({}, review, {}), tried)


def test_source_quality_gate_precedes_any_delivery_recommendation():
    sources = [
        dict(
            id="rule",
            kind="normative",
            origin="ocr_unreviewed",
            text="An unverified image reading.",
        )
    ]
    review = open_review(sources)
    review["coverage"]["model_context_packing"] = [record(sources)]
    assert normative_task(review)["action"] == "verify_source_quality"


def test_transport_attempt_is_recorded_separately_from_successful_schema():
    tools = toolkit()
    ask = Mock(side_effect=RuntimeError("offline transport fixture"))
    with pytest.raises(RuntimeError, match="offline transport fixture"):
        run(ask, None, None, "", {}, AgentAnswer, None, {"queries": []}, tools)
    assert tools.coverage["model_context_packing"][-1]["response_state"] == "failed"
    tools = toolkit()
    ask = Mock(return_value=AgentAnswer(summary="Synthetic protocol fixture", items=[]))
    run(ask, None, None, "", {}, AgentAnswer, None, {"queries": []}, tools)
    assert tools.coverage["model_context_packing"][-1]["response_state"] == "completed"


def test_native_pdf_snapshot_search_context_fact_ledger_and_publisher(
    tmp_path, monkeypatch
):
    # No domain-specific projection is allowed to contribute to this result.
    monkeypatch.setattr(
        "normative_material_search.specification_proof", lambda *args: None
    )
    monkeypatch.setattr(
        "normative_coefficient_policy.compaction_basis", lambda *args: None
    )
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(40, 40, 550, 220),
        "Room Beta. Wall coating.\nCoating area 17.25 m2.\n"
        "This is a synthetic project sheet used only to exercise preservation of native text, "
        "page identity, retrieval and source boundaries. It does not describe an actual construction project.",
    )
    with closing(PDFTextSource(doc, cache_dir=tmp_path / "cache")) as source:
        prepared = source.prepare_full()
    doc.close()
    index = PageTextIndex.from_snapshot(prepared.to_snapshot())
    hit = DocumentSearch(index.texts).search_page("coating")["hits"][0]
    assert hit["page"] == 1 and "17.25" in index.texts[0]
    tools = toolkit()
    tools.index = index
    tools.sources = []
    tools.snapshot_refs = []
    read = tools.execute(EvidenceRequest(tool="read_page", page=1))
    sid = read["source_id"]
    packed = compact(
        dict(tool_results=[dict(request=dict(tool="read_page", page=1), result=read)]),
        tools.sources,
    )
    assert (
        next(s for s in packed["sources"] if s["id"] == sid)["text"] == index.texts[0]
    )
    p = VorPosition(
        index=0,
        work_name="Wall coating",
        volume="17.25",
        unit="м2",
        position_source=PositionSource(
            document_sha256="b" * 64,
            format="xlsx",
            sheet="Specification",
            row=7,
            header_row=1,
            position_id="fixture-row-7",
            sections=[SourceHeading(address="A1", text="Room Beta")],
        ),
    )
    body = index.texts[0]
    review: dict[str, Any] = dict(
        pipeline_version=13,
        execution_state="complete",
        sources=tools.sources,
        items=[
            dict(
                topic="Area",
                state="confirmed",
                required=True,
                claim_ids=["quantity", "unit"],
                project_source_ids=[sid],
                normative_source_ids=[],
                explanation="Synthetic literal area",
            )
        ],
        parameter_proposals=[
            dict(
                parameter="quantity",
                operation="direct",
                value="17.25",
                unit="m2",
                source_id=sid,
                quote="Coating area 17.25 m2.",
                scope_quote=body,
                scope_state="verified",
            )
        ],
        evidence_trace=[],
    )
    enriched = enrich(p, review, {0: p}, {})
    ledger: dict[str, Any] = fact_ledger(p, enriched, {0: p}, {})
    assert (
        len(ledger["facts"]) == 1 and ledger["facts"][0]["subject_state"] == "verified"
    ), (
        [(c["state"], c["reason"]) for c in enriched["parameter_cards"]],
        [(f["literal_state"], f["issues"]) for f in ledger["facts"]],
    )
    assert ledger["facts"][0]["scope_id"] and ledger["facts"][0]["owner_position"] == 0
    matching = dict(
        status="ОК",
        checks=[dict(claim_id="quantity", required=True, state="review_required")],
    )
    result = finalize_state(enriched, matching, "fixture-position", position=p)
    # Project geometry alone must not publish a complete normative verdict.
    assert result["state"] == "needs_facts"
    assert (
        next(c for c in result["parameter_cards"] if c["id"] == "quantity")["state"]
        == "confirmed"
    )
    assert any(
        a["key"] == "claim:normative:applicability"
        for a in result["evidence_trace"][-1]["actions"]
    )
    changed = deepcopy(review)
    changed["parameter_proposals"][0]["scope_state"] = "unresolved"
    enriched = enrich(p, changed, {0: p}, {})
    unresolved_ledger: dict[str, Any] = fact_ledger(p, enriched, {0: p}, {})
    assert unresolved_ledger["facts"][0]["subject_state"] == "unresolved"
    assert (
        finalize_state(enriched, matching, "fixture-position", position=p)["state"]
        == "needs_facts"
    )


@pytest.mark.parametrize(
    "claim",
    ["quantity", "unit", "presence", "thickness", "input:area", "geometry:depth"],
)
def test_project_obligations_never_route_to_a_normative_lookup(claim):
    review = dict(sources=[], items=[dict(claim_ids=[claim], state="needs_facts")])
    task = next(
        a
        for a in build_work_plan({}, review, {})["actions"]
        if a["key"] == "claim:" + claim
    )
    assert task["family"] == "project_scope" and task["action"] == "retrieve_project"


@pytest.mark.parametrize(
    ("card_id", "role", "parameter", "unit"),
    [
        ("factor:8:1.41", "coefficient", "coefficient", ""),
        ("input:area", "project_value", "quantity", "м2"),
        ("unit_scale:source-x", "unit_scale", "unit_scale", "м2"),
    ],
)
def test_card_and_proposal_identities_join_by_fact_kind_without_value_selection(
    card_id, role, parameter, unit
):
    review = dict(
        sources=[],
        items=[],
        parameter_cards=[
            dict(id=card_id, role=role, state="needs_facts", declared_value="1.41")
        ],
    )
    before = build_work_plan({}, review, {})
    tried = [a["retry_fingerprint"] for a in before["actions"]]
    # A different numeric candidate must be tracked, never silently discarded
    # for not matching the workbook. This is discovery, not confirmation.
    review["sources"] = [
        dict(
            id="candidate",
            kind="project" if parameter == "quantity" else "normative",
            text="Value 77.2",
        )
    ]
    review["parameter_proposals"] = [
        dict(parameter=parameter, source_id="candidate", value="77.2", unit=unit)
    ]
    plan = build_work_plan({}, review, {})
    action = next(a for a in plan["actions"] if a["key"] == "card:" + card_id)
    assert (
        action["proposal_count"] == 1
        and action["state"] == "candidate_requires_validation"
    )
    assert action["source_ids"] == ["candidate"]
    assert action in pending_actions(plan, tried)
    assert review["parameter_cards"][0]["state"] == "needs_facts"
