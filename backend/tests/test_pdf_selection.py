"""Selections must not include neighbouring rows or gaps between text ranges."""

import pymupdf
import pytest
from pdf_selection import resolve_selection
from workspace_models import SpecifyBasisRequest


def request(page, box, mode="area", rects=None):
    rect = [
        box[0] / page.rect.width,
        box[1] / page.rect.height,
        box[2] / page.rect.width,
        box[3] / page.rect.height,
    ]
    return SpecifyBasisRequest(
        version_id="test", page=1, rect=rect, mode=mode, rects=rects
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_glyph_centres_exclude_next_line_even_when_ascenders_touch_frame(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((50, 100), "SELECTED 222.78", fontsize=11)
        page.insert_text((50, 112), "NEXT ROW 95.91", fontsize=11)
        box = pymupdf.Rect(40, 85, 350, 105)
        page.set_rotation(rotation)
        result = resolve_selection(
            page, request(page, box * page.rotation_matrix), "test"
        )
        assert "222.78" in result["quote"]
        assert "NEXT" not in result["quote"] and "95.91" not in result["quote"]


def test_text_ranges_keep_gap_and_unselected_middle_line_out():
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        for y, text in [(100, "FIRST 10"), (120, "UNSELECTED 999"), (140, "LAST 20")]:
            page.insert_text((50, y), text, fontsize=11)
        boxes = [page.search_for(text)[0] for text in ("FIRST 10", "LAST 20")]
        rects = [[b.x0 / 400, b.y0 / 400, b.x1 / 400, b.y1 / 400] for b in boxes]
        payload = request(page, [40, 80, 350, 145], mode="text", rects=rects)
        result = resolve_selection(page, payload, "test")
        assert "FIRST" in result["quote"] and "LAST" in result["quote"]
        assert "UNSELECTED" not in result["quote"] and "999" not in result["quote"]
        assert len(result["region"]["highlights"]) == 2


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_table_click_selects_one_row_and_all_its_cells(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=400)
        for x in (50, 350, 420, 550):
            page.draw_line((x, 80), (x, 180))
        for y in (80, 110, 145, 180):
            page.draw_line((50, y), (550, y))
        for x, y, text in [
            (60, 100, "Work"),
            (360, 100, "Unit"),
            (430, 100, "Quantity"),
            (60, 132, "SELECTED"),
            (360, 132, "m"),
            (430, 132, "222.78"),
            (60, 167, "NEXT ROW"),
            (360, 167, "m"),
            (430, 167, "95.91"),
        ]:
            page.insert_text((x, y), text, fontsize=11)
        page.set_rotation(rotation)
        click = pymupdf.Rect(100, 125, 101, 126) * page.rotation_matrix
        result = resolve_selection(page, request(page, click, mode="row"), "test")
        assert "SELECTED" in result["quote"] and "222.78" in result["quote"]
        assert "NEXT" not in result["quote"] and "95.91" not in result["quote"]
        assert len(result["region"]["highlights"]) == 3


def test_no_text_scan_area_is_preserved_but_text_mode_is_rejected():
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        payload = request(page, [20, 20, 100, 100])
        assert resolve_selection(page, payload, "test")["quote"] == ""
        with pytest.raises(ValueError):
            resolve_selection(
                page,
                request(page, [20, 20, 100, 100], mode="text", rects=[payload.rect]),
                "test",
            )
