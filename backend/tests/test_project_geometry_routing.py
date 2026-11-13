"""PD-only parent routing must not become a trusted green-card shortcut."""

from copy import deepcopy

import pytest

from audit_models import VorPosition
from normative_parent_basis import DEPENDENCY_TRACE, verified_geometry_dependency
from normative_position_check import CheckedItem, validate_items
from normative_verdict import REVIEW, publish, readable_review, source_supported
from test_normative_parent_geometry import fixture, geometry_cards, invoke


def item(claim, state="confirmed", norms=None) -> dict:
    return dict(
        topic=claim,
        state=state,
        required=True,
        claim_ids=[claim],
        explanation=claim,
        project_source_ids=["pd"],
        normative_source_ids=norms or [],
    )


def review() -> dict:
    return dict(
        position_key="child",
        pipeline_version=23,
        execution_state="complete",
        state="confirmed",
        summary="model claims confirmed",
        parameter_proposals=[],
        parameter_cards=[],
        sources=[
            dict(
                id="pd",
                kind="project",
                origin="native",
                version_id="pdf",
                page=1,
                text="Другой участок: площадь 99 м2",
            ),
            dict(id="n", kind="normative", text="Norm"),
        ],
        items=[
            item("derived:parent"),
            item("normative:applicability", norms=["n"]),
            item("derived:rate", norms=["n"]),
        ],
        evidence_trace=[],
    )


def publish_material(result, position=None):
    return publish(
        {"Статус аудита": "ОК"},
        dict(checks=[]),
        "child",
        dict(stage="done", review=result),
        enabled=True,
        derived=True,
        position=position,
    )


def test_pd_only_parent_is_a_source_route_not_a_normative_or_machine_route():
    assert source_supported(item("derived:parent"))
    for claim in (
        "method",
        "method:comment:equipment:1",
        "normative:applicability",
        "coefficient",
    ):
        assert not source_supported(item(claim))
    mixed = item("derived:parent")
    mixed["claim_ids"].append("method")
    assert not source_supported(mixed)
    assert not source_supported({**item("derived:parent"), "project_source_ids": []})


def test_validation_preserves_project_parent_item_without_inventing_a_norm_reference():
    raw = item("derived:parent")
    sources = review()["sources"]
    checked = validate_items([CheckedItem.model_validate(raw)], sources)
    assert checked[0]["state"] == "confirmed"
    assert not checked[0]["normative_source_ids"]
    assert checked[0]["project_source_ids"] == ["pd"]


@pytest.mark.parametrize(
    "cards",
    [
        [],
        [
            dict(
                id="quantity",
                state="confirmed",
                label="Copied quantity",
                reason="green",
                found_value="10",
                found_unit="м2",
                source_ids=["pd"],
                source_anchors=[dict(source_id="pd", quote="площадь 99 м2", rects=[])],
            )
        ],
    ],
)
def test_unrelated_pd_empty_parameters_and_copied_green_cards_cannot_close_material(
    cards,
):
    result = review()
    result["parameter_cards"] = cards
    original = deepcopy(result)
    assert not verified_geometry_dependency(result)
    assert publish_material(result)["Статус аудита"] == REVIEW
    assert result == original


def certified():
    donor, record, raw, positions, context = fixture()
    result = invoke(donor, record, raw, positions, context)
    result, cards = geometry_cards(result, positions)
    assert cards["quantity"]["state"] == cards["thickness"]["state"] == "confirmed"
    assert verified_geometry_dependency(result, positions[1])
    return result, positions[1]


def test_valid_bound_geometry_is_displayed_separately_and_never_confirms_rate_or_norm():
    result, child = certified()
    view = readable_review(result)
    geometry = [i for i in view["items"] if i["claim_ids"] == ["derived:parent"]]
    assert geometry and all(
        i["state"] == "confirmed" and not i["normative_source_ids"] for i in geometry
    )
    assert any(
        i["state"] == "needs_facts" and "derived:rate" in i["claim_ids"]
        for i in view["items"]
    )
    assert any(
        i["state"] == "needs_facts" and "normative:applicability" in i["claim_ids"]
        for i in view["items"]
    )
    result["position_key"] = "child"
    assert publish_material(result, child)["Статус аудита"] == REVIEW


@pytest.mark.parametrize(
    "change",
    ["parameters", "source", "quality", "scope", "card", "proof", "other_child"],
)
def test_copied_dependency_or_green_cards_are_rebuilt_before_publication(change):
    result, child = certified()
    # Close unrelated semantic items to isolate the new geometry route. These
    # synthetic assertions are not normative evidence or a production verdict.
    result["items"] = [
        item("derived:parent"),
        item("normative:applicability", norms=["n"]),
        item("derived:rate", norms=["n"]),
    ]
    result["sources"].extend(review()["sources"])
    if change == "parameters":
        result["parameter_proposals"] = []
    elif change in {"source", "quality"}:
        source = next(s for s in result["sources"] if s["id"].startswith("parent-"))
        source["text"] = (
            "Другой участок: площадь 99 м2" if change == "source" else source["text"]
        )
        if change == "quality":
            source.update(origin="ocr", numeric_verified=False)
    elif change == "scope":
        next(p for p in result["parameter_proposals"] if p["parameter"] == "quantity")[
            "scope_state"
        ] = "unresolved"
    elif change == "card":
        next(c for c in result["parameter_cards"] if c["id"] == "quantity")[
            "found_value"
        ] = "99"
    elif change == "proof":
        result["evidence_trace"] = [
            e for e in result["evidence_trace"] if e.get("kind") != DEPENDENCY_TRACE
        ]
    else:
        child = child.model_copy(update={"volume": "99"})
    assert not verified_geometry_dependency(result, child)
    result["position_key"] = "child"
    assert publish_material(result, child)["Статус аудита"] == REVIEW


def test_valid_binder_geometry_closes_only_parent_obligation():
    result, child = certified()
    result["position_key"] = "child"
    result["items"] = [
        item("derived:parent"),
        item("normative:applicability", norms=["n"]),
        item("derived:rate", norms=["n"]),
    ]
    result["sources"].extend(review()["sources"])
    assert publish_material(result, child)["Статус аудита"] == "ОК"
    without_proof = deepcopy(result)
    without_proof["evidence_trace"] = []
    assert publish_material(without_proof, child)["Статус аудита"] == REVIEW
    # Independent coefficient gaps still block publication with valid geometry.
    result["parameter_cards"].append(
        dict(
            id="coefficient",
            role="coefficient",
            required=True,
            state="needs_facts",
            label="Потери",
            reason="Нет основания коэффициента",
        )
    )
    report = publish_material(result, child)
    assert "Не все условия подтверждены" not in report["Что не так"]
    assert "Нет основания коэффициента" in report["Что не так"]
    assert report["Статус аудита"] == REVIEW


def test_own_project_quantity_without_parent_link_keeps_existing_route():
    result = review()
    result["items"] = [item("quantity"), item("normative:applicability", norms=["n"])]
    position = VorPosition(
        index=0,
        work_name="Самостоятельный материал",
        position_type="Материал",
        volume="10",
        unit="м3",
    )
    report = publish(
        {"Статус аудита": "ОК"},
        dict(checks=[]),
        "child",
        dict(stage="done", review=result),
        enabled=True,
        position=position,
    )
    assert report["Статус аудита"] == "ОК"


def test_existing_normative_parent_route_does_not_require_new_geometry_proof():
    result = review()
    result["items"][0]["normative_source_ids"] = ["n"]
    assert not verified_geometry_dependency(result)
    assert publish_material(result)["Статус аудита"] == "ОК"
