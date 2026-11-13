from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from normative_agent import EvidenceTools
from normative_curb_quantity import project, TOPIC, verified_mismatch
from normative_parameters import build_cards


def setup():
    data = json.loads(
        (Path(__file__).parent / "fixtures/curb-column-scope28.json").read_text(
            encoding="utf-8"
        )
    )
    tools = EvidenceTools.__new__(EvidenceTools)
    tools.sources = []
    tools.cells = {}
    tools.events = []
    tools.preferred = []
    tools.coverage = {}
    tools.version = data["version_id"]
    tools.filename = "Pinned project"
    tables = [[] for _ in range(99)]
    tables[data["page"] - 1] = [data["table"]]
    tools.index = SimpleNamespace(
        tables=tables, texts=[""] * 99, metadata=data["metadata"]
    )
    return (
        VorPosition.model_validate(data["position"]),
        data["result"],
        tools,
        data["relation"],
    )


def test_real_street_total_is_selected_by_section_and_mark_not_vor_number():
    position, result, tools, relation = setup()
    original = deepcopy(result)
    assert project(position, result, tools, relation)
    card = next(
        c
        for c in build_cards(position, result, {28: position}, relation)
        if c["id"] == "quantity"
    )
    assert (
        card["state"] == "possible_mismatch"
        and card["found_value"] == "1574.00"
        and card["found_unit"] == "м"
    )
    assert (
        next(i for i in result["items"] if i["topic"] == TOPIC)["state"]
        == "possible_mismatch"
    )
    assert [
        i for i in original["items"] if "quantity" not in i.get("claim_ids", [])
    ] == [i for i in result["items"] if "quantity" not in i.get("claim_ids", [])]
    assert result["evidence_trace"][-1]["original_proposals"] == [
        p for p in original["parameter_proposals"] if p["parameter"] == "quantity"
    ]
    frozen = deepcopy(result)
    assert not project(position, result, tools, relation) and result == frozen


@pytest.mark.parametrize("declared", ["161", "279", "1574.0"])
def test_declared_amount_is_only_used_for_final_comparison(declared):
    position, result, tools, relation = setup()
    position.volume = declared
    assert project(position, result, tools, relation)
    card = next(
        c
        for c in build_cards(position, result, {28: position}, relation)
        if c["id"] == "quantity"
    )
    assert card["found_value"] == "1574.00"
    assert card["state"] == (
        "confirmed" if declared == "1574.0" else "possible_mismatch"
    )


def test_bus_stop_section_selects_its_own_column_instead_of_street():
    position, result, tools, relation = setup()
    position.position_source.sections = [
        dict(address="A1", text="Раздел. Устройство автобусных остановок")
    ]
    position.volume = "161"
    for material in relation["scope_materials"]:
        material["ownership"]["provenance"]["sections"] = [
            dict(address="A1", text="Раздел. Устройство автобусных остановок")
        ]
    assert project(position, result, tools, relation)
    card = next(
        c
        for c in build_cards(position, result, {28: position}, relation)
        if c["id"] == "quantity"
    )
    assert card["found_value"] == "161" and card["state"] == "confirmed"


def test_independent_pd_conflict_is_reported_while_norm_applicability_stays_open():
    from normative_verdict import finalize_state, verified_quantity
    from normative_parameters import enrich

    position, result, tools, relation = setup()
    assert project(position, result, tools, relation)
    assert verified_mismatch(position, result)
    result = enrich(position, result, {28: position}, relation)
    result['position_key']='same'
    assert verified_quantity(result,'same','м',position=position)==('1574.00','м')
    assert verified_quantity(result,'same','м',position=position,derived=True) is None
    assert not any(
        i["state"] == "confirmed"
        and "normative:applicability" in i.get("claim_ids", [])
        for i in result["items"]
    )
    assert (
        finalize_state(
            result, dict(status="Требуется ручная проверка"), "same", position=position
        )["state"]
        == "possible_mismatch"
    )
    for mutation in ["no_proof", "source_changed", "cell_changed", "wrong_vor_scope"]:
        changed = deepcopy(result)
        pos = position.model_copy(deep=True)
        event = next(
            e
            for e in changed["evidence_trace"]
            if e.get("kind") == "explicit_curb_column_quantity_v1"
        )
        if mutation == "no_proof":
            changed["evidence_trace"] = []
        elif mutation == "source_changed":
            next(
                s
                for s in changed["sources"]
                if s["id"] == event["proposal"]["source_id"]
            )["numeric_verified"] = False
        elif mutation == "cell_changed":
            event["cell_evidence"]["cell"]["text"] = "0"
        else:
            pos.position_source.sections = []
        assert not verified_mismatch(pos, changed)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_section",
        "exit_scope",
        "two_marks",
        "two_tables",
        "bad_numeric",
        "bad_unit",
        "changed_location",
        "child_position",
        "not_curb_work",
    ],
)
def test_ambiguous_or_unverified_evidence_does_not_change_review(change):
    position, result, tools, relation = setup()
    table = tools.index.tables[91][0]
    if change == "unknown_section":
        position.position_source.sections = []
    elif change == "exit_scope":
        position.position_source.sections = [dict(address="A1", text="Съезды")]
    elif change == "two_marks":
        relation["scope_materials"][0]["work"] += " и БР100.30.15"
    elif change == "two_tables":
        tools.index.tables[91].append(deepcopy(table))
    elif change == "bad_numeric":
        for c in table["cells"]:
            if c["role"] == "cell":
                c["numeric_verified"] = False
    elif change == "bad_unit":
        table["column_units"] = {}
    elif change == "changed_location":
        for s in result["sources"]:
            if ":cell:" in s["id"] and "1574" in s["text"]:
                s["rects"] = []
    elif change == "child_position":
        relation["parent"] = {"index": 1}
    else:
        position.work_name = "Разборка бортового камня"
    original = deepcopy(result)
    assert not project(position, result, tools, relation)
    assert result == original
