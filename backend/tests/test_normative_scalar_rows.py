"""Native scalar rows remain literal facts with unverified engineering scope."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_scalar_rows import (
    observations,
    verified_components,
    proposal_matches,
    source_id,
)
from normative_source_identity import shared_source


def fixture():
    cells = []
    for row, texts in enumerate(
        (
            ("№", "Наименование работ", "Единица измерения", "Количество"),
            ("4", "Разборка пути РТ62", "м", "75"),
        )
    ):
        for column, text in enumerate(texts):
            cells.append(
                dict(
                    cell_id=f"c{row}-{column}",
                    role="header" if row == 0 else "cell",
                    row=row,
                    column=column,
                    colspan=1,
                    rowspan=1,
                    origin="native",
                    quality="usable",
                    text=text,
                    bbox=[column * 10, row * 10, column * 10 + 10, row * 10 + 10],
                    header_ids=[] if row == 0 else [f"c0-{column}"],
                )
            )
    table = dict(table_id="t1", origin="native", row_count=2, col_count=4, cells=cells)
    index = SimpleNamespace(
        metadata=dict(
            page_geometry={"0": dict(width=40, height=20, matrix=[1, 0, 0, 1, 0, 0])}
        )
    )
    return index, table


def collect(index, table):
    return observations(
        index, "pdf", 1, table, [c for c in table["cells"] if c["row"] == 1], "Document"
    )


def test_single_native_scalar_row_has_full_server_provenance_and_compact_preview():
    index, table = fixture()
    before = deepcopy(table)
    records = collect(index, table)
    assert len(records) == 1
    source, preview = records[0]["source"], records[0]["preview"]
    assert verified_components(source) == [dict(value="75", unit="м")]
    assert source["origin"] == "native_row_measurement"
    assert (
        preview["scope_verified"] is False and preview["recognition_verified"] is True
    )
    assert preview["read_request"]["column_ids"] == [2, 3]
    assert "scalar_row_context" not in preview and "row_cells" not in str(preview)
    assert len(source["scalar_row_context"]["source_anchors"]) == 2
    assert len(source["scalar_row_context"]["header_anchors"]) == 2
    assert table == before
    proposal = dict(
        parameter="quantity", value="75", unit="м", scope_state="unresolved"
    )
    assert proposal_matches(proposal, source)
    assert verified_components(shared_source(source, 2)) == verified_components(source)
    assert collect(index, table)[0]["source"]["id"] == source["id"]


@pytest.mark.parametrize(
    "change",
    [
        "unit_ocr",
        "value_ocr",
        "header_ocr",
        "description_ocr",
        "table_ocr",
        "rate",
        "pair",
        "range",
        "non_numeric",
        "uncertain",
        "review_geometry",
        "crossrow",
        "rowspan",
        "ambiguous_header",
        "missing_header",
        "wrong_header_span",
        "body_overlap",
        "header_bbox",
    ],
)
def test_acquisition_rejects_ocr_ambiguous_or_non_scalar_physical_rows(change):
    index, table = fixture()
    unit, value = table["cells"][6:8]
    if change == "unit_ocr":
        unit["origin"] = "ocr"
    elif change == "value_ocr":
        value["origin"] = "ocr"
    elif change == "header_ocr":
        table["cells"][3]["origin"] = "ocr"
    elif change == "description_ocr":
        table["cells"][5]["origin"] = "ocr"
    elif change == "table_ocr":
        table["origin"] = "ocr"
    elif change == "rate":
        unit["text"] = "кг/м2"
    elif change == "pair":
        unit["text"], value["text"] = "шт/т", "126/29,61"
    elif change == "range":
        value["text"] = "70-75"
    elif change == "non_numeric":
        value["text"] = "75 м"
    elif change == "uncertain":
        value["quality"] = "uncertain"
    elif change == "review_geometry":
        value["geometry_validation"] = dict(requires_source_review=True)
    elif change == "crossrow":
        unit["row"] = 2
    elif change == "rowspan":
        unit["rowspan"] = value["rowspan"] = 2
    elif change == "ambiguous_header":
        table["cells"].append(dict(table["cells"][3], cell_id="duplicate-header"))
    elif change == "missing_header":
        value["header_ids"] = []
    elif change == "wrong_header_span":
        table["cells"][3]["colspan"] = 2
    elif change == "body_overlap":
        table["cells"].append(dict(value, cell_id="another-quantity"))
    else:
        table["cells"][3]["bbox"] = [0, 0, 10, 10]
    assert not collect(index, table)


@pytest.mark.parametrize(
    "change",
    [
        "value",
        "unit",
        "bbox",
        "anchor",
        "header",
        "native_flag",
        "crossrow",
        "scope",
        "rects",
        "identity",
    ],
)
def test_mutated_server_envelopes_fail_even_if_a_new_handle_is_calculated(change):
    index, table = fixture()
    source = collect(index, table)[0]["source"]
    context = source["scalar_row_context"]
    unit = next(
        c for c in context["row_cells"] if c["cell_id"] == context["unit_cell_id"]
    )
    value = next(
        c for c in context["row_cells"] if c["cell_id"] == context["value_cell_id"]
    )
    if change == "value":
        value["text"] = "76"
    elif change == "unit":
        unit["text"] = "т"
    elif change == "bbox":
        value["bbox"] = [0, 0, 3, 3]
    elif change == "anchor":
        context["source_anchors"][1]["literal_text"] = "76"
    elif change == "header":
        context["header_cells"][1]["text"] = "Other quantity"
    elif change == "native_flag":
        value["origin"] = "ocr"
    elif change == "crossrow":
        unit["row"] = 2
    elif change == "scope":
        context["scope_verified"] = True
    elif change == "rects":
        source["rects"] = [[0, 0, 1, 1]]
    else:
        source["page"] = 2
    source["id"] = source_id(context)
    assert not verified_components(source)
    assert not proposal_matches(
        dict(parameter="quantity", value="75", unit="м"), source
    )


def test_decimal_unit_and_proposal_are_not_selected_by_the_vor_quantity():
    index, table = fixture()
    table["cells"][7]["text"] = "570,37"
    source = collect(index, table)[0]["source"]
    assert verified_components(source) == [dict(value="570.37", unit="м")]
    assert not proposal_matches(
        dict(parameter="quantity", value="570.38", unit="м"), source
    )
    assert not proposal_matches(
        dict(parameter="quantity", value="570.37", unit="т"), source
    )
    assert not proposal_matches(
        dict(parameter="thickness", value="570.37", unit="м"), source
    )


def test_missing_page_geometry_never_produces_a_plausible_native_fact():
    index, table = fixture()
    index.metadata = {}
    assert not collect(index, table)


def integrated_fixture():
    from normative_agent import EvidenceTools, EvidenceRequest

    index, table = fixture()
    index.texts = ("Native table",)
    index.tables = ([table],)
    index.pending_ocr, index.errors, index.quality = [], {}, {}
    index.metadata.update(document_version_id="pdf", preparation_complete=True)
    tools = EvidenceTools.from_prepared(index, "pdf")
    read = tools.execute(
        EvidenceRequest(tool="read_table", page=1, table_id="t1", row_ids=[1])
    )
    scalar = next(s for s in tools.sources if s.get("scalar_row_context"))
    return tools, read, scalar


def test_main_tools_emit_compact_native_proposal_and_independent_literal_state():
    from normative_quantity_anchors import quantity_parameter_candidates
    from normative_parameters import build_cards
    from normative_fact_ledger import build
    from test_normative_parameters import position

    tools, read, scalar = integrated_fixture()
    row = next(
        r
        for r in read["row_measurements"]
        if r.get("measurement_kind") == "native_scalar_row"
    )
    assert (
        row["source_id"] == scalar["id"] and row["value"] == "75" and row["unit"] == "м"
    )
    candidates = quantity_parameter_candidates(tools.sources, tools.cells)
    selected = [c for c in candidates if c["provenance"]["kind"] == "native_scalar_row"]
    assert len(selected) == 1
    assert "scalar_row_context" not in str(selected)
    proposal = selected[0]["proposal"]
    assert proposal["scope_state"] == "unresolved"
    p = position(volume="75", unit="м")
    p.work_name = "Разборка пути РТ62"
    result = dict(
        pipeline_version=27,
        sources=tools.sources,
        parameter_proposals=[proposal],
        items=[],
    )
    result["parameter_cards"] = build_cards(p, result, {0: p}, {})
    fact = build(p, result, {0: p}, {})["facts"][0]
    assert result["parameter_cards"][0]["state"] == "needs_facts"
    assert (
        fact["literal_state"] == "verified"
        and fact["subject_binding_state"] == "unresolved"
    )


@pytest.mark.parametrize(
    "change",
    [
        "context_missing",
        "context_damage",
        "missing_tag_and_context",
        "alias",
        "wrong_rail",
    ],
)
def test_main_guards_validate_scalar_protocol_even_through_alias_or_lost_metadata(
    change,
):
    from normative_quantity_anchors import quantity_parameter_candidates
    from normative_parameters import build_cards
    from normative_fact_ledger import build
    from normative_parameter_feedback import literal_parameter_issues
    from workspace_models import NormativeParameterProposal
    from test_normative_parameters import position

    _, _, scalar = integrated_fixture()
    source = deepcopy(scalar)
    p = position(volume="75", unit="м")
    p.work_name = "Разборка пути РТ62"
    if change == "context_missing":
        source.pop("scalar_row_context")
    elif change == "context_damage":
        source["scalar_row_context"]["row_cells"][-1]["bbox"] = [0, 0, 3, 3]
    elif change == "missing_tag_and_context":
        source.pop("scalar_row_context")
        source["origin"] = "native"
    elif change == "alias":
        source = shared_source(source, 9)
    else:
        p.work_name = "Разборка пути Р65"
    proposal = NormativeParameterProposal(
        parameter="quantity",
        value="75",
        unit="м",
        source_id=source["id"],
        quote=source["text"],
        scope_quote=source["text"],
        scope_state="verified",
    )
    result = dict(
        pipeline_version=27,
        sources=[source],
        parameter_proposals=[proposal.model_dump()],
        items=[],
    )
    result["parameter_cards"] = build_cards(p, result, {0: p}, {})
    facts = build(p, result, {0: p}, {})["facts"]
    if change == "alias":
        assert not literal_parameter_issues(proposal, source)
        assert result["parameter_cards"][0]["state"] == "confirmed"
        assert (
            facts[0]["literal_state"] == facts[0]["subject_binding_state"] == "verified"
        )
        assert len(quantity_parameter_candidates([source], {})) == 1
    elif change == "wrong_rail":
        assert not literal_parameter_issues(proposal, source)
        assert result["parameter_cards"][0]["state"] == "needs_facts"
        assert (
            facts[0]["literal_state"] == "verified"
            and facts[0]["subject_binding_state"] == "unresolved"
        )
    else:
        assert literal_parameter_issues(proposal, source)
        assert result["parameter_cards"][0]["state"] == "needs_facts"
        assert (
            facts[0]["literal_state"]
            == facts[0]["subject_binding_state"]
            == "unresolved"
        )
        assert not quantity_parameter_candidates([source], {})


def test_row_amount_cannot_be_relabelled_as_layer_thickness():
    from normative_parameter_feedback import literal_parameter_issues
    from normative_parameters import build_cards
    from test_normative_parameters import position
    from workspace_models import NormativeParameterProposal

    _, _, source = integrated_fixture()
    proposal = NormativeParameterProposal(parameter='thickness', value='75', unit='м',
        source_id=source['id'], quote=source['text'], scope_quote=source['text'], scope_state='verified')
    assert literal_parameter_issues(proposal, source)
    p = position(volume='75', unit='м')
    p.work_name = 'Устройство основания h=75 м'
    cards = build_cards(p, dict(pipeline_version=27, sources=[source], items=[],
        parameter_proposals=[proposal.model_dump()]), {0: p}, {})
    assert next(c for c in cards if c['id'] == 'thickness')['state'] == 'needs_facts'
