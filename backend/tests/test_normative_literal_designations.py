"""Literal project compliance is separate from defining an extra VOR mark."""

from copy import deepcopy

import pytest

from audit_core import build_claim_specs
from audit_models import VorPosition
from normative_position_check import CheckedItem, validate_items
from normative_verdict import source_supported


def case():
    position = VorPosition(
        index=0,
        work_name="Смесь А22Нт на БНД 70/100 по ГОСТ 33133-2014",
        volume="1",
        unit="т",
        position_type="Материал",
    )
    claim = next(
        s.claim_id
        for s in build_claim_specs(position)
        if s.expected_text == "ГОСТ 33133-2014"
    )
    item = CheckedItem(
        topic="Обозначение в спецификации",
        state="confirmed",
        claim_ids=[claim],
        explanation="Обозначение дословно задано ПД для слоя.",
        project_source_ids=["pd"],
        normative_source_ids=[],
    )
    source = dict(
        id="pd",
        kind="project",
        origin="native",
        page=49,
        text="Нижний слой покрытия — А22Нт на БНД 70/100 по ГОСТ 33133-2014, h=7 см.",
    )
    return position, item, source


def test_exact_project_standard_survives_without_redundant_normative_citation():
    position, item, source = case()
    original = deepcopy((item, source))
    checked = validate_items([item], [source], set(item.claim_ids), position=position)
    assert checked[0]["state"] == "confirmed" and source_supported(checked[0])
    assert checked[0]["normative_source_ids"] == []
    assert (item, source) == original


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "different_year",
        "numeric_suffix",
        "unreviewed_ocr",
        "normative_in_project_field",
        "unknown_claim",
        "unknown_reference",
        "missing_position",
        "negative_conclusion",
    ],
)
def test_project_only_route_never_fills_missing_or_incompatible_evidence(change):
    position, item, source = case()
    if change == "absent":
        source["text"] = "Асфальтобетон на БНД 70/100, h=7 см."
    elif change == "different_year":
        source["text"] = source["text"].replace("2014", "2024")
    elif change == "numeric_suffix":
        source["text"] = source["text"].replace("2014", "20140")
    elif change == "unreviewed_ocr":
        source["origin"] = "ocr"
    elif change == "normative_in_project_field":
        source["kind"] = "normative"
    elif change == "unknown_claim":
        item.claim_ids = ["attribute:name:designation:999"]
    elif change == "unknown_reference":
        item.project_source_ids = ["invented"]
    elif change == "missing_position":
        position = None
    else:
        item.state = "possible_mismatch"
    result = validate_items([item], [source], set(item.claim_ids), position=position)
    assert result[0]["state"] == "needs_facts"


def test_literal_property_does_not_close_normative_applicability_in_same_answer():
    position, item, source = case()
    application = item.model_copy(update={"claim_ids": ["normative:applicability"]})
    result = validate_items(
        [item, application],
        [source],
        {*item.claim_ids, "normative:applicability"},
        position=position,
    )
    assert [r["state"] for r in result] == ["confirmed", "needs_facts"]


def test_prose_aliases_use_dispatch_registry_and_do_not_rewrite_quotes():
    from normative_agent import restore_source_ids
    from normative_position_check import AgentAnswer

    position, item, source = case()
    item.explanation = "s7 подтверждает обозначение. Модель s70 не процитирована."
    item.project_source_ids = ["s7"]
    answer = AgentAnswer(
        summary="Проверено по s7.",
        items=[item],
        parameters=[
            dict(
                parameter="thickness",
                value="7",
                unit="см",
                source_id="s7",
                quote="literal s7",
            )
        ],
    )
    restore_source_ids(answer, {"s7": "pd"}, [source])
    assert answer.summary == "Проверено по ПД, стр. 49."
    assert (
        answer.items[0].explanation
        == "ПД, стр. 49 подтверждает обозначение. Модель s70 не процитирована."
    )
    assert answer.items[0].project_source_ids == ["pd"]
    assert answer.parameters[0].quote == "literal s7"
