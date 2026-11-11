from copy import deepcopy
import json

import pytest

from pdf_table_checks import check_table


def table(area="374,30", volume="74,86"):
    def cell(key, row, column, text, role="cell", colspan=1, headers=()):
        return dict(cell_id=key, row=row, column=column, text=text, role=role,
                    colspan=colspan, rowspan=1, header_ids=list(headers),
                    bbox=[column * 100, row * 20, (column + colspan) * 100, (row + 1) * 20])
    return dict(bbox=[0, 0, 200, 100], origin="native", header_rows=2,
                row_count=3, col_count=2, cells=[
                    cell("layer", 0, 0, "Левая сторона, слой основания h=0,20м", "header", 2),
                    cell("area_header", 1, 0, "м²", "header"),
                    cell("volume_header", 1, 1, "м³", "header"),
                    cell("area", 2, 0, area, headers=("layer", "area_header")),
                    cell("volume", 2, 1, volume, headers=("layer", "volume_header")),
                ])


def products(result):
    return [c for c in result["checks"] if c["kind"] == "volume_area_thickness"]


def test_lost_decimal_is_a_conflict_with_physical_inputs_not_a_correction():
    source = table(volume="7486")
    result = check_table(source)
    check = products(result)[0]
    assert result["state"] == "conflict"
    assert check["operands"] == {"S": "374.30", "V": "7486", "h_m": "0.20"}
    assert check["calculated_volume"] == "74.8600"
    assert {c["cell_id"] for c in check["source_cells"]} == {"area", "volume"}
    assert check["thickness_header_id"] == "layer"
    assert source["cells"][-1]["text"] == "7486"


def test_valid_rounding_uses_displayed_precision_not_relative_fudge():
    result = check_table(table("10,03", "2,01"))
    check = products(result)[0]
    assert check["state"] == "consistent"
    assert check["roundoff_tolerance"] == "0.00600"
    assert check_table(table("10,03", "2,02"))["state"] == "conflict"


def test_wrong_side_or_layer_cannot_pair_even_when_arithmetic_matches():
    source = table()
    other = deepcopy(source["cells"][0])
    other.update(cell_id="right_layer", text="Правая сторона, другой слой h=0,20м")
    source["cells"].insert(1, other)
    source["cells"][-1]["header_ids"] = ["right_layer", "volume_header"]
    result = check_table(source)
    assert result["state"] == "not_checkable"
    assert all(c["state"] == "not_checkable" for c in products(result))


def test_multiple_same_scope_numeric_cells_are_ambiguous():
    source = table()
    extra = deepcopy(source["cells"][-1])
    extra["cell_id"] = "second_volume"
    source["cells"].append(extra)
    assert products(check_table(source))[0]["reason"] == "ambiguous_or_missing_same_scope_area_volume_pair"


@pytest.mark.parametrize("token", ["74 86", "74,8,6", "7о,86", "74.86.0", "74/86", "NaN", "1e2"])
def test_malformed_ocr_tokens_are_not_normalized(token):
    result = check_table(table(volume=token))
    assert result["state"] == "not_checkable"
    assert "malformed_or_negative_numeric_literal" in {c["reason"] for c in products(result)}


@pytest.mark.parametrize("change,reason", [
    ("unit", "unknown_or_unproved_literal_unit"),
    ("thickness", "missing_or_ambiguous_literal_thickness"),
    ("geometry", "invalid_source_geometry"),
    ("header_geometry", "invalid_header_geometry"),
    ("span", "header_span_does_not_cover_cell"),
])
def test_missing_proof_or_invalid_geometry_is_explicit(change, reason):
    source = table()
    if change == "unit":
        source["cells"][2]["text"] = "объем"
    elif change == "thickness":
        source["cells"][0]["text"] = "Основание"
    elif change == "geometry":
        source["geometry_validation"] = dict(requires_source_review=True)
    elif change == "header_geometry":
        source["cells"][0]["geometry_validation"] = dict(requires_source_review=True)
    else:
        source["cells"][0]["colspan"] = 1
    result = check_table(source)
    assert result["state"] == "not_checkable"
    assert reason in {c["reason"] for c in products(result)}


def test_arithmetic_never_promotes_ocr_and_does_not_mutate_or_alias_source():
    source = table()
    for cell in source["cells"]:
        cell.update(numeric_verified=False, quality="uncertain", variants=[{"text": cell["text"]}])
    before = deepcopy(source)
    result = check_table(source)
    assert result["state"] == "consistent"
    assert source == before
    assert result["ocr_verified"] is False
    assert all(c["ocr_verified"] is False for c in result["checks"])
    assert "numeric_verified" not in json.dumps(result)
    json.dumps(result, allow_nan=False)
    products(result)[0]["source_cells"][0]["bbox"][0] = -10
    assert source == before


def test_ambiguous_subtotal_scopes_are_not_summed():
    source = table()
    subtotal = deepcopy(source["cells"][-1])
    subtotal.update(cell_id="subtotal", row=3, text="Итого")
    source["cells"].append(subtotal)
    totals = [c for c in check_table(source)["checks"] if c["kind"] == "totals"][0]
    assert totals["state"] == "not_checkable"
    assert totals["source_cell_ids"] == ["subtotal"]
    assert "subtotal_exclusion" in totals["reason"]


def test_empty_table_has_explicit_limitations():
    result = check_table({"cells": []})
    assert result["state"] == "not_checkable"
    assert result["limitations"]


def blank_unit_proof_table():
    """Raster representation: distinct blank unit cells and label leaves."""
    from pdf_table_verification import VERSION

    source = table()
    labels = []
    source["column_units"] = {}
    source.update(origin="ocr", header_rows=3, row_count=4)
    for column, key, label, unit in [
        (0, "area_header", "Площадь,", "м2"),
        (1, "volume_header", "Объем,", "м3"),
    ]:
        header = next(c for c in source["cells"] if c["cell_id"] == key)
        heading = deepcopy(header)
        heading.update(cell_id=key + "_label", text=label, header_ids=["layer"])
        labels.append(heading)
        header.update(row=2, text="", bbox=[column * 100, 40, (column + 1) * 100, 60])
        source["column_units"][str(column)] = dict(
            version=VERSION, column=column, header_id=key, bbox=list(header["bbox"]), unit=unit,
            variants=[dict(scale=scale, words=[dict(text=unit, conf=96)]) for scale in (1, 1.35)],
        )
        body = next(c for c in source["cells"] if c.get("role") == "cell" and c["column"] == column)
        body.update(row=3, bbox=[column * 100, 60, (column + 1) * 100, 80])
        body["header_ids"].append(heading["cell_id"])
    source["cells"].extend(labels)
    return source


def test_blank_proved_unit_headers_and_measurement_leaves_do_not_split_scope():
    source = blank_unit_proof_table()
    result = check_table(source)
    assert result["state"] == "consistent"
    check = next(c for c in products(result) if c["state"] == "consistent")
    assert {h["cell_id"] for h in check["source_headers"]} == {
        "layer", "area_header", "volume_header", "area_header_label", "volume_header_label",
    }


def test_unknown_blank_header_is_not_dropped_like_a_proved_unit_header():
    source = blank_unit_proof_table()
    unknown = deepcopy(source["cells"][-1])
    unknown.update(cell_id="unknown_scope", text="")
    source["cells"].append(unknown)
    next(c for c in source["cells"] if c["cell_id"] == "volume")["header_ids"].append("unknown_scope")
    assert check_table(source)["state"] == "not_checkable"


def test_material_text_is_not_mistaken_for_a_measurement_leaf():
    source = blank_unit_proof_table()
    next(c for c in source["cells"] if c["cell_id"] == "volume_header_label")["text"] = "Объем, слой 2"
    assert check_table(source)["state"] == "not_checkable"

def test_total_rows_are_not_checked_as_volume_products():
    source = table()
    source["cells"].append(dict(cell_id="total_label", row=3, column=0, text="Итого по пикету:",
        role="cell", colspan=1, rowspan=1, header_ids=[], bbox=[0, 60, 100, 80]))
    source["cells"].append(dict(cell_id="total_area", row=3, column=0, text="377,76",
        role="cell", colspan=1, rowspan=1, header_ids=("layer", "area_header"), bbox=[0, 60, 100, 80]))
    source["cells"].append(dict(cell_id="total_volume", row=3, column=1, text="18,88",
        role="cell", colspan=1, rowspan=1, header_ids=("layer", "volume_header"), bbox=[100, 60, 200, 80]))
    source["row_count"] = 4
    result = check_table(source)
    flagged = [c for c in products(result)
               if {x["cell_id"] for x in c["source_cells"]} & {"total_area", "total_volume"}]
    assert flagged and all(c["state"] == "not_checkable" for c in flagged)
    assert all(c["reason"] == "total_row_product_not_applicable_use_summation" for c in flagged)
    assert all(c.get("row_kind") == "subtotal" for c in flagged)
