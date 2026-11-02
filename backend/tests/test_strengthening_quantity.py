from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from normative_agent import EvidenceTools
from normative_construction_scope import repair_thickness
from normative_parameters import build_cards
from normative_strengthening_quantity import project
from test_construction_scope import fixture


def setup():
    position, result = fixture()
    assert repair_thickness(position, result)
    data = json.loads(
        (Path(__file__).parent / "fixtures/strengthening-area22.json").read_text(
            encoding="utf-8"
        )
    )
    for source in deepcopy(data["sources"]):
        source["text"] = source["text"].replace(" | h=0,07m", "")
        result["sources"].append(source)
    tools = EvidenceTools.__new__(EvidenceTools)
    tools.sources = data["sources"]
    tools.cells = {}
    tools.events = []
    tools.quantity_sums = []
    tools.preferred = []
    tools.version = data["sources"][0]["version_id"]
    tools.filename = data["sources"][0]["label"]
    tools.coverage = {}
    tables = [[] for _ in range(99)]
    tables[88] = [data["table"]]
    tools.index = SimpleNamespace(tables=tables, texts=[""] * 99, metadata={})
    return position, result, tools


def test_real_lower_layer_area_confirmed_without_promoting_normative_items():
    position, result, tools = setup()
    original = deepcopy(result)
    assert project(position, result, tools)
    card = next(
        c
        for c in build_cards(position, result, {22: position}, {})
        if c["id"] == "quantity"
    )
    assert (
        card["state"] == "confirmed"
        and card["found_value"] == "8920.97"
        and card["found_unit"] == "м2"
    )
    assert [i for i in result["items"] if i.get("claim_ids") != ["quantity"]] == [
        i for i in original["items"] if i.get("claim_ids") != ["quantity"]
    ]
    assert result["state"] == original["state"]
    assert (
        next(i for i in result["items"] if i.get("claim_ids") == ["quantity"])["state"]
        == "confirmed"
    )
    assert result["evidence_trace"][-1]["superseded_quantity_items"]
    assert result["evidence_trace"][-1]["original_proposals"] == [
        p for p in original["parameter_proposals"] if p["parameter"] == "quantity"
    ]
    frozen = deepcopy(result)
    assert not project(position, result, tools)
    assert result == frozen


def test_different_declared_area_does_not_change_pd_selection():
    position, result, tools = setup()
    position.volume = "9999"
    assert project(position, result, tools)
    card = next(
        c
        for c in build_cards(position, result, {22: position}, {})
        if c["id"] == "quantity"
    )
    assert card["state"] == "possible_mismatch" and card["found_value"] == "8920.97"


def test_scoped_quantity_conflict_and_multiple_claims_are_not_silently_erased():
    position, result, tools = setup()
    result["parameter_proposals"].append(
        dict(parameter="quantity", scope_state="verified", source_id="other", value="1")
    )
    original = deepcopy(result)
    assert not project(position, result, tools)
    assert result == original
    position, result, tools = setup()
    item = next(i for i in result["items"] if i.get("claim_ids") == ["quantity"])
    item["claim_ids"].append("normative:applicability")
    original = deepcopy(item)
    assert project(position, result, tools)
    assert original in result["items"]


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_role",
        "no_street",
        "wrong_mix",
        "hmin",
        "ambiguous_construction",
        "unverified_thickness",
        "different_pdf",
        "missing_side",
        "wrong_column_height",
        "wrong_area_unit",
        "changed_location",
        "bad_numeric",
    ],
)
def test_missing_or_conflicting_scope_never_promotes(mutation):
    position, result, tools = setup()
    proposal = next(
        p for p in result["parameter_proposals"] if p["parameter"] == "thickness"
    )
    source = next(s for s in result["sources"] if s["id"] == proposal["source_id"])
    if mutation == "wrong_role":
        position.work_name = position.work_name.replace("нижнего", "верхнего")
    elif mutation == "no_street":
        position.position_source.sections = []
    elif mutation == "wrong_mix":
        source["text"] = source["text"].replace("А22Нт", "А22Нл")
    elif mutation == "hmin":
        source["text"] = source["text"].replace("h=7 см", "hmin=7 см")
    elif mutation == "ambiguous_construction":
        result["sources"].append({**deepcopy(source), "id": "duplicate", "page": 99})
    elif mutation == "unverified_thickness":
        proposal["scope_state"] = "unresolved"
    elif mutation == "different_pdf":
        tools.version = "different"
    elif mutation == "missing_side":
        tools.index.tables[88][0]["cells"] = [
            c
            for c in tools.index.tables[88][0]["cells"]
            if not (c["row"] == 41 and c["column"] == 10)
        ]
    elif mutation == "wrong_column_height":
        for c in tools.index.tables[88][0]["cells"]:
            if c["row"] == 4 and c["column"] == 10:
                c["text"] = "h=0,08m"
    elif mutation == "wrong_area_unit":
        position.unit = "м3"
    elif mutation == "changed_location":
        tools.sources[0]["rects"] = [[0, 0, 0.1, 0.1]]
    else:
        tools.sources[0]["numeric_verified"] = False
    original = deepcopy(result)
    assert not project(position, result, tools)
    assert result == original
