from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_parameters import enrich
from normative_parent_operand import parent_operand
from test_normative_resource_consumption import fixture


def setup():
    work, owner, result = fixture()
    owner.volume = "2.092432"
    work.volume = "2.1527"
    work.formula = "2.09*1.03"
    work.calculation = {"expression": work.formula}
    for position in [work, owner]:
        position.position_source = SimpleNamespace(
            row=100 + position.index,
            format="xlsx",
            sheet="Work",
            document_sha256="a" * 64,
        )
    relation = {"parent": {"index": owner.index}}
    return work, owner, result, relation


def test_rounded_parent_input_is_workbook_provenance_not_pd_quantity_or_rate():
    work, owner, result, relation = setup()
    frozen = deepcopy(result)
    after = enrich(work, result, {0: owner, 1: work}, relation)
    cards = {c["id"]: c for c in after["parameter_cards"]}
    assert cards["factor:1:2.09"]["state"] == "confirmed"
    assert cards["factor:1:2.09"]["role"] == "calculation"
    assert cards["factor:1:1.03"]["label"] == "Нормативный расход"
    assert (
        cards["quantity"]["state"] == "needs_facts" and after["state"] == "needs_facts"
    )
    assert result == frozen
    proof = next(
        e
        for e in after["evidence_trace"]
        if e["kind"] == "rounded_parent_workbook_operand_v1"
    )
    assert (
        proof["parent"]["quantity"] == "2.092432" and proof["rounded_parent"] == "2.09"
    )
    assert (
        len(
            [
                e
                for e in enrich(work, after, {0: owner, 1: work}, relation)[
                    "evidence_trace"
                ]
                if e["kind"] == proof["kind"]
            ]
        )
        == 1
    )
    owner.volume = "3.092432"
    rebuilt = enrich(work, after, {0: owner, 1: work}, relation)
    assert (
        next(c for c in rebuilt["parameter_cards"] if c["id"] == "factor:1:2.09")[
            "state"
        ]
        == "needs_facts"
    )
    assert not any(e["kind"] == proof["kind"] for e in rebuilt["evidence_trace"])


@pytest.mark.parametrize(
    "change",
    [
        "wrong_parent",
        "wrong_quantity",
        "wrong_book",
        "wrong_sheet",
        "same_row",
        "no_source",
        "wrong_units",
        "unproved_rate",
        "unproved_arithmetic",
        "extra_operation",
        "divisor",
        "integer",
    ],
)
def test_no_matching_by_a_convenient_number_alone(change):
    work, owner, result, relation = setup()
    expression = work.formula
    value = "2.09"
    arithmetic = True
    if change == "wrong_parent":
        relation["parent"]["index"] = 5
    elif change == "wrong_quantity":
        owner.volume = "0.067048"
    elif change == "wrong_book":
        owner.position_source.document_sha256 = "b" * 64
    elif change == "wrong_sheet":
        owner.position_source.sheet = "Other"
    elif change == "same_row":
        owner.position_source.row = work.position_source.row
    elif change == "no_source":
        owner.position_source = None
    elif change == "wrong_units":
        work.unit = "м3"
    elif change == "unproved_rate":
        result["evidence_trace"] = []
    elif change == "unproved_arithmetic":
        arithmetic = False
    elif change == "extra_operation":
        expression = "2.09*1.03+1"
    elif change == "divisor":
        expression = "2.09/1.03"
    else:
        expression = "2*1.03"
        value = "2"
    assert (
        parent_operand(
            work,
            owner,
            result,
            value,
            expression,
            relation,
            arithmetic_confirmed=arithmetic,
        )
        is None
    )
