"""Project observations survive comparison outcomes without relaxing scope."""
from copy import deepcopy

import pytest

from normative_fact_ledger import build, shared
from normative_parameters import build_cards
from normative_service import position_key
from test_normative_parameters import position, source


def fixture(declared="400", scope_state="verified"):
    p = position(volume=declared, unit="шт")
    p.work_name = "Монтаж элементов конструкции К-1"
    text = "Конструкция К-1, монтаж элементов, количество 400 шт"
    result = dict(pipeline_version=27, sources=[dict(source(text=text), version_id="pdf", page=1)],
        parameter_proposals=[dict(parameter="quantity", source_id="p1", value="400", unit="шт",
            quote="количество 400 шт", scope_quote=text if scope_state == "verified" else "",
            scope_state=scope_state)], items=[])
    result["parameter_cards"] = build_cards(p, result, {p.index: p}, {})
    return p, result


@pytest.mark.parametrize(("declared", "scope_state", "comparison", "binding"), [
    ("400", "verified", "confirmed", "verified"),
    ("500", "verified", "possible_mismatch", "verified"),
    ("400", "unresolved", "needs_facts", "unresolved"),
])
def test_literal_binding_and_comparison_are_independent(declared, scope_state, comparison, binding):
    p, result = fixture(declared, scope_state)
    before = deepcopy(result)
    fact = build(p, result, {p.index: p}, {})["facts"][0]
    assert fact["literal_state"] == "verified"
    assert fact["subject_binding_state"] == fact["subject_state"] == binding
    assert fact["owner_position"] == (p.index if binding == "verified" else None)
    assert fact["comparison_state"] == comparison
    assert fact["calculation_state"] == "not_applicable"
    assert result == before


def test_project_fact_identity_survives_a_changed_declared_quantity():
    matching, correct = fixture()
    different, incorrect = fixture("500")
    a = build(matching, correct, {0: matching}, {})["facts"][0]
    b = build(different, incorrect, {0: different}, {})["facts"][0]
    assert a["id"] == b["id"]
    assert a["owner_position"] == b["owner_position"] == 0
    assert a["comparison_state"] != b["comparison_state"]


def test_unresolved_calculation_keeps_found_literal_and_records_missing_inputs():
    p, result = fixture()
    result["parameter_proposals"].append(dict(parameter="quantity", source_id="",
        operation="sum", operand_source_ids=["p1", "missing"], value="500", unit="шт",
        quote="", scope_quote="", scope_state="unresolved"))
    result["parameter_cards"] = build_cards(p, result, {0: p}, {})
    ledger = build(p, result, {0: p}, {})
    assert len(ledger["facts"]) == 1
    assert ledger["facts"][0]["value"] == "400"
    assert ledger["facts"][0]["literal_state"] == "verified"
    assert ledger["calculations"][0]["calculation_state"] == "unresolved"
    assert ledger["calculations"][0]["operand_source_ids"] == ["p1", "missing"]


@pytest.mark.parametrize("change", ["scope", "quote", "quality", "dimension", "card_anchor"])
def test_saved_green_cards_do_not_replace_binding_validation(change):
    p, result = fixture()
    if change == "scope":
        result["parameter_proposals"][0]["scope_state"] = "unresolved"
    elif change == "quote":
        result["parameter_proposals"][0]["scope_quote"] = "Другая конструкция, количество 400 шт"
    elif change == "quality":
        result["sources"][0].update(origin="ocr", numeric_verified=False)
    elif change == "dimension":
        result["parameter_cards"][0].update(found_unit="м2")
    else:
        result["parameter_cards"][0]["source_anchors"][0]["quote"] = "invented"
    fact = build(p, result, {0: p}, {})["facts"][0]
    assert fact["subject_binding_state"] == "unresolved"
    assert fact["owner_position"] is None
    if change in {"scope", "quote", "dimension", "card_anchor"}:
        assert fact["literal_state"] == "verified"


def test_mismatched_project_quantity_can_be_reused_without_transferring_verdict():
    from test_prior_run_project_facts import fixture as shared_fixture

    context, record, _, positions, graph, row = shared_fixture()
    result = row["payload"]["position_check"]
    # This donor's own VOR quantity differs; the saved project geometry remains.
    positions[0].volume = "999"
    row["payload"]["position_key"] = position_key(positions[1])
    result["parameter_cards"] = build_cards(positions[1], result, positions, row["payload"]["work_relations"])
    assert next(c for c in result["parameter_cards"] if c["id"] == "quantity")["state"] == "possible_mismatch"
    bundle = shared(context, record, positions, 1, graph)
    assert {c["proposal"]["parameter"] for c in bundle["candidates"]} == {"quantity", "thickness"}
    assert all(c["proposal"]["scope_state"] == "unresolved" for c in bundle["candidates"])
    assert "comparison_state" not in str(bundle)


def test_changed_comparison_does_not_require_a_previously_matching_saved_card_state():
    p, result = fixture()
    p.volume = "500"
    fact = build(p, result, {0: p}, {})["facts"][0]
    assert result["parameter_cards"][0]["state"] == "confirmed"
    assert fact["comparison_state"] == "possible_mismatch"
    assert fact["literal_state"] == fact["subject_binding_state"] == "verified"


def test_tampered_alias_does_not_become_a_verified_fact():
    from normative_source_identity import shared_source

    p, result = fixture()
    alias = shared_source(result["sources"][0], 2)
    result["sources"] = [alias]
    result["parameter_proposals"][0]["source_id"] = alias["id"]
    result["parameter_cards"] = build_cards(p, result, {0: p}, {})
    assert build(p, result, {0: p}, {})["facts"][0]["literal_state"] == "verified"
    alias["rects"] = [[0.1, 0.2, 0.4, 0.8]]
    fact = build(p, result, {0: p}, {})["facts"][0]
    assert fact["literal_state"] == fact["subject_binding_state"] == "unresolved"


def test_generated_downgrade_of_mixed_item_does_not_revoke_a_proved_project_fact():
    from normative_parameters import enrich

    p, result = fixture("500")
    result["items"] = [dict(topic="Mixed comparison", state="confirmed", explanation="Read source",
        required=True, claim_ids=["quantity", "thickness"], project_source_ids=["p1"], normative_source_ids=[])]
    enriched = enrich(p, result, {0: p}, {})
    assert enriched["items"][0]["state"] == "needs_facts"
    fact = build(p, enriched, {0: p}, {})["facts"][0]
    assert fact["literal_state"] == fact["subject_binding_state"] == "verified"
    assert fact["comparison_state"] == "possible_mismatch"
    enriched["items"][0]["explanation"] = "A new independent scope dispute"
    assert build(p, enriched, {0: p}, {})["facts"][0]["subject_binding_state"] == "unresolved"
