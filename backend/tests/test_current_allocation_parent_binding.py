"""A planned-volume recipe must not inherit non-input work area/depth checks."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from normative_fact_contract import PROJECTION, restore_numeric_items
from normative_layer_assembly import (
    material_allocation_proof,
    verified_material_allocation,
)
from normative_parameters import enrich
from normative_parent_basis import (
    GEOMETRY_TOPIC,
    GEOMETRY_TRACE,
    _current_material_geometry,
    bind_parent_basis,
)
from normative_verdict import REVIEW
from test_planned_allocation_integration import prepared, report


def setup(upper=False):
    positions, raw, relation = prepared(upper)
    record = dict(
        id="run", vor_version_id="vor", config=dict(pdf_versions={"PD": "v1"})
    )
    context = SimpleNamespace(repository=Mock())
    context.repository.normative_reviews.return_value = []
    return positions, raw, relation, record, context


def bind(positions, raw, relation, record, context):
    return bind_parent_basis(
        context, record, raw, positions, relation, position=positions[1]
    )


@pytest.mark.parametrize("upper", [False, True])
def test_valid_current_allocation_bypasses_parent_area_and_partial_height(upper):
    positions, raw, relation, record, context = setup(upper)
    before = deepcopy(raw)
    assert material_allocation_proof(positions[1], raw, positions, relation)
    bound = bind(positions, raw, relation, record, context)
    assert not any(i["topic"].startswith(GEOMETRY_TOPIC) for i in bound["items"])
    assert bound["parameter_proposals"] == raw["parameter_proposals"]
    assert not any(e.get("kind") == GEOMETRY_TRACE for e in bound["evidence_trace"])
    context.repository.normative_reviews.assert_not_called()
    checked = enrich(positions[1], bound, positions, relation)
    assert verified_material_allocation(checked, positions[1])
    assert report(checked, positions[1])["Статус аудита"] == "ОК"
    assert raw == before


def old_partial_binding(positions, raw, record):
    # Reproduce the old partial branch: full PD height can be inherited although
    # the parent area is missing. It is not an input of this material recipe.
    bound = _current_material_geometry(
        raw, positions[0], positions[1], positions, record
    )
    assert any(i["topic"].startswith(GEOMETRY_TOPIC) for i in bound["items"])
    saved = enrich(positions[1], bound, positions, dict(parent={"index": 0}))
    assert any(
        e.get("kind") == PROJECTION
        and e["original"]["topic"].startswith(GEOMETRY_TOPIC)
        for e in saved["evidence_trace"]
    )
    return saved


def test_saved_server_alias_and_numeric_projection_are_retired_without_losing_own_literals():
    positions, raw, relation, record, context = setup()
    saved = old_partial_binding(positions, raw, record)
    before = deepcopy(saved)
    assert report(saved, positions[1])["Статус аудита"] == REVIEW
    bound = bind(positions, saved, relation, record, context)
    assert not any(i["topic"].startswith(GEOMETRY_TOPIC) for i in bound["items"])
    assert not any(e.get("kind") == PROJECTION for e in bound["evidence_trace"])
    assert not any(
        p["source_id"].startswith("parent-0:geometry:")
        for p in bound["parameter_proposals"]
    )
    retained_aliases = {
        s["id"] for s in bound["sources"] if s["id"].startswith("parent-0:geometry:")
    }
    assert retained_aliases <= {
        sid for i in bound["items"] for sid in i["project_source_ids"]
    }
    restore_numeric_items(bound)
    assert not any(i["topic"].startswith(GEOMETRY_TOPIC) for i in bound["items"])
    assert any(
        p["source_id"] == "construction" and p["parameter"] == "thickness"
        for p in bound["parameter_proposals"]
    )
    checked = enrich(positions[1], bound, positions, relation)
    assert verified_material_allocation(checked, positions[1])
    assert report(checked, positions[1])["Статус аудита"] == "ОК"
    again = enrich(
        positions[1],
        bind(positions, checked, relation, record, context),
        positions,
        relation,
    )
    assert report(again, positions[1])["Статус аудита"] == "ОК"
    assert saved == before


def test_model_height_unknown_is_preserved_even_with_server_like_topic():
    positions, raw, relation, record, context = setup()
    unknown = dict(
        topic=GEOMETRY_TOPIC + ": модельный вопрос",
        state="needs_facts",
        required=True,
        claim_ids=["geometry:comment:thickness:1"],
        explanation="Не установлена физическая толщина части",
        project_source_ids=["construction"],
        normative_source_ids=[],
    )
    raw["items"].append(unknown)
    bound = bind(positions, raw, relation, record, context)
    assert unknown in bound["items"]
    checked = enrich(positions[1], bound, positions, relation)
    assert verified_material_allocation(checked, positions[1])
    assert unknown in checked["items"]
    assert report(checked, positions[1])["Статус аудита"] == REVIEW


def test_new_model_dispute_on_an_old_alias_is_not_restored_to_server_confirmed_item():
    positions, raw, relation, record, context = setup()
    saved = old_partial_binding(positions, raw, record)
    unknown = next(i for i in saved["items"] if i["topic"].startswith(GEOMETRY_TOPIC))
    unknown["explanation"] = "Новый самостоятельный спор о физической толщине операции"
    original = deepcopy(unknown)
    bound = bind(positions, saved, relation, record, context)
    retained = next(
        i for i in bound["items"] if i["explanation"] == original["explanation"]
    )
    assert (
        retained["state"] == "needs_facts"
        and retained["claim_ids"] == original["claim_ids"]
    )
    assert all(
        any(s["id"] == sid for s in bound["sources"])
        for sid in retained["project_source_ids"]
    )
    restore_numeric_items(bound)
    assert retained in bound["items"] and retained["state"] == "needs_facts"
    checked = enrich(positions[1], bound, positions, relation)
    assert report(checked, positions[1])["Статус аудита"] == REVIEW


@pytest.mark.parametrize("change", ["unbalanced", "project_part"])
def test_invalid_allocation_or_explicit_physical_part_constraint_keeps_parent_path(
    change,
):
    positions, raw, relation, record, context = setup()
    if change == "unbalanced":
        positions[3].formula = "108*1.26"
    else:
        raw["sources"].append(
            dict(
                id="physical-part",
                kind="project",
                version_id="v1",
                page=50,
                origin="native",
                text="ЩПС 0/31,5: технологический слой1 — 105.84 м3",
            )
        )
        raw["items"].append(
            dict(
                topic="Явное проектное условие",
                state="needs_facts",
                required=True,
                claim_ids=["geometry:comment:thickness:1"],
                project_source_ids=["physical-part"],
                normative_source_ids=[],
                explanation="Физическая часть требует самостоятельной проверки",
            )
        )
    assert not material_allocation_proof(positions[1], raw, positions, relation)
    bound = bind(positions, raw, relation, record, context)
    context.repository.normative_reviews.assert_called_once()
    checked = enrich(positions[1], bound, positions, relation)
    assert report(checked, positions[1])["Статус аудита"] != "ОК"
    if change == "project_part":
        assert any(
            i["topic"] == "Явное проектное условие" and i["state"] == "needs_facts"
            for i in checked["items"]
        )


def test_saved_literal_alias_and_fresh_primary_source_do_not_create_false_ambiguity():
    positions, raw, relation, record, context = setup()
    source = next(s for s in raw["sources"] if s["id"] == "construction")
    raw["sources"] = [s for s in raw["sources"] if s["id"] != "construction"] + [
        {
            **deepcopy(source),
            "id": "shared-construction",
            "physical_source_id": "construction",
        },
        source,
    ]
    next(p for p in raw["parameter_proposals"] if p["parameter"] == "thickness")[
        "source_id"
    ] = "shared-construction"
    saved = old_partial_binding(positions, raw, record)
    bound = bind(positions, saved, relation, record, context)
    assert (
        next(p for p in bound["parameter_proposals"] if p["parameter"] == "thickness")[
            "source_id"
        ]
        == "shared-construction"
    )
    checked = enrich(positions[1], bound, positions, relation)
    assert verified_material_allocation(checked, positions[1])
    assert report(checked, positions[1])["Статус аудита"] == "ОК"


def test_equal_number_or_page_does_not_authorize_canonicalizing_another_literal():
    positions, raw, relation, record, context = setup()
    source = next(s for s in raw["sources"] if s["id"] == "construction")
    raw["sources"] = [s for s in raw["sources"] if s["id"] != "construction"] + [
        {
            **deepcopy(source),
            "id": "shared-construction",
            "physical_source_id": "construction",
            "text": source["text"] + " Другое примечание.",
        },
        source,
    ]
    next(p for p in raw["parameter_proposals"] if p["parameter"] == "thickness")[
        "source_id"
    ] = "shared-construction"
    bound = bind(positions, raw, relation, record, context)
    assert (
        next(p for p in bound["parameter_proposals"] if p["parameter"] == "thickness")[
            "source_id"
        ]
        == "shared-construction"
    )

    checked = enrich(positions[1], bound, positions, relation)
    assert not verified_material_allocation(checked, positions[1])


def test_material_feedback_uses_full_declared_height_without_claiming_shared_area():
    from normative_position_check import declared_layer_context
    from normative_parameters import build_cards
    from normative_local_checks import layer_group

    positions, raw, relation, _, _ = setup(upper=True)
    positions[2].volume = "91"
    assert layer_group(positions[0], [], positions) is None
    context = declared_layer_context(positions[1], positions[0], [], positions)
    assert context is not None
    assert context["height"] == "20"
    assert context["quantity_shared"] is False
    assert context["operation_quantities"] == ["90", "91"]
    assert context["work_indices"] == [0, 2]
    assert "quantity" not in context
    # The same structural hint does not upgrade the work's own quantity.
    assert declared_layer_context(positions[0], positions[0], [], positions) is None
    cards = build_cards(positions[0], raw, positions, {})
    assert next(c for c in cards if c["id"] == "quantity")["state"] != "confirmed"
