from copy import deepcopy
import json
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_unit_measure import apply, TOPIC


@pytest.mark.parametrize('case', ['valid', 'bad_hash', 'conflicting_definitions', 'compound_measure', 'no_binding'])
def test_literal_candidate_measure_never_selects_norm(case):
    from normative_source_registry import full_text_handle
    from normative_unit_measure import literal_definition_card

    position, _ = setup()
    code = '15-01-001-01'
    text = f'Измеритель: 1000 м2\n{code} Устройство покрытия'
    if case == 'compound_measure':
        text = text.replace('1000 м2', '1000 м2; 100 м3')
    definition = dict(id=full_text_handle('definition', text), kind='normative', label=f'ГЭСН · {code}',
                      page=5, source_id='pdf-pin', text=text, origin='extracted_native_unreviewed')
    if case == 'bad_hash':
        definition['text'] += '.'
    condition = dict(id='condition', kind='normative', source_id='pdf-pin', page=2,
                     text='Условие техчасти', label='ГЭСН · техчасть', origin='native')
    result = dict(execution_state='complete', sources=[definition, condition], items=[],
                  evidence_trace=[dict(kind='conditional_norm_selection_v1', reason='base_selection_ambiguous',
                                       allowed_base_codes=[code], condition_source_id='condition')])
    if case == 'conflicting_definitions':
        result['sources'].append(dict(definition, id=full_text_handle('other', definition['text']), page=6))
    if case == 'no_binding':
        result['evidence_trace'].clear()
    before = deepcopy(result)
    card = literal_definition_card(result, position)
    assert result == before
    if case == 'valid':
        assert card and card['state'] == 'confirmed' and card['found_value'] == '1000'
        assert not card['required']
        assert card['source_ids'] == [definition['id']]
    else:
        assert card is None


def setup():
    data = json.loads(
        (Path(__file__).parent / "fixtures/work-norm-measure22.json").read_text(
            encoding="utf-8"
        )
    )
    return VorPosition.model_validate(data["position"]), data["result"]


def test_actual_measure_is_confirmed_independently_of_width_and_material_rate():
    position, result = setup()
    original = deepcopy(result)
    assert apply(position, result, {22: position}, {})
    item = next(i for i in result["items"] if i["topic"] == TOPIC)
    assert item["state"] == "confirmed" and item["claim_ids"] == ["unit"]
    assert item["project_source_ids"] and item["normative_source_ids"] == ["n2"]
    assert [i for i in result["items"] if i["claim_ids"] != ["unit"]] == [
        i for i in original["items"] if i["claim_ids"] != ["unit"]
    ]
    assert result["state"] == original["state"] == "needs_facts"
    frozen = deepcopy(result)
    assert apply(position, result, {22: position}, {}) and result == frozen


@pytest.mark.parametrize(
    "change",
    [
        "missing_quantity",
        "missing_scale",
        "wrong_unit",
        "bad_quote",
        "wrong_norm",
        "uncertain_norm",
        "material_child",
        "conflicting_unit",
        "compound_claim",
        "wrong_source_kind",
    ],
)
def test_unverified_or_ambiguous_measure_not_promoted(change):
    position, result = setup()
    scale = next(c for c in result["parameter_cards"] if c["id"] == "unit_scale:n2")
    quantity = next(c for c in result["parameter_cards"] if c["id"] == "quantity")
    item = next(i for i in result["items"] if i["claim_ids"] == ["unit"])
    relation = {}
    if change == "missing_quantity":
        quantity["state"] = "needs_facts"
    elif change == "missing_scale":
        scale["state"] = "needs_facts"
    elif change == "wrong_unit":
        position.unit = "т"
    elif change == "bad_quote":
        scale["quote"] = "Измеритель: 10 м2"
    elif change == "wrong_norm":
        item["normative_source_ids"] = ["n7"]
    elif change == "uncertain_norm":
        next(s for s in result["sources"] if s["id"] == "n2")["origin"] = (
            "uncertain_table"
        )
    elif change == "material_child":
        relation = {"parent": {"index": 99}}
    elif change == "conflicting_unit":
        item["state"] = "possible_mismatch"
    elif change == "compound_claim":
        item["claim_ids"].append("normative:applicability")
    else:
        next(s for s in result["sources"] if s["id"] == quantity["source_ids"][0])[
            "kind"
        ] = "normative"
    positions = {22: position, 99: position.model_copy(update={"index": 99})}
    assert not apply(position, result, positions, relation)
    assert not any(i.get("topic") == TOPIC for i in result["items"])


def test_different_quantity_does_not_make_matching_units_unknown():
    position, result = setup()
    quantity = next(c for c in result["parameter_cards"] if c["id"] == "quantity")
    quantity["state"] = "possible_mismatch"
    assert apply(position, result, {22: position}, {})
    assert (
        next(i for i in result["items"] if i.get("claim_ids") == ["unit"])["state"]
        == "confirmed"
    )
    assert quantity["state"] == "possible_mismatch" and result["state"] == "needs_facts"


def test_projection_revoked_if_numeric_evidence_changes_on_replay():
    position, result = setup()
    assert apply(position, result, {22: position}, {})
    next(c for c in result["parameter_cards"] if c["id"] == "unit_scale:n2")[
        "state"
    ] = "needs_facts"
    assert not apply(position, result, {22: position}, {})
    assert not any(i.get("topic") == TOPIC for i in result["items"])
    assert (
        next(i for i in result["items"] if i["claim_ids"] == ["unit"])["state"]
        == "needs_facts"
    )
