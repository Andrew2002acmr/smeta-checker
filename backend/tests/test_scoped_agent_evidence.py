"""Source/subject separation and bounded production research orchestration."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from audit_models import VorPosition, PositionSource
from normative_fact_contract import (
    atomic_items,
    dimension_candidates,
    record_projection,
    restore_numeric_items,
)
from normative_scope_graph import build, for_agent, scope
from normative_evidence_followup import plan_for, question, resume_evidence
from workspace_models import NormativePositionCheckItem


def position(index=0, section="Раздел 4. Съезды", address="A2", **kwargs):
    return VorPosition(
        index=index,
        work_name="Основание",
        volume="80",
        unit="м2",
        position_type="Работа",
        position_source=PositionSource(
            document_sha256="a" * 64,
            format="xlsx",
            sheet="S",
            row=10 + index,
            header_row=1,
            position_id=str(index),
            sections=[dict(address=address, text=section)],
        ),
        **kwargs,
    )


def test_identical_names_and_section_numbers_never_transfer_cross_site_quantity():
    first = position()
    other = position(2, "Раздел 4. Остановки", "A50")
    other.volume = "999"
    graph = for_agent(SimpleNamespace(repository=Mock()), {}, {0: first, 2: other}, 0)
    assert scope(first) != scope(other)
    assert [p["index"] for p in graph["positions"]] == [0]
    assert graph["same_work_other_scopes"][0]["index"] == 2
    assert "quantity" not in graph["same_work_other_scopes"][0]
    assert "999" not in str(graph)
    other.position_source.sections = []
    assert scope(other) is None


def test_layer_edge_describes_thickness_not_shared_area():
    from test_normative_layer_assembly import fixture

    a, b = fixture()
    graph = build({a.index: a, b.index: b})
    edge = next(e for e in graph["edges"] if e["kind"] == "technological_layers")
    assert edge["thickness_cm"] == "22"
    assert edge["quantity_proven"] is False
    assert a.volume != b.volume


def test_mixed_claims_are_independent_but_model_uncertainty_is_preserved():
    item = NormativePositionCheckItem(
        topic="Сверка",
        state="confirmed",
        explanation="Основания",
        claim_ids=["quantity", "unit", "normative:applicability"],
        project_source_ids=["p"],
        normative_source_ids=["n"],
    )
    trace = []
    split = atomic_items([item], trace)
    assert [i.claim_ids for i in split] == [
        ["quantity", "unit"],
        ["normative:applicability"],
    ]
    assert not split[0].normative_source_ids and split[1].normative_source_ids == ["n"]
    assert trace[0]["original"] == item.model_dump()
    assert all(
        i.state == "needs_facts"
        for i in atomic_items([item.model_copy(update={"state": "needs_facts"})])
    )


def test_production_validator_keeps_geometry_when_normative_ocr_is_unreviewed():
    from normative_position_check import validate_items

    item = NormativePositionCheckItem(
        topic="Сверка",
        state="confirmed",
        explanation="Площадь 80 м2",
        claim_ids=["quantity", "normative:applicability"],
        project_source_ids=["p"],
        normative_source_ids=["n"],
    )
    sources = [
        dict(id="p", kind="project", origin="native", text="Площадь 80 м2"),
        dict(id="n", kind="normative", origin="ocr_unreviewed", text="Норматив"),
    ]
    rows = validate_items([item], sources, atomic=True)
    geometry = next(r for r in rows if r["claim_ids"] == ["quantity"])
    norm = next(r for r in rows if r["claim_ids"] == ["normative:applicability"])
    assert geometry["state"] == "confirmed"
    assert norm["state"] == "needs_facts"


def test_production_cards_do_not_treat_area_and_volume_as_competing_quantities():
    from test_normative_parameters import position as simple_position, proposal, source
    from normative_parameters import build_cards

    p = simple_position()
    text = "Площадь 431,611 м2; объём 86,322 м3; толщина 120 мм."
    result = dict(
        sources=[source(text=text)],
        parameter_proposals=[
            proposal("quantity", "431.611", "м2", "Площадь 431,611 м2"),
            proposal("quantity", "86.322", "м3", "объём 86,322 м3"),
        ],
    )
    card = build_cards(p, result, {0: p}, {})[0]
    assert card["state"] == "confirmed" and card["found_unit"] == "м2"
    # Two different areas still demand scope resolution, regardless of one
    # coinciding numerically with the expected VOR quantity.
    result["sources"][0]["text"] += " Другая площадь 431,61 м2."
    result["parameter_proposals"].append(
        proposal("quantity", "431.61", "м2", "Другая площадь 431,61 м2")
    )
    assert build_cards(p, result, {0: p}, {})[0]["state"] == "needs_facts"


def test_dimensions_partition_without_searching_for_equal_vor_number():
    area = dict(value="1931", unit="м2")
    volume = dict(value="386.2", unit="м3")
    assert dimension_candidates([area, volume], ("area", 999)) == [area]
    assert dimension_candidates([volume], ("area", 1931)) == [volume]
    unknown = dict(value="1931", unit="???")
    assert dimension_candidates([area, unknown], ("area", 1931)) == [area, unknown]


def test_derived_downgrade_is_reversible_but_new_model_dispute_is_not():
    original = dict(
        state="confirmed", explanation="Точная цитата", claim_ids=["quantity"]
    )
    projected = dict(
        original, state="needs_facts", explanation="Точная цитата. Карточка открыта"
    )
    result = dict(items=[deepcopy(projected)], evidence_trace=[])
    record_projection(result, original, projected)
    restore_numeric_items(result)
    assert result["items"] == [original]
    assert not result["evidence_trace"]
    result = dict(
        items=[dict(projected, explanation="Новый спор модели")], evidence_trace=[]
    )
    record_projection(result, original, projected)
    restore_numeric_items(result)
    assert result["items"][0]["state"] == "needs_facts"


def payload():
    actions = [
        dict(key="card:quantity", action="retrieve_project", retry_fingerprint="q")
    ]
    return dict(
        position_check=dict(
            pipeline_version=6,
            execution_state="complete",
            state="needs_facts",
            evidence_trace=[dict(kind="evidence_work_plan_v1", actions=actions)],
            coverage={},
        )
    )


@pytest.mark.parametrize(
    "change", ["old", "failed", "complete", "used", "quality", "parent"]
)
def test_followup_does_not_retry_legacy_failure_closed_or_ineligible_obligations(
    change,
):
    p = payload()
    r = p["position_check"]
    if change == "old":
        r["pipeline_version"] = 5
    if change == "failed":
        r["execution_state"] = "technical_failure"
    if change == "complete":
        r["state"] = "confirmed"
    if change == "used":
        r["coverage"]["evidence_followup"] = dict(round=1)
    if change == "quality":
        r["evidence_trace"][0]["actions"][0]["action"] = "verify_source_quality"
    if change == "parent":
        r["evidence_trace"][0]["actions"][0]["action"] = "resolve_parent_basis"
    assert plan_for(p) is None


def test_followup_question_is_stable_and_does_not_use_review_ids():
    p = plan_for(payload())
    assert p["additional_calls_limit"] == 3
    assert question(p) == question(deepcopy(p))
    assert "q" == p["actions"][0]["retry_fingerprint"]


def test_resume_rejects_stale_pins_before_importing_sources():
    p = position()
    from normative_service import position_key

    data = dict(
        position_key=position_key(p),
        vor_version_id="vor",
        kb={"id": "kb"},
        position_check=dict(sources=[dict(id="p", kind="project", version_id="old")]),
    )
    tools = SimpleNamespace(sources=[])
    with pytest.raises(ValueError, match="PDF pins"):
        resume_evidence(
            tools,
            dict(review=dict(id="r", payload=data)),
            dict(vor_version_id="vor", config={"pdf_versions": {"x": "new"}}),
            p,
            {"id": "kb"},
        )
    assert not tools.sources


def test_resume_retains_frozen_source_when_only_overlay_order_changed():
    from normative_service import position_key

    p = position()
    source = dict(
        id="sum",
        kind="project",
        version_id="pdf",
        page=1,
        text="2 + 3 = 5",
        rects=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.2, 0.7, 0.4]],
    )
    data = dict(
        position_key=position_key(p),
        vor_version_id="vor",
        kb=dict(id="kb"),
        position_check=dict(sources=[source], parameter_proposals=[]),
    )
    tools = SimpleNamespace(
        sources=[{**source, "rects": list(reversed(source["rects"]))}],
        events=[],
        coverage={},
    )
    with patch("normative_fact_ledger.supply"):
        resume_evidence(
            tools,
            dict(review=dict(id="r", payload=data), plan=dict(actions=[])),
            dict(vor_version_id="vor", config=dict(pdf_versions={"x": "pdf"})),
            p,
            dict(id="kb"),
        )
    assert tools.sources == [source]
    assert any(e["kind"] == "source_geometry_order_normalized" for e in tools.events)


def test_background_parent_update_only_revalidates_saved_answer_without_llm():
    from normative_background import review_position

    row = dict(id="r", kind="check", payload=payload())
    repo = Mock()
    repo.load_run.return_value = dict(
        status="done", config={"normative_after_match": True}
    )
    repo.normative_reviews.return_value = [row]
    with (
        patch("normative_position_check.check") as paid,
        patch("normative_position_check.validate_saved", return_value=row) as local,
    ):
        out = review_position(
            SimpleNamespace(repository=repo),
            "run",
            0,
            local_only=True,
            dependency_revision="proof",
        )
        assert out["local_only"] is True
        local.assert_called_once()
        paid.assert_not_called()


def test_background_targeted_pass_receives_plan_and_previous_evidence():
    from normative_background import review_position

    row = dict(id="r", kind="check", payload=payload())
    repo = Mock()
    repo.load_run.return_value = dict(
        status="done", config={"normative_after_match": True}
    )
    repo.normative_reviews.return_value = [row]
    with patch("normative_position_check.check", return_value=row) as paid:
        out = review_position(
            SimpleNamespace(repository=repo), "run", 0, evidence_followup=True
        )
        assert out == {"review_id": "r"}
        assert paid.call_args.kwargs["continuation"]["review"] == row
        assert paid.call_args.kwargs["continuation"]["plan"]["actions"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_shared_sum_reacquires_real_cells_without_inventing_quality(uncertain):
    from test_normative_quantity_sum_repair import setup
    from normative_agent import EvidenceRequest
    from normative_fact_ledger import supply

    _, donor, answer, _ = setup()
    ids = list(donor.cells)
    computed = donor.execute(
        EvidenceRequest(tool="sum_cells", operand_ids=ids, unit="м2")
    )
    assert "error" not in computed
    sources = deepcopy(donor.sources)
    mapping = {s["id"]: "shared:" + str(i) for i, s in enumerate(sources)}
    for source in sources:
        source["physical_source_id"] = source["id"]
        source["id"] = mapping[source["id"]]
    proposal = dict(
        parameter="quantity",
        operation="sum",
        source_id=mapping[computed["source_id"]],
        value=computed["value"],
        unit="м2",
        operand_source_ids=[mapping[s] for s in ids],
        quote="old",
        scope_quote="old",
        scope_state="unresolved",
    )
    bundle = dict(sources=sources, candidates=[dict(proposal=proposal)])
    _, fresh, _, _ = setup()
    fresh.cells.clear()
    fresh.sources.clear()
    if uncertain:
        for table in fresh.index.tables[-1]:
            for cell in table["cells"]:
                if cell["column"] in {2, 8}:
                    cell["quality"] = "uncertain"
                    cell["numeric_verified"] = False
    supply(fresh, bundle)
    assert fresh.cells and all("bbox" in v["cell"] for v in fresh.cells.values())
    assert proposal["scope_state"] == "unresolved"
    if uncertain:
        assert not fresh.quantity_sums
        assert all(v["cell"]["quality"] == "uncertain" for v in fresh.cells.values())
    else:
        assert proposal["operand_source_ids"] == ids
        assert fresh.quantity_sums[0]["value"] == computed["value"]


def test_literal_fact_does_not_claim_subject_when_only_another_dimension_is_bound():
    from normative_fact_ledger import build as ledger

    p = position()
    sources = [
        dict(
            id="p",
            kind="project",
            origin="native",
            text="Площадь 80 м2; объём 16 м3",
            version_id="pdf",
            page=1,
        )
    ]
    result = dict(
        sources=sources,
        parameter_proposals=[
            dict(
                parameter="quantity",
                source_id="p",
                value="16",
                unit="м3",
                quote="объём 16 м3",
                scope_quote="объём 16 м3",
                scope_state="verified",
            )
        ],
        parameter_cards=[
            dict(
                id="quantity",
                state="confirmed",
                source_ids=["p"],
                found_value="80",
                found_unit="м2",
                source_anchors=[dict(source_id="p", quote="Площадь 80 м2", rects=[])],
            )
        ],
    )
    facts = ledger(p, result, {0: p}, {})["facts"]
    assert facts[0]["dimension"] == "volume" and facts[0]["literal_state"] == "verified"
    assert (
        facts[0]["subject_state"] == "unresolved" and facts[0]["owner_position"] is None
    )
