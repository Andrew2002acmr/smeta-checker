"""A resumed sum must use pinned cells and never become a single-side total."""

from copy import deepcopy

import pytest

from normative_agent import EvidenceRequest
from normative_position_check import (
    AgentAnswer,
    bind_explicit_sum_sources,
    retain_literal_parameters,
)
from test_normative_agent import sid, toolkit


def resumed():
    original = toolkit()
    original.execute(EvidenceRequest(tool="read_table", table_id="t1"))
    again = toolkit()
    again.sources = deepcopy(original.sources)
    return again


def test_archived_operands_restore_exact_pinned_cells_without_ocr():
    tools = resumed()
    before = deepcopy(tools.sources)
    assert not tools.cells
    result = tools.execute(
        EvidenceRequest(tool="sum_cells", operand_ids=[sid("c1"), sid("c2")], unit="м2")
    )
    assert result["value"] == "69.12" and result["scope_verified"] is False
    assert tools.sources[: len(before)] == before
    assert set(tools.cells) == {sid("c1"), sid("c2")}


@pytest.mark.parametrize(
    "change",
    ["invented", "other_version", "not_registered", "wrong_unit", "uncertain_cell"],
)
def test_restoration_does_not_find_replacements_for_unproved_operands(change):
    tools = resumed()
    operands = [sid("c1"), sid("c2")]
    unit = "м2"
    if change == "invented":
        tools.sources[1]["id"] = "invented"
        operands[0] = "invented"
    elif change == "other_version":
        tools.sources[1]["version_id"] = "other"
    elif change == "not_registered":
        tools.sources = [s for s in tools.sources if s["id"] != sid("c1")]
    elif change == "wrong_unit":
        unit = "т"
    else:
        tools.index.tables[0][0]["cells"][1]["quality"] = "uncertain"
    if change == "uncertain_cell":
        with pytest.raises(ValueError, match="Source registry identity conflict"):
            tools.execute(
                EvidenceRequest(tool="sum_cells", operand_ids=operands, unit=unit)
            )
    else:
        assert "error" in tools.execute(
            EvidenceRequest(tool="sum_cells", operand_ids=operands, unit=unit)
        )


@pytest.mark.parametrize("scope", ["verified", "unresolved"])
def test_explicit_sum_can_bind_generated_handle_without_another_model_request(scope):
    tools = resumed()
    answer = AgentAnswer(
        summary="Selected operands",
        items=[],
        parameters=[
            dict(
                parameter="quantity",
                operation="sum",
                operand_source_ids=[sid("c1"), sid("c2")],
                value="69.12",
                unit="м2",
                source_id="",
                quote="12,34 + 56,78",
                scope_quote="Площадь, м2",
                scope_state=scope,
            )
        ],
    )
    bind_explicit_sum_sources(answer, tools.sources, tools)
    proposal = answer.parameters[0]
    assert proposal.source_id.startswith("agent:sum:")
    assert proposal.scope_state == scope
    assert proposal.operand_source_ids == [sid("c1"), sid("c2")]
    assert "Площадь, м2" in proposal.scope_quote
    assert tools.events[-1]["kind"] == "explicit_sum_source_binding_v1"


@pytest.mark.parametrize(
    "change", ["wrong_value", "fabricated_handle", "single_direct"]
)
def test_binding_never_repairs_a_different_value_reference_or_composition(change):
    tools = resumed()
    data = dict(
        parameter="quantity",
        operation="sum",
        operand_source_ids=[sid("c1"), sid("c2")],
        value="69.12",
        unit="м2",
        quote="69.12",
        scope_state="verified",
    )
    if change == "wrong_value":
        data["value"] = "99"
    elif change == "fabricated_handle":
        data["source_id"] = "invented"
    else:
        data.update(operation="direct", source_id=sid("c1"))
    answer = AgentAnswer(summary="Selected operands", items=[], parameters=[data])
    before = deepcopy(answer)
    bind_explicit_sum_sources(answer, tools.sources, tools)
    assert answer == before


def test_explicit_unresolved_sum_is_not_replaced_by_old_single_side():
    tools = resumed()
    tools.parameter_evidence = dict(
        quantity=dict(
            parameter="quantity",
            operation="direct",
            source_id=sid("c2"),
            value="56.78",
            unit="м2",
            quote="56,78",
            scope_quote="56,78",
            scope_state="verified",
        )
    )
    answer = AgentAnswer(
        summary="New composition",
        items=[],
        parameters=[
            dict(
                parameter="quantity",
                operation="sum",
                operand_source_ids=[sid("c1"), "not-read"],
                value="69.12",
                unit="м2",
                scope_state="verified",
            )
        ],
    )
    before = deepcopy(answer)
    retain_literal_parameters(answer, tools.sources, tools)
    assert answer == before
