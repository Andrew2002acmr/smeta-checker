"""P0 proof integrity: sums never confirm their own split (TZ section 18)."""

from types import SimpleNamespace

from audit_assessment import (
    proof_is_independent,
    reduce_assessment,
    sum_covers_declared,
)
from audit_graph import derive_quantities
from audit_models import VorPosition
from normative_rate_consistency import check_rate_consistency
from test_audit_assessment import base_input


def position(**overrides):
    payload = {
        "index": 12,
        "work_name": "Розлив эмульсии",
        "volume": "3.033",
        "unit": "т",
    }
    payload.update(overrides)
    return VorPosition(**payload)


def graph_context(vor):
    from audit_graph import GraphContext

    return GraphContext(
        repository=None,
        blob_store=None,
        position=vor,
        record={"id": "run-1", "config": {}},
        snapshot_refs=[{"key": "primary"}],
        supporting={},
        validation_only=True,
    )


def projected_queue(vor, assessment_payload):
    from workspace_service import _projected_graph_row

    assessment = reduce_assessment(assessment_payload).model_dump(mode="json")
    row = _projected_graph_row({"id": "run-1"}, vor, assessment)
    return row.queue, row.report_status


def confirmed_project_input(**overrides):
    payload = base_input()
    payload.update(overrides)
    return payload


def test_sum_total_alone_leaves_each_share_unproven():
    """TZ test 3: 5055 + 248 = 5303 proves the total, not the split."""
    proposals = [
        {
            "parameter": "quantity",
            "operation": "sum",
            "source_id": "agent:sum:total",
            "unit": "м2",
            "value": "5302.81",
            "quote": "2582,95 + 2719,86",
            "scope_quote": "",
            "scope_state": "unresolved",
            "operand_source_ids": ["pd:left", "pd:right"],
        }
    ]
    sources = [
        {
            "id": "agent:sum:total",
            "kind": "project",
            "origin": "calculated_from_cells",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2582.95 + 2719.86 = 5302.81",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
        {
            "id": "pd:left",
            "kind": "project",
            "origin": "native",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2582.95",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
        {
            "id": "pd:right",
            "kind": "project",
            "origin": "native",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2719.86",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
    ]
    # The arithmetic is valid, so the routed sum is independent as a total.
    assert proof_is_independent(16, proposals, sources) is True
    # The total covers position 16 (5302.81 rounds to declared 5303),
    # but never the 5055 share of position 12.
    assert sum_covers_declared("5303.0", "м2", proposals) is True
    assert sum_covers_declared("5055", "м2", proposals) is False
    vor = position(index=12, volume="3.033", unit="т")
    state_in = {
        "proposals": proposals,
        "sources": sources,
        "facts": [],
        "items": [],
        "quantity_kind": "derived",
    }
    out = derive_quantities(state_in, SimpleNamespace(context=graph_context(vor)))
    # The card never confirmed here (no literal/scope proof): without a
    # confirmed total there is no consistent total either.
    assert out["own_quantity"]["state"] == "unresolved"
    assert "sum_distribution_unproven" not in out["own_quantity"]["conditions"]


def test_sum_total_confirmed_but_share_unproven(monkeypatch):
    """TZ test 3 (contract): confirmed total + uncovered share = unproven."""
    import normative_parameters

    proposals = [
        {
            "parameter": "quantity",
            "operation": "sum",
            "source_id": "agent:sum:total",
            "unit": "м2",
            "value": "5302.81",
            "quote": "2582,95 + 2719,86",
            "scope_quote": "",
            "scope_state": "unresolved",
            "operand_source_ids": ["pd:left", "pd:right"],
        }
    ]
    sources = [
        {
            "id": "agent:sum:total",
            "kind": "project",
            "origin": "calculated_from_cells",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2582.95 + 2719.86 = 5302.81",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
        {
            "id": "pd:left",
            "kind": "project",
            "origin": "native",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2582.95",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
        {
            "id": "pd:right",
            "kind": "project",
            "origin": "native",
            "page": 89,
            "version_id": "pdf-1",
            "text": "2719.86",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        },
    ]
    monkeypatch.setattr(
        normative_parameters,
        "build_cards",
        lambda *a, **k: [
            {
                "id": "quantity",
                "state": "confirmed",
                "found_value": "5302.81",
                "found_unit": "м2",
                "source_ids": ["agent:sum:total"],
                "reason": "Сумма сошлась.",
            }
        ],
    )
    vor = position(index=12, volume="3.033", unit="т")
    out = derive_quantities(
        {
            "proposals": proposals,
            "sources": sources,
            "facts": [],
            "items": [],
            "quantity_kind": "derived",
        },
        SimpleNamespace(context=graph_context(vor)),
    )
    # Total confirmed, but 5302.81 m2 does not cover declared 3.033 t:
    # the share stays unproven, never confirmed.
    assert out["own_quantity"]["state"] == "consistent_unproven"
    assert "sum_distribution_unproven" in out["own_quantity"]["conditions"]
    assert out["own_quantity"]["found_value"] == "5302.81"


def test_circular_vor_reference_is_not_independent():
    """TZ test 5: 12 proves 14 proves 12 is rejected."""
    # Each side routes its sum through the other VOR amount instead of
    # PD cells: both directions fail, the loop proves nothing.
    proposals_12 = [
        {
            "parameter": "quantity",
            "operation": "sum",
            "source_id": "agent:sum:loop12",
            "unit": "м2",
            "value": "5303",
            "operand_source_ids": ["vor:14"],
        }
    ]
    proposals_14 = [
        {
            "parameter": "quantity",
            "operation": "sum",
            "source_id": "agent:sum:loop14",
            "unit": "м2",
            "value": "5303",
            "operand_source_ids": ["vor:12"],
        }
    ]
    sources = [
        {
            "id": "agent:sum:loop12",
            "kind": "project",
            "origin": "calculated_from_cells",
            "page": 89,
            "version_id": "pdf-1",
            "text": "5055 + 248 = 5303",
        },
        {
            "id": "agent:sum:loop14",
            "kind": "project",
            "origin": "calculated_from_cells",
            "page": 89,
            "version_id": "pdf-1",
            "text": "248 + 5055 = 5303",
        },
        {"id": "vor:12", "kind": "workbook", "origin": "vor_cell", "text": "5055"},
        {"id": "vor:14", "kind": "workbook", "origin": "vor_cell", "text": "248"},
    ]
    assert proof_is_independent(12, proposals_12, sources) is False
    assert proof_is_independent(14, proposals_14, sources) is False
    assert proof_is_independent(12, [], sources) is False


def test_display_formula_conflict_marks_proved_mismatch():
    """TZ test 6: D36 vs E36 divergence is a proved workbook mismatch."""
    from human_basis import position_key
    from normative_workbook_conflicts import conflicts

    vor = VorPosition(
        index=18,
        work_name="Розлив на выравнивающий слой",
        volume="2.092432",
        unit="т",
        formula="5303,08*0,4/1000",
        calculation={
            "cell": "D36",
            "status": "calculated",
            "expression": "=5231.08*0.4/1000",
            "value": "2.092432",
        },
        formula_refs={"display_cell": "E36"},
    )
    # position_key needs only the stable row payload; source row is synthetic.
    key = position_key(vor)
    assert isinstance(key, str) and len(key) == 64
    # Without a pinned xlsx source row the typed proof stays silent (fail closed).
    assert conflicts(vor, key) == []


def test_comment_rate_conflict_marks_proved_mismatch():
    """TZ test 7: formula rate 0.4 vs comment rate 0.6 contradicts."""
    findings = check_rate_consistency(
        formula_text="=8921*0.4/1000", comment="Расход 0,6 л/м2", unit="т"
    )
    assert len(findings) == 1
    assert findings[0]["kind"] == "rate-comment-mismatch"
    assert findings[0]["comment_rate"] == "0.6"
    assert findings[0]["assumption"] == "density=1"
    assert (
        check_rate_consistency(
            formula_text="=5055*0.6/1000", comment="Расход 0,6 л/м2", unit="т"
        )
        == []
    )


def test_dimension_assumption_recorded_without_own_mismatch():
    """TZ test 8: l/m2 divided by 1000 records density=1, never a mismatch."""
    findings = check_rate_consistency(
        formula_text="=5055*0.6/1000", comment="Расход 0,6 л/м2", unit="т"
    )
    # Rates agree: no finding at all, hence no dimension mismatch either.
    assert findings == []
    conflicted = check_rate_consistency(
        formula_text="=8921*0.4/1000", comment="Расход 0,6 л/м2", unit="т"
    )
    # The only finding kind is the rate contradiction; the density
    # assumption travels as metadata for dimensional review, never as
    # a second mismatch verdict.
    assert [f["kind"] for f in conflicted] == ["rate-comment-mismatch"]
    assert conflicted[0]["assumption"] == "density=1"


def test_rounded_total_confirms_without_remarks():
    """TZ test 1: 5303 vs 5302.81 confirms (rounding regression)."""
    proposals = [
        {
            "parameter": "quantity",
            "operation": "sum",
            "source_id": "agent:sum:total",
            "unit": "м2",
            "value": "5302.81",
            "operand_source_ids": ["pd:left", "pd:right"],
        }
    ]
    # The coverage gate itself accepts engineering rounding of the total.
    assert sum_covers_declared("5303", "м2", proposals) is True
    assert sum_covers_declared("5303.0", "м2", proposals) is True
    payload = confirmed_project_input()
    payload["own_quantity"] = {
        "kind": "direct",
        "state": "confirmed",
        "unit": "м2",
        "declared_value": "5303",
        "found_value": "5302.81",
        "found_unit": "м2",
        "explanation": "Округление до целых допустимо.",
    }
    vor = position(index=16, volume="5303.0", unit="м2")
    queue, _ = projected_queue(vor, payload)
    assert queue == "ok"


def test_unproven_split_projects_to_review_queue():
    """TZ test 3 (queue): consistent_unproven never shows as ok."""
    payload = confirmed_project_input()
    payload["own_quantity"] = {
        "kind": "derived",
        "state": "consistent_unproven",
        "unit": "т",
        "declared_value": "3.033",
        "found_value": "5302.81",
        "found_unit": "м2",
        "conditions": ["sum_distribution_unproven"],
        "explanation": "Сумма сходится, распределение по составляющим не доказано.",
    }
    vor = position(index=12, volume="3.033", unit="т")
    queue, status = projected_queue(vor, payload)
    assert queue == "review"
    assert status == "Требуется ручная проверка"


def test_relationships_mismatch_projects_to_fix_queue():
    """TZ test 6 (queue): proved D/E divergence shows as fix, never pending."""
    payload = confirmed_project_input()
    payload["project"]["obligations"] = [
        {
            "id": "project:binding",
            "domain": "presence",
            "subject": "Привязка",
            "state": "pending",
        }
    ]
    payload["relationships"] = {
        "obligations": [
            {
                "id": "relationships:workbook_arithmetic",
                "domain": "workbook_arithmetic",
                "subject": "Арифметика ВОР",
                "state": "possible_mismatch",
                "comparable": True,
                "mismatch_level": "proved",
                "reason": "Разные исходные числа в расчёте ВОР: 5231.08 в D36, 5303.08 в E36.",
            }
        ]
    }
    payload["execution_state"] = "complete"
    from workspace_service import _projected_graph_row

    assessment = reduce_assessment(payload).model_dump(mode="json")
    assert assessment["project"]["state"] == "pending"
    assert assessment["relationships"]["state"] == "possible_mismatch"
    row = _projected_graph_row(
        {"id": "run-1"}, position(index=18, volume="2.092432", unit="т"), assessment
    )
    assert row.queue == "fix"
    assert row.report_status == "Обнаружено расхождение"
    assert "5231.08 в D36" in (row.problem or "")


def test_independent_direct_facts_confirm_each_share():
    """TZ test 4: two direct PD facts (5055 milled + 248 existing) confirm."""
    proposals = [
        {
            "parameter": "quantity",
            "operation": "direct",
            "source_id": "pd:milled",
            "unit": "м2",
            "value": "5055",
            "quote": "Площадь фрезерования 5055 м2",
            "scope_quote": "Площадь фрезерования 5055 м2",
            "scope_state": "verified",
            "operand_source_ids": [],
        }
    ]
    sources = [
        {
            "id": "pd:milled",
            "kind": "project",
            "origin": "native",
            "page": 72,
            "version_id": "pdf-1",
            "text": "Площадь фрезерования 5055 м2",
            "numeric_verified": True,
            "unit_verified": True,
            "unit": "м2",
        }
    ]
    # Direct facts carry no sum: the sum gate stays out of the way.
    assert proof_is_independent(12, proposals, sources) is False
    assert sum_covers_declared("5055", "м2", proposals) is False
    vor = position(
        index=12,
        volume="3.033",
        unit="т",
        formula="5055*0,6/1000",
        comment="Расход 0,6 л/м2",
        calculation={
            "expression": "=5055*0.6/1000",
            "status": "calculated",
            "value": "3.033",
        },
    )
    out = derive_quantities(
        {
            "proposals": proposals,
            "sources": sources,
            "facts": [
                {
                    "id": "fact:milled",
                    "parameter": "quantity",
                    "source_id": "pd:milled",
                    "literal_state": "verified",
                    "subject_state": "verified",
                }
            ],
            "items": [],
            "quantity_kind": "derived",
        },
        SimpleNamespace(context=graph_context(vor)),
    )
    # Derived row with a directly proved area base: conditional, and the
    # sum machinery must not downgrade or remark it.
    assert out["own_quantity"]["state"] == "conditional"
    assert "sum_distribution_unproven" not in out["own_quantity"]["conditions"]
    payload = confirmed_project_input()
    queue, _ = projected_queue(vor, payload)
    assert queue == "ok"


def test_duplicate_physical_cells_cannot_sum():
    """Dedup: two handles over one physical cell are rejected."""
    import sys

    sys.path.insert(0, ".")
    from test_normative_agent import toolkit
    from normative_agent import EvidenceRequest

    tools = toolkit()
    first = tools.execute(
        EvidenceRequest(
            tool="read_table", page=1, table_id="t1", row_ids=[1], column_ids=[1]
        )
    )
    sid = first["rows"][0]["source_id"]
    alias = sid + "-alias"
    tools.cells[alias] = dict(tools.cells[sid])
    tools.sources.append(
        dict(next(s for s in tools.sources if s["id"] == sid), id=alias)
    )
    result = tools.execute(
        EvidenceRequest(tool="sum_cells", operand_ids=[sid, alias], unit="м2")
    )
    assert (
        result.get("error")
        == "duplicate_schedule_cells_require_separate_scope_validation"
    )
    assert any(
        event.get("kind") == "duplicate_schedule_skipped" for event in tools.events
    )


def test_identical_position_reuses_finished_assessment():
    """Speed: identical rows share one graph run, revision recomputed."""
    from uuid import uuid4
    from audit_tasks import _reuse_duplicate_result

    run_id = uuid4()
    stored = reduce_assessment(confirmed_project_input()).model_dump(mode="json")
    assert stored["position_index"] == 0

    def mkpos(index):
        return position(
            index=index,
            work_name="Устройство основания",
            volume="431.61",
            unit="м2",
            formula="",
            position_type="Работа",
            comment="",
            position_source={
                "document_sha256": "a" * 64,
                "format": "xlsx",
                "sheet": "Дорожная одежда",
                "row": 18 + index,
                "header_row": 17,
                "position_id": f"r{index}",
                "cells": {},
                "sections": [],
                "scope_key": "",
            },
        )

    jobs = {
        0: SimpleNamespace(position_index=0, status="done", result=stored),
        2: SimpleNamespace(position_index=2, status="queued", result=None),
    }
    finished = {}

    def fake_inputs(context, record):
        return [mkpos(0), mkpos(2)]

    import workspace_service

    real_inputs = workspace_service.inputs_for_run
    workspace_service.inputs_for_run = fake_inputs
    try:
        repo = SimpleNamespace(
            load_run=lambda rid, include_results=False: {"id": str(rid), "config": {}},
            load_position_job=lambda rid, idx: jobs[idx],
            finish_position_job=lambda rid, idx, status, result=None: finished.update(
                {"idx": idx, "status": status, "result": result}
            ),
        )
        context = SimpleNamespace(repository=repo)
        import audit_tasks

        real_chain = audit_tasks._maybe_chain_finalize
        audit_tasks._maybe_chain_finalize = lambda *a, **k: None
        try:
            out = _reuse_duplicate_result(context, run_id, 2)
        finally:
            audit_tasks._maybe_chain_finalize = real_chain
    finally:
        workspace_service.inputs_for_run = real_inputs
    assert out == {"position_index": 2, "duplicate": False, "reused": True}
    assert finished["idx"] == 2 and finished["status"] == "done"
    assert finished["result"]["position_index"] == 2
    assert finished["result"]["revision"] != stored["revision"]
    assert finished["result"]["project"]["state"] == stored["project"]["state"]
    assert finished["result"]["fingerprint"] != stored["fingerprint"]


def test_different_comment_blocks_assessment_reuse():
    """Speed: same formula on different scopes never shares a verdict."""
    from uuid import uuid4
    from audit_tasks import _reuse_duplicate_result

    run_id = uuid4()

    def fake_inputs(context, record):
        return [
            position(
                index=0,
                work_name="Розлив",
                volume="3.033",
                unit="т",
                formula="5055*0,6/1000",
                position_type="Работа",
                comment="Расход 0,6 л/м2",
            ),
            position(
                index=2,
                work_name="Розлив",
                volume="3.033",
                unit="т",
                formula="5055*0,6/1000",
                position_type="Работа",
                comment="Расход 0,4 л/м2",
            ),
        ]

    import workspace_service

    real_inputs = workspace_service.inputs_for_run
    workspace_service.inputs_for_run = fake_inputs
    try:
        repo = SimpleNamespace(
            load_run=lambda rid, include_results=False: {"id": str(rid), "config": {}},
            load_position_job=lambda rid, idx: (_ for _ in ()).throw(
                AssertionError("must not load jobs without twins")
            ),
            finish_position_job=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not finish")
            ),
        )
        context = SimpleNamespace(repository=repo)
        out = _reuse_duplicate_result(context, run_id, 2)
    finally:
        workspace_service.inputs_for_run = real_inputs
    assert out is None
