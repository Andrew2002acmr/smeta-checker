"""Consumer-visible table retrieval: headers survive bounded context and snapshots."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_core import PageTextIndex
from table_context import render_table, resolve_fragment


def test_usable_volume_table_is_not_displaced_by_ocr_mentions():
    from audit_core import audit_text_pages, technical_error

    data = table()
    data["page"] = 5
    texts = ["Объем работ Грунт " * 40] * 4 + [
        "Таблица 1 — Объем работ\n" + render_table(data)[0]
    ]
    index = PageTextIndex(texts, tables=[[], [], [], [], [data]])
    contexts = []

    def ask(context):
        contexts.append(context)
        return technical_error("No provider in this regression")

    audit_text_pages(index, [], ask, "Грунт", "28")
    assert set(contexts[0]) == {5}
    assert "<table" in contexts[0][5]
    assert "Гравийный грунт" in contexts[0][5]


def table():
    return {
        "table_id": "t1",
        "page": 1,
        "row_count": 3,
        "col_count": 3,
        "quality": "usable",
        "origin": "native",
        "bbox": [0, 0, 300, 90],
        "cells": [
            {
                "cell_id": "h0",
                "row": 0,
                "column": 0,
                "rowspan": 2,
                "colspan": 1,
                "text": "Грунт",
                "role": "header",
                "header_ids": [],
                "bbox": [0, 0, 100, 60],
            },
            {
                "cell_id": "h1",
                "row": 0,
                "column": 1,
                "rowspan": 1,
                "colspan": 2,
                "text": "Угол трения, °",
                "role": "header",
                "header_ids": [],
                "bbox": [100, 0, 300, 30],
            },
            {
                "cell_id": "h2",
                "row": 1,
                "column": 1,
                "rowspan": 1,
                "colspan": 1,
                "text": "Сухой",
                "role": "header",
                "header_ids": ["h1"],
                "bbox": [100, 30, 200, 60],
            },
            {
                "cell_id": "h3",
                "row": 1,
                "column": 2,
                "rowspan": 1,
                "colspan": 1,
                "text": "Водонасыщенный",
                "role": "header",
                "header_ids": ["h1"],
                "bbox": [200, 30, 300, 60],
            },
            {
                "cell_id": "v0",
                "row": 2,
                "column": 0,
                "text": "Гравийный грунт",
                "role": "cell",
                "header_ids": ["h0"],
                "bbox": [0, 60, 100, 90],
            },
            {
                "cell_id": "v1",
                "row": 2,
                "column": 1,
                "text": "28",
                "role": "cell",
                "header_ids": ["h1", "h2"],
                "bbox": [100, 60, 200, 90],
            },
            {
                "cell_id": "v2",
                "row": 2,
                "column": 2,
                "text": "21",
                "role": "cell",
                "header_ids": ["h1", "h3"],
                "bbox": [200, 60, 300, 90],
            },
        ],
    }


def test_merged_header_paths_and_cell_provenance_survive_snapshot():
    index = PageTextIndex(["Гравийный грунт 28 21"], tables=[[table()]])
    restored = PageTextIndex.from_snapshot(index.to_snapshot())
    fragment = restored.fragment(0, "Гравийный грунт", "", "", "21", 800)
    from lxml import html

    rendered = html.fromstring(fragment)
    assert len(rendered.xpath(".//thead/tr")) == 2
    assert rendered.xpath(".//th[@rowspan='2']/text()") == ["Грунт"]
    assert rendered.xpath(".//th[@colspan='2']/text()") == ["Угол трения, °"]
    assert rendered.xpath(".//tbody/tr/td/text()") == ["Гравийный грунт", "28", "21"]
    _, locator = render_table(restored.tables[0][0])
    value = next(item for item in locator["members"] if item["cell_id"] == "v2")
    assert value["header_ids"] == ["h1", "h3"]
    assert value["bbox"] == [200, 60, 300, 90]


def test_bounded_retrieval_never_delivers_partial_structured_row():
    index = PageTextIndex(
        ["обычный текст<table><tr><td>Гравийный грунт 28 21</td></tr></table>"],
        tables=[[table()]],
    )
    full, _ = render_table(index.tables[0][0])
    fragment = index.fragment(0, "грунт", "", "", "21", len(full) - 1)
    assert len(fragment) <= len(full) - 1
    assert "<table" not in fragment
    assert "Гравийный грунт" not in fragment


def test_uncertain_structure_cannot_become_usable_evidence():
    data = table()
    data["quality"] = "uncertain"
    _, locator = render_table(data)
    assert locator["quality"] == "uncertain"


def test_identical_rows_in_different_tables_keep_distinct_evidence_addresses():
    first, second = table(), table()
    second["table_id"] = "t2"
    fragments = dict(render_table(data) for data in (first, second))
    assert len(fragments) == 2
    assert {locator["table_id"] for locator in fragments.values()} == {"t1", "t2"}


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_pdf_merged_header_and_adjacent_rows_keep_separate_values(rotation):
    import pymupdf
    from pdf_text import PDFTextSource

    with pymupdf.open() as doc:
        page = doc.new_page()
        for x in (50, 200, 400):
            page.draw_line((x, 50), (x, 170))
        page.draw_line((300, 80), (300, 170))
        for y in (50, 110, 140, 170):
            page.draw_line((50, y), (400, y))
        page.draw_line((200, 80), (400, 80))
        for x, y, text in [
            (60, 75, "Soil"),
            (210, 70, "Friction"),
            (210, 100, "Dry"),
            (310, 100, "Wet"),
            (60, 130, "Gravel"),
            (210, 130, "28"),
            (310, 130, "21"),
            (60, 160, "Sand"),
            (210, 160, "35"),
            (310, 160, "25"),
        ]:
            page.insert_text((x, y), text)
        page.set_rotation(rotation)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            try:
                index = source.for_position([0], "Gravel", "21", max_ocr_pages=0)
            finally:
                source.close()
    saved = PageTextIndex.from_snapshot(index.to_snapshot())
    grid = saved.tables[0][0]
    by_text = {cell["text"]: cell for cell in grid["cells"] if cell["text"]}
    assert by_text["Soil"]["rowspan"] == 2
    assert by_text["Friction"]["colspan"] == 2
    ids = {cell["cell_id"]: cell["text"] for cell in grid["cells"]}
    assert [ids[key] for key in by_text["21"]["header_ids"]] == ["Friction", "Wet"]
    full = saved.fragment(0, "Gravel", "", "", "21", 8000)
    assert "Gravel" in full and "Sand" in full
    gravel, locator = render_table(grid, 2, 3)
    assert "<td>21</td>" in gravel
    assert "Sand" not in gravel
    assert "<td>25</td>" not in gravel
    assert resolve_fragment([grid], gravel) == locator


def test_multidigit_page_metadata_keeps_canonical_snapshot_hash():
    index = PageTextIndex(
        ["page"] * 15,
        errors={2: "failed", 12: "failed"},
        quality={index: {"state": "pending"} for index in (1, 2, 10, 12)},
        tables=[[] for _ in range(15)],
    )
    restored = PageTextIndex.from_snapshot(index.to_snapshot())
    assert restored.errors == {2: "failed", 12: "failed"}
    assert restored.quality[12]["state"] == "pending"


def test_uncertain_table_cannot_confirm_work_even_when_words_match():
    import json
    from audit_core import parse_audit_response

    data = table()
    data["quality"] = "uncertain"
    quote, locator = render_table(data)
    result = parse_audit_response(
        json.dumps({"claims": [{"claim_id": "presence", "source_ids": ["s1"]}]}),
        {1: quote},
        False,
        expected_work="Гравийный грунт",
        quote_sources={"s1": {"page": 1, "quote": quote, "locator": locator}},
    )
    presence = next(
        check for check in result["checks"] if check["claim_id"] == "presence"
    )
    assert presence["state"] == "review_required"
    assert result["work_found"] is not True


def test_bounded_table_keeps_header_and_inherited_rowspan_source():
    from lxml import html
    from table_context import select_fragment

    data = {
        "table_id": "t1",
        "page": 1,
        "row_count": 3,
        "col_count": 2,
        "header_rows": 1,
        "quality": "usable",
        "origin": "native",
        "cells": [
            {
                "cell_id": "h0",
                "row": 0,
                "column": 0,
                "text": "Object",
                "role": "header",
            },
            {
                "cell_id": "h1",
                "row": 0,
                "column": 1,
                "text": "Quantity",
                "role": "header",
            },
            {
                "cell_id": "name",
                "row": 1,
                "column": 0,
                "rowspan": 2,
                "text": "Bridge",
                "role": "cell",
                "header_ids": ["h0"],
                "bbox": [0, 10, 50, 30],
            },
            {
                "cell_id": "v1",
                "row": 1,
                "column": 1,
                "text": "10",
                "role": "cell",
                "header_ids": ["h1"],
            },
            {
                "cell_id": "v2",
                "row": 2,
                "column": 1,
                "text": "20",
                "role": "cell",
                "header_ids": ["h1"],
            },
        ],
    }
    budget = len(render_table(data, 2, 3)[0])
    text, locator = select_fragment(data, budget, lambda text: int("20" in text))
    parsed = html.fromstring(text)
    assert parsed.xpath("./thead/tr/th/text()") == ["Object", "Quantity"]
    assert parsed.xpath("./tbody/tr/td/text()") == ["Bridge", "20"]
    assert len(text) <= budget
    source = next(cell for cell in locator["members"] if cell["cell_id"] == "name")
    assert source["rowspan"] == 2
    assert source["bbox"] == [0, 10, 50, 30]
    assert resolve_fragment([data], text) == locator
    assert resolve_fragment([data], text.replace(">20<", ">90<")) is None
