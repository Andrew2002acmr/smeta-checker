"""Actual lawn literals can resolve a local family, never their own adjustment."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_lawn_soil_scope import lawn_soil_scope
from normative_parameters import build_cards
from normative_thickness_adjustment import (
    PROPOSAL,
    _negative_thickness_fact,
    apply_guard,
    findings,
)
from normative_thickness_rules import source_definition, base_thickness


PD = "agent:p39:0"
CELL = "agent:p91:cell:152ed67c6ccaaf5ef2e6f862"


def case():
    raw = json.loads(
        (Path(__file__).parent / "fixtures/lawn-soil45-frozen.json").read_text(
            encoding="utf-8"
        )
    )
    return VorPosition.model_validate(raw["position"]), raw["result"]


def parts(result):
    sources = {s["id"]: s for s in result["sources"]}
    parameters = {p["parameter"]: p for p in result["parameter_proposals"]}
    return sources, parameters


def bridge(position, result):
    cards = build_cards(position, result, {45: position}, {})
    fact = _negative_thickness_fact(result, cards)
    definition = source_definition(parts(result)[0]["n2"])
    return bool(
        fact
        and definition
        and lawn_soil_scope(position, result, cards, fact, definition)
    )


def select_step(result):
    sources, _ = parts(result)
    for item in result["items"]:
        if "normative:applicability" in item.get("claim_ids", []):
            item["normative_source_ids"] = ["n2", "n13"]
    result["evidence_trace"] = [
        dict(
            kind=PROPOSAL,
            base_source_id="n2",
            adjustment_source_id="n13",
            signed_steps=1,
            base_quote=base_thickness(sources["n2"]["text"])["quote"],
            adjustment_quote=sources["n13"]["text"],
        )
    ]


def test_actual45_needs_explicit_adjustment_despite_proved_local_material_scope():
    position, result = case()
    original = deepcopy(result)
    assert bridge(position, result)
    (finding,) = findings(position, result, {45: position}, {})
    assert finding["reason"] == "missing_or_invalid_adjustment"
    assert [
        (c["code"], c["signed_steps"]) for c in finding["adjustment_candidates"]
    ] == [("47-01-046-05", 1)]
    assert result == original


def test_actual45_correct_typed_step_removes_only_this_guard_never_promotes_items():
    position, result = case()
    select_step(result)
    original = deepcopy(result)
    assert findings(position, result, {45: position}, {}) == []
    assert not apply_guard(position, result, {45: position}, {})
    assert result == original
    assert result["state"] == "needs_facts"


@pytest.mark.parametrize(
    "change",
    [
        "work_soil_missing",
        "work_lawn_missing",
        "work_negated",
        "work_other_material",
        "work_no_height",
        "work_minimum_height",
        "work_mixed_heights",
        "work_manual",
        "work_method_unknown",
        "section_missing",
        "wrong_section",
        "base_soil_missing",
        "base_lawn_missing",
        "base_other_material",
        "base_resources",
        "base_P_height",
        "base_manual04",
        "base_method_unknown",
        "pd_other_material",
        "pd_alternative",
        "pd_mixed_heights",
        "pd_no_height",
        "pd_ocr",
        "pd_page_missing",
        "pd_version_missing",
        "pd_fabricated_scope",
        "pd_wrong_unit",
        "pd_unverified",
        "pd_duplicate",
        "quantity_other_section",
        "quantity_soil_missing",
        "quantity_unverified",
        "quantity_other_pin",
        "quantity_wrong_unit",
        "quantity_wrong_number",
        "quantity_neighbor_matches_work",
        "quantity_wrong_height",
        "quantity_mixed_heights",
        "quantity_no_height",
        "quantity_unknown_height_unit",
        "quantity_not_physical",
        "quantity_no_rects",
        "quantity_ocr_unverified",
        "quantity_duplicate",
    ],
)
def test_ambiguous_or_wrong_lawn_provenance_cannot_resolve_family(change):
    position, result = case()
    sources, params = parts(result)
    q, h = params["quantity"], params["thickness"]
    if change == "work_soil_missing":
        position.work_name = position.work_name.replace(
            "растительной земли", "материала"
        )
    elif change == "work_lawn_missing":
        position.work_name = position.work_name.replace("газонов", "покрытия")
    elif change == "work_negated":
        position.work_name = "Не " + position.work_name
    elif change == "work_other_material":
        position.work_name += " с песком"
    elif change == "work_no_height":
        position.work_name = position.work_name.replace("h = 0,20 м", "")
    elif change == "work_minimum_height":
        position.work_name = position.work_name.replace("h =", "hmin =")
    elif change == "work_mixed_heights":
        position.work_name += " и h = 0,30 м"
    elif change == "work_manual":
        position.work_name = position.work_name.replace("механизировано", "вручную")
    elif change == "work_method_unknown":
        position.work_name = position.work_name.replace("(механизировано)", "")
    elif change == "section_missing":
        position.position_source.sections = []
    elif change == "wrong_section":
        position.position_source.sections[-1].text = "Раздел. Устройство тротуаров."
    elif change.startswith("base_"):
        source = sources["n2"]
        if change == "base_soil_missing":
            source["text"] = source["text"].replace("растительной земли", "грунта")
        elif change == "base_lawn_missing":
            source["text"] = source["text"].replace("газона", "склона")
        elif change == "base_other_material":
            source["text"] = source["text"].replace(
                "растительной земли", "растительной земли и песка"
            )
        elif change == "base_resources":
            source["label"] += " · ресурсы"
        elif change == "base_P_height":
            source["text"] = source["text"].replace("слоем 15\nсм", "слоем П")
        elif change == "base_manual04":
            source["text"] = (
                source["text"]
                .replace("47-01-046-03", "47-01-046-04")
                .replace("механизированным способом", "вручную")
            )
            source["label"] = source["label"].replace("47-01-046-03", "47-01-046-04")
        elif change == "base_method_unknown":
            source["text"] = source["text"].replace(
                "механизированным способом", "неустановленным способом"
            )
    elif change == "pd_other_material":
        h["scope_quote"] = h["scope_quote"].replace(
            "плодородный слой почвы", "слой песка"
        )
        sources[PD]["text"] = h["scope_quote"]
    elif change == "pd_alternative":
        h["scope_quote"] += " Или слой песка."
        sources[PD]["text"] = h["scope_quote"]
    elif change == "pd_mixed_heights":
        h["scope_quote"] += " Другой плодородный слой почвы толщиной 0,30 м."
        sources[PD]["text"] = h["scope_quote"]
    elif change == "pd_no_height":
        h["quote"] = h["scope_quote"] = sources[PD]["text"] = (
            "Наносится плодородный слой почвы."
        )
    elif change == "pd_ocr":
        sources[PD]["origin"] = "ocr"
    elif change == "pd_page_missing":
        sources[PD].pop("page")
    elif change == "pd_version_missing":
        sources[PD].pop("version_id")
    elif change == "pd_fabricated_scope":
        h["scope_quote"] += " Выдуманная цитата."
    elif change == "pd_wrong_unit":
        h["unit"] = "см"
    elif change == "pd_unverified":
        h["scope_state"] = "unresolved"
    elif change == "pd_duplicate":
        result["parameter_proposals"].append(deepcopy(h))
    elif change == "quantity_other_section":
        q["quote"] = q["scope_quote"] = sources[CELL]["text"] = q[
            "scope_quote"
        ].replace(
            "Устройство газонов. Улица | Газон", "Устройство тротуаров. Улица | Тротуар"
        )
    elif change == "quantity_soil_missing":
        q["quote"] = q["scope_quote"] = sources[CELL]["text"] = q[
            "scope_quote"
        ].replace("почвенно-растительный", "неизвестный")
    elif change == "quantity_unverified":
        q["scope_state"] = "unresolved"
    elif change == "quantity_other_pin":
        sources[CELL]["version_id"] = "another-project-version"
    elif change == "quantity_wrong_unit":
        sources[CELL]["unit"] = "м3"
    elif change == "quantity_wrong_number":
        q["value"] = "807.68"
    elif change == "quantity_neighbor_matches_work":
        q["value"] = position.volume = "807.68"
    elif change in {
        "quantity_wrong_height",
        "quantity_mixed_heights",
        "quantity_no_height",
        "quantity_unknown_height_unit",
    }:
        replacement = {
            "quantity_wrong_height": "10cM",
            "quantity_mixed_heights": "20cM и 10 см",
            "quantity_no_height": "П",
            "quantity_unknown_height_unit": "20 unknown",
        }[change]
        q["quote"] = q["scope_quote"] = sources[CELL]["text"] = q[
            "scope_quote"
        ].replace("20cM", replacement)
    elif change == "quantity_not_physical":
        sources[CELL]["id"] = q["source_id"] = "other-text"
    elif change == "quantity_no_rects":
        sources[CELL]["rects"] = []
    elif change == "quantity_ocr_unverified":
        sources[CELL]["numeric_verified"] = False
    elif change == "quantity_duplicate":
        result["parameter_proposals"].append(deepcopy(q))
    assert not bridge(position, result)


@pytest.mark.parametrize(
    "change",
    [
        "step",
        "sign",
        "pdf",
        "version",
        "measure",
        "reference",
        "quote",
        "uncited",
        "direction",
    ],
)
def test_local_soil_identity_does_not_bypass_existing_adjustment_proofs(change):
    position, result = case()
    select_step(result)
    sources, _ = parts(result)
    proposal = result["evidence_trace"][0]
    step = sources["n13"]
    if change == "step":
        proposal["signed_steps"] = 2
    elif change == "sign":
        proposal["signed_steps"] = -1
    elif change == "pdf":
        step["source_id"] = "another-pdf"
    elif change == "version":
        step["version_id"] = "another-version"
    elif change == "measure":
        step["text"] = step["text"].replace("100 м2", "100 м3")
    elif change == "reference":
        step["text"] = step["text"].replace(
            "47-01-046-01 по 47-01-046-04", "47-01-047-01"
        )
    elif change == "quote":
        proposal["adjustment_quote"] += " invented"
    elif change == "uncited":
        for item in result["items"]:
            item["normative_source_ids"] = ["n2"]
    elif change == "direction":
        step["text"] = step["text"].replace("добавлять или исключать", "не добавлять")
    assert bridge(position, result)
    assert findings(position, result, {45: position}, {})


def test_random_lawn_source_cannot_replace_the_card_bound_quantity_evidence():
    position, result = case()
    sources, _ = parts(result)
    card = next(
        c
        for c in build_cards(position, result, {45: position}, {})
        if c["id"] == "quantity"
    )
    hcard = next(
        c
        for c in build_cards(position, result, {45: position}, {})
        if c["id"] == "thickness"
    )
    fact = _negative_thickness_fact(result, [hcard])
    card["source_ids"] = ["other"]
    assert not lawn_soil_scope(
        position, result, [card, hcard], fact, source_definition(sources["n2"])
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "10-20cm",
        "10–20cm",
        "10 — 20 cm",
        ">20cm",
        ">=20cm",
        "<20cm",
        "≤20cm",
        "≥20cm",
        "~20cm",
        "≈20cm",
        "не менее20cm",
        "от20cm",
        "до20cm",
        "около20cm",
        "примерно20cm",
        "20cm-30",
        "20cm до30",
        "20cm и30",
        "20cm приблизительно",
        "20cm вручную",
        "20cm ручным способом",
        "20cm±5%",
        "20cm+5",
        "20cm−5",
        "20cm)±5% (",
        "20cm)+5 (",
    ],
)
def test_physical_header_range_bound_approximation_or_manual_scope_is_not_exact(
    replacement,
):
    position, result = case()
    sources, params = parts(result)
    q = params["quantity"]
    q["quote"] = q["scope_quote"] = sources[CELL]["text"] = q["scope_quote"].replace(
        "20cM", replacement
    )
    assert not bridge(position, result)
