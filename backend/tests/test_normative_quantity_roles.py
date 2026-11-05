from copy import deepcopy

import pytest

from normative_parameters import build_cards, numeric_card_keys
from normative_fact_ledger import build
from normative_verdict import source_supported
from normative_workbook_rate import quantity_contract
from test_normative_parameters import position, proposal, source


def fixture(found="431.611", scope="verified", unit="м2"):
    p = position(volume="0.345288", unit="т", comment="Расход 0,8 л/м2")
    p.work_name = "Розлив эмульсии на покрытие"
    p.formula = "431.61*0.8/1000"
    text = f"Площадь покрытия {found} {unit}"
    s = {
        **source(text=text),
        "version_id": "pdf",
        "page": 1,
        "rects": [[0.1, 0.2, 0.3, 0.4]],
    }
    raw = {
        **proposal("quantity", found, unit, text),
        "scope_state": scope,
        "scope_quote": text,
    }
    result = dict(
        pipeline_version=6,
        sources=[s],
        parameter_proposals=[raw],
        items=[
            dict(
                claim_ids=["input:area"],
                state="confirmed",
                project_source_ids=["p1"],
                normative_source_ids=[],
            ),
            dict(
                claim_ids=["quantity"],
                state="needs_facts",
                project_source_ids=["p1"],
                normative_source_ids=[],
            ),
        ],
    )
    return p, result


def test_area_and_output_mass_are_separate_typed_obligations():
    p, result = fixture()
    original = deepcopy(result)
    cards = build_cards(p, result, {p.index: p}, {})
    by_id = {c["id"]: c for c in cards}
    assert by_id["input:area"]["state"] == "confirmed"
    assert by_id["input:area"]["found_unit"] == "м2"
    assert by_id["quantity"]["state"] == "needs_facts"
    assert by_id["quantity"]["unit"] == "т" and not by_id["quantity"].get("found_value")
    assert "плотность" in by_id["quantity"]["reason"]
    assert result == original
    assert numeric_card_keys(["input:area"]) == {"input:area"}
    assert source_supported(result["items"][0])


@pytest.mark.parametrize("change", ["scope", "quote", "ocr"])
def test_input_area_keeps_the_source_and_scope_guards(change):
    p, result = fixture()
    if change == "scope":
        result["parameter_proposals"][0]["scope_state"] = "unresolved"
    elif change == "quote":
        result["parameter_proposals"][0]["quote"] = "Invented quantity 431.611"
    else:
        result["sources"][0]["origin"] = "ocr_unverified"
    cards = {c["id"]: c for c in build_cards(p, result, {p.index: p}, {})}
    assert cards["input:area"]["state"] == "needs_facts"
    assert cards["quantity"]["state"] == "needs_facts"


def test_actual_input_disagreement_and_direct_mass_disagreement_are_retained():
    p, result = fixture(found="450")
    cards = {c["id"]: c for c in build_cards(p, result, {p.index: p}, {})}
    assert cards["input:area"]["state"] == "possible_mismatch"
    assert cards["quantity"]["state"] == "needs_facts"
    p, result = fixture(found="2", unit="т")
    result["items"] = []
    cards = {c["id"]: c for c in build_cards(p, result, {p.index: p}, {})}
    assert cards["quantity"]["state"] == "possible_mismatch"
    assert cards["input:area"]["state"] == "needs_facts"
    assert not cards["input:area"].get("found_value")


def test_untyped_or_conflicting_rate_is_not_used_to_relax_quantity_checks():
    p, result = fixture()
    result["items"] = []
    for comment in ("", "Расход 0,7 л/м2", "Расход 0,8 л/м2; расход 0,6 кг/м2"):
        p.comment = comment
        cards = {c["id"]: c for c in build_cards(p, result, {p.index: p}, {})}
        assert "input:area" not in cards
        assert cards["quantity"]["state"] == "possible_mismatch"


def test_ledger_never_calls_verified_input_area_verified_output_mass():
    p, result = fixture()
    result["parameter_cards"] = build_cards(p, result, {p.index: p}, {})
    ledger = build(p, result, {p.index: p}, {})
    fact = ledger["facts"][0]
    assert fact["dimension"] == "area"
    assert fact["quantity_role"] == "calculation_input"
    assert fact["subject_state"] == "verified"
    assert fact["comparison_states"] == [dict(id="input:area", state="confirmed")]


def test_area_and_direct_mass_can_both_be_validated_without_cross_binding():
    p, result = fixture()
    text = "Масса розлива 0.345288 т"
    result["sources"].append(
        {**source("mass", text=text), "version_id": "pdf", "page": 2}
    )
    result["parameter_proposals"].append(
        {
            **proposal("quantity", "0.345288", "т", text, id="mass"),
            "scope_state": "verified",
            "scope_quote": text,
        }
    )
    result["items"][1].update(state="confirmed", project_source_ids=["mass"])
    result["parameter_cards"] = build_cards(p, result, {p.index: p}, {})
    cards = {c["id"]: c for c in result["parameter_cards"]}
    assert cards["quantity"]["state"] == cards["input:area"]["state"] == "confirmed"
    assert cards["quantity"]["source_ids"] == ["mass"]
    assert cards["input:area"]["source_ids"] == ["p1"]
    facts = build(p, result, {p.index: p}, {})["facts"]
    assert {(f["dimension"], f["quantity_role"]) for f in facts} == {
        ("area", "calculation_input"),
        ("mass", "output"),
    }


def test_quantity_contract_is_navigation_not_a_mass_proof():
    p, _ = fixture()
    contract = quantity_contract(p)
    assert contract["project_input"]["unit"] == "м2"
    assert contract["output_unit"] == "т" and not contract["verified"]
    assert contract["density_required"]
    p.comment = ""
    assert quantity_contract(p) is None


def test_area_input_does_not_satisfy_a_condition_on_output_quantity():
    p, result = fixture()
    norm = source("n1", "normative", "Коэффициент 0,8. При количестве более 400 м2.")
    result["sources"].append(norm)
    result["parameter_proposals"].append(
        proposal(
            "coefficient",
            "0.8",
            "",
            "Коэффициент 0,8.",
            id="n1",
            rules=[
                dict(
                    parameter="quantity",
                    operator="gt",
                    value="400",
                    unit="м2",
                    quote="При количестве более 400 м2.",
                )
            ],
        )
    )
    cards = {c["id"]: c for c in build_cards(p, result, {p.index: p}, {})}
    assert cards["input:area"]["state"] == "confirmed"
    assert cards["factor:0:0.8"]["state"] == "needs_facts"
    assert "недостаточно" in cards["factor:0:0.8"]["conditions"][0]
