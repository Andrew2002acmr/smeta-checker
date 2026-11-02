"""A schedule quantity belongs to its physical work row, not its name's numbers."""

import json

import pymupdf
import pytest

from audit_core import PageTextIndex, _bind_quantity_fact, parse_audit_response
from audit_llm import _catalog_chunks
from pdf_tables import native_tables
from table_context import render_table, resolve_fragment


WORK = "Разборка трамвайного пути, рельсы РТ62, шпала железобетонная, эпюра 1680 шт./км"


def schedule():
    from pdf_tables import finish_table

    rows = [
        ["№ п/п", "Наименование работ", "Единица измерения", "Количество"],
        ["4", WORK, "м", "75"],
        [
            "5",
            "Разборка трамвайного пути, рельсы Р65, шпала железобетонная, эпюра 1440 шт./км",
            "м",
            "570,37",
        ],
        ["5.1", "Рельсы Р65 (вес 1 м равен 64,82 кг)", "м/т", "1140,74/73,94"],
    ]
    cells = [
        dict(
            row=r,
            column=c,
            rowspan=1,
            colspan=1,
            text=value,
            bbox=[c * 100, r * 30, (c + 1) * 100, (r + 1) * 30],
        )
        for r, row in enumerate(rows)
        for c, value in enumerate(row)
    ]
    return finish_table(
        cells,
        table_id="t1",
        page=27,
        bbox=[0, 0, 400, 120],
        origin="native",
        header_rows=1,
    )


@pytest.mark.parametrize(
    "expected,state", [("75", "confirmed"), ("74", "contradiction")]
)
def test_work_row_selects_neighbouring_quantity_without_using_expected_value(
    expected, state
):
    table = schedule()
    quote, locator = render_table(table)
    response = json.dumps(
        {
            "claims": [
                {"claim_id": c, "source_ids": ["s"]}
                for c in ("presence", "quantity", "unit")
            ]
        }
    )
    result = parse_audit_response(
        response,
        {27: quote},
        False,
        expected_work=WORK,
        expected_volume=expected,
        expected_unit="м",
        position_type="Работа",
        quote_sources={"s": {"page": 27, "quote": quote, "locator": locator}},
    )
    assert (
        next(c for c in result["checks"] if c["claim_id"] == "quantity")["state"]
        == state
    )
    assert result["found_volume"] == "75" and result["found_unit"] == "м"


def test_intrinsic_density_in_name_does_not_block_scalar_schedule_quantity():
    fact, state = _bind_quantity_fact([f"4 | {WORK} | м | 75"], WORK, "м")
    assert state == "bound" and fact.value == 75


def test_equal_names_with_conflicting_quantities_are_not_selected_by_expected_value():
    quote = f"<table><tr><td>{WORK}</td><td>м</td><td>75</td></tr><tr><td>{WORK}</td><td>м</td><td>76</td></tr></table>"
    fact, state = _bind_quantity_fact([quote], WORK, "м")
    assert fact is None and state == "ambiguous_measurements"


def test_structured_context_and_catalog_retain_headers_and_addressed_cells():
    table = schedule()
    index = PageTextIndex(["plain text that loses column boundaries"], tables=[[table]])
    restored = PageTextIndex.from_snapshot(index.to_snapshot())
    quote = restored.fragment(0, WORK, "", "", "75", 5000)
    chunks = list(_catalog_chunks(quote))
    assert all(chunk in quote for chunk in chunks)
    assert any('<td>75</td>' in chunk for chunk in chunks)
    locator = resolve_fragment(restored.tables[0], quote)
    value = next(c for c in locator["members"] if c["text"] == "75")
    assert value["column"] == 3 and value["row"] == 1 and value["header_ids"]


def test_old_grid_snapshots_keep_their_original_corpus_identity():
    legacy = PageTextIndex(["legacy text"], table_rows=[[schedule()]])
    snapshot = legacy.to_snapshot()
    assert "tables" not in snapshot
    restored = PageTextIndex.from_snapshot(snapshot)
    assert restored.tables[0][0]["col_count"] == 4
    assert restored.to_snapshot()["corpus_id"] == snapshot["corpus_id"]


def test_shared_page_snapshot_keeps_structured_tables(tmp_path):
    from audit_snapshot import load_snapshot, store_snapshot
    from storage_blobs import FileBlobStore

    store = FileBlobStore(tmp_path)
    index = PageTextIndex(["page"], tables=[[schedule()]])
    restored = load_snapshot(store, store_snapshot(store, index))
    assert restored.tables == index.tables
    assert restored.to_snapshot()["corpus_id"] == index.to_snapshot()["corpus_id"]


def test_long_table_keeps_complete_addressed_row_within_evidence_limit():
    from pdf_tables import finish_table

    grid = schedule()
    cells = [dict(c) for c in grid["cells"]]
    for row in range(4, 30):
        for column, text in enumerate(
            [str(row), "Соседняя другая работа " * 8, "м", "74"]
        ):
            cells.append(
                dict(
                    row=row,
                    column=column,
                    rowspan=1,
                    colspan=1,
                    text=text,
                    bbox=[column * 100, row * 30, (column + 1) * 100, (row + 1) * 30],
                )
            )
    grid = finish_table(
        cells,
        table_id="t1",
        page=1,
        bbox=[0, 0, 400, 900],
        origin="native",
        header_rows=1,
    )
    index = PageTextIndex(["page"], tables=[[grid]])
    quote = index.fragment(0, WORK, "", "", "74", 12000, prefer_volume=True)
    assert len(quote) <= 1200 and resolve_fragment(index.tables[0], quote)
    assert all(chunk in quote for chunk in _catalog_chunks(quote))
    fact, state = _bind_quantity_fact([quote], WORK, "м")
    assert state == "bound" and fact.value == 75


def test_drawing_frame_does_not_swallow_quantity_column():
    with pymupdf.open() as pdf:
        p = pdf.new_page(width=600, height=842)
        p.draw_rect((60, 20, 572, 780))
        for y in (100, 130, 180, 220, 260, 300, 340):
            p.draw_line((60, y), (570, y))
        for x in (90, 350, 420):
            p.draw_line((x, 100), (x, 340))
        for x, y, text in [
            (65, 120, "No"),
            (100, 120, "Work"),
            (360, 120, "Unit"),
            (440, 120, "Quantity"),
            (65, 155, "4"),
            (100, 155, "Tram dismantling RT62 sleepers"),
            (370, 155, "m"),
            (460, 155, "75"),
            (65, 200, "5"),
            (100, 200, "Tram dismantling R65 sleepers"),
            (370, 200, "m"),
            (460, 200, "570.37"),
        ]:
            p.insert_text((x, y), text, fontsize=10)
        tables = native_tables(p, lambda page, box: page.get_textbox(box))
        grid = next(t for t in tables if any(c["text"] == "75" for c in t["cells"]))
        assert grid["quality"] == "usable" and grid["col_count"] == 4
        quote, _ = render_table(grid)
        fact, state = _bind_quantity_fact(
            [quote], "Tram dismantling RT62 sleepers", "m"
        )
        assert state == "bound" and fact.value == 75
