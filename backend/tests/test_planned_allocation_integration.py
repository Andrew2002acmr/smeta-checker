"""Allocation cards preserve planned quantities and independent semantic gaps."""

from copy import deepcopy

import pytest

from normative_coefficient_policy import accepted_claims
from normative_layer_assembly import ALLOCATION_PROOF, verified_material_allocation
from normative_parameters import enrich
from normative_verdict import REVIEW, publish, readable_review
from test_material_planned_allocation import fixture
from test_named_coefficient_policy import conditional_fixture


def item(claim, state="confirmed", norms=None):
    return dict(
        topic=claim,
        claim_ids=[claim],
        state=state,
        required=True,
        explanation=claim,
        project_source_ids=["total"],
        normative_source_ids=norms or [],
    )


def prepared(upper=False, index=1) -> tuple[dict, dict, dict]:
    positions, fixture_result, _ = fixture(upper)
    result: dict = deepcopy(fixture_result)
    relation = dict(parent={"index": index - 1}, children=[])
    _, coefficient_result = conditional_fixture()
    result.update(
        pipeline_version=24,
        execution_state="complete",
        state="needs_facts",
        summary="Проверка материала",
        coverage=coefficient_result["coverage"],
        related_positions=[],
        evidence_trace=[],
        items=[
            item("derived:parent", "needs_facts"),
            item("derived:rate", "needs_facts", ["sp"]),
            item("normative:applicability", norms=["sp"]),
        ],
    )
    result["sources"].append(deepcopy(coefficient_result["sources"][1]))
    return positions, result, relation


def report(result, position):
    return publish(
        {"Статус аудита": "ОК"},
        {"checks": []},
        "key",
        dict(stage="done", review={**result, "position_key": "key"}),
        enabled=True,
        derived=True,
        position=position,
    )


@pytest.mark.parametrize("upper,index", [(False, 1), (False, 3), (True, 1), (True, 3)])
def test_quantity_is_calculated_allocation_and_never_a_literal_project_part(
    upper, index
):
    positions, raw, relation = prepared(upper, index)
    before = deepcopy(raw)
    result = enrich(positions[index], raw, positions, relation)
    assert raw == before
    assert verified_material_allocation(result, positions[index])
    proof = next(
        e for e in result["evidence_trace"] if e.get("kind") == ALLOCATION_PROOF
    )
    operand = next(o for o in proof["operands"] if o["material_index"] == index)
    cards = {c["id"]: c for c in result["parameter_cards"]}
    quantity = cards["quantity"]
    assert quantity["state"] == "confirmed" and quantity["role"] == "calculation"
    assert quantity["quantity_role"] == "allocated_output"
    assert quantity["found_value"] == operand["calculated_material_volume"]
    assert quantity["found_unit"] == "м3" and quantity["unit"] == "м3"
    assert quantity["quote"] == proof["project_basis"]["volume"]["scope_quote"]
    assert operand["declared_volume"] not in quantity["quote"]
    assert not quantity["proof_limits"]["stage_areas_verified"]
    assert not quantity["proof_limits"]["stage_thicknesses_verified"]
    assert not quantity["proof_limits"]["individual_project_volumes_verified"]
    assert "thickness" not in cards
    base = next(
        c for c in cards.values() if c["role"] == "calculation_declared_allocation"
    )
    assert (
        base["state"] == "confirmed"
        and base["found_value"] == operand["declared_volume"]
    )
    assert base["quote"] == quantity["quote"]
    assert "derived:rate" in accepted_claims(positions[index], result)
    assert report(result, positions[index])["Статус аудита"] == "ОК"
    projected = readable_review(result)
    dependency = next(
        i for i in projected["items"] if i["claim_ids"] == ["derived:parent"]
    )
    assert (
        dependency["state"] == "confirmed"
        and "Плановое распределение" in dependency["topic"]
    )
    assert "площади" in dependency["explanation"]


@pytest.mark.parametrize(
    "claim", ["normative:applicability", "attribute:comment:granulometry:1"]
)
def test_semantic_unknown_is_not_closed_by_allocation_or_coefficient(claim):
    positions, raw, relation = prepared()
    raw["items"] = [i for i in raw["items"] if i["claim_ids"] != [claim]] + [
        item(claim, "needs_facts", ["sp"])
    ]
    result = enrich(positions[1], raw, positions, relation)
    assert verified_material_allocation(result, positions[1])
    assert any(
        i["claim_ids"] == [claim] and i["state"] == "needs_facts"
        for i in result["items"]
    )
    assert report(result, positions[1])["Статус аудита"] == REVIEW


@pytest.mark.parametrize(
    "change",
    [
        "quantity",
        "base",
        "arithmetic",
        "coefficient",
        "empty_cards",
        "event",
        "proposal",
        "source",
        "normative_range",
        "child",
    ],
)
def test_replayed_proof_or_copied_green_cards_cannot_bypass_existing_guards(change):
    positions, raw, relation = prepared()
    position = positions[1]
    result = enrich(position, raw, positions, relation)
    assert report(result, position)["Статус аудита"] == "ОК"
    if change == "quantity":
        next(c for c in result["parameter_cards"] if c["id"] == "quantity")[
            "found_value"
        ] = "999"
    elif change == "base":
        next(
            c
            for c in result["parameter_cards"]
            if c["role"] == "calculation_declared_allocation"
        )["found_value"] = "999"
    elif change in {"arithmetic", "coefficient"}:
        key = "calculation:1" if change == "arithmetic" else "factor:1:1.26"
        result["parameter_cards"] = [
            c for c in result["parameter_cards"] if c["id"] != key
        ]
    elif change == "empty_cards":
        result["parameter_cards"] = []
    elif change == "event":
        result["evidence_trace"] = [
            e for e in result["evidence_trace"] if e.get("kind") != ALLOCATION_PROOF
        ]
    elif change == "proposal":
        result["parameter_proposals"] = []
    elif change == "source":
        next(s for s in result["sources"] if s["id"] == "total")["text"] = (
            "Другая конструкция,999м3"
        )
    elif change == "normative_range":
        next(s for s in result["sources"] if s["id"] == "sp")["text"] = (
            "Применимость не установлена"
        )
    else:
        position = positions[3]
    assert report(result, position)["Статус аудита"] != "ОК"


def test_reenrichment_replaces_the_proof_and_does_not_duplicate_cards():
    positions, raw, relation = prepared()
    first = enrich(positions[1], raw, positions, relation)
    second = enrich(positions[1], first, positions, relation)
    assert verified_material_allocation(second, positions[1])
    assert (
        len([e for e in second["evidence_trace"] if e.get("kind") == ALLOCATION_PROOF])
        == 1
    )
    assert second["parameter_cards"] == first["parameter_cards"]
