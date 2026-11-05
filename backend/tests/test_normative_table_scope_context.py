from copy import deepcopy
from typing import Any

import pytest

from analysis_replay import digest
from normative_agent import cell_source_id, model_source_aliases
from normative_context_packing import pack_sources
from normative_table_headers import (
    SCOPE_VERSION,
    _literal_cell,
    table_scope_context,
    validated_table_scope_context,
)


def scope_fixture():
    header = dict(
        cell_id="table:h",
        row=0,
        column=1,
        colspan=1,
        text="Объем, м³",
        bbox=[1, 0, 2, 1],
    )
    labels = [
        dict(
            cell_id=f"table:r{row}",
            row=row,
            column=0,
            colspan=1,
            text=text,
            bbox=[0, row, 1, row + 1],
        )
        for row, text in enumerate(
            ["Съезд №1", "ул. Пролетарская", "Съезд №2"], start=1
        )
    ]
    cell = dict(
        cell_id="table:v",
        row=4,
        column=1,
        text="188,74",
        bbox=[1, 4, 2, 5],
        header_ids=["table:h"],
        numeric_verified=True,
        origin="ocr",
        quality="usable",
    )
    table: dict[str, Any] = dict(
        table_id="table", cells=[header, *labels, cell], quality="uncertain"
    )
    table["visual_scope_transcription"] = dict(
        version=SCOPE_VERSION,
        table_sha256=digest(table),
        version_id="pdf",
        page=90,
        total_cell_ids=["table:v"],
        body_first_row=1,
        total_row=4,
        row_labels=[_literal_cell(c) for c in labels],
        caption="Ведомость работ по устройству дорожной одежды",
        document_sha256="a" * 64,
        extraction_sha256="e" * 64,
        render_sha256="b" * 64,
        review_sha256=["c" * 64, "d" * 64],
        document_identity=dict(original_sha256="a" * 64, extraction_sha256="e" * 64),
    )
    sid = cell_source_id("pdf", 90, "table", "table:v")
    context = table_scope_context(table, cell, sid, "pdf", 90)
    assert context
    source = dict(
        id=sid,
        kind="project",
        version_id="pdf",
        page=90,
        origin="ocr",
        table_scope_context=context,
    )
    return table, cell, source


def test_literal_context_preserves_rows_and_caption_without_inferring_scope():
    table, cell, source = scope_fixture()
    context = validated_table_scope_context(source)
    assert context and "exits" not in context and "verified" not in context
    assert context["caption"] == "Ведомость работ по устройству дорожной одежды"
    assert [c["text"] for c in context["row_labels"]] == [
        "Съезд №1",
        "ул. Пролетарская",
        "Съезд №2",
    ]
    assert table["quality"] == "uncertain" and cell["quality"] == "usable"
    context["row_labels"].clear()
    assert validated_table_scope_context(source)
    assert table_scope_context(table, cell, source["id"], "other-pdf", 90) is None
    assert table_scope_context(table, cell, source["id"], "pdf", 91) is None
    assert (
        table_scope_context(table, table["cells"][1], source["id"], "pdf", 90) is None
    )


def test_packing_preserves_literal_scope_projection_and_aliases_its_exact_source():
    _, _, source = scope_fixture()
    source["text"] = "Объем, м³\nВсего:\n188,74"
    packed = pack_sources({}, [source], preferred=[source["id"]], limit=10000)
    assert packed["sources"][0]["table_scope_context"] == source["table_scope_context"]
    sent, aliases = model_source_aliases(packed, [source])
    assert (
        sent["sources"][0]["table_scope_context"]["source_binding"]["source_id"] == "s0"
    )
    assert aliases["s0"] == source["id"]


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(id="another-source"),
        lambda s: s.update(kind="normative"),
        lambda s: s.update(version_id="another-pdf"),
        lambda s: s.update(page=89),
        lambda s: s.update(origin="native"),
        lambda s: s["table_scope_context"]["row_labels"].pop(1),
        lambda s: s["table_scope_context"]["row_labels"][0].update(text="Улица"),
        lambda s: s["table_scope_context"]["header_cells"][0].update(
            text="Площадь, м²"
        ),
        lambda s: s["table_scope_context"]["source_binding"].update(column=2),
        lambda s: s["table_scope_context"]["source_binding"].update(
            header_ids=["another-header"]
        ),
        lambda s: s["table_scope_context"]["provenance"].update(quality="uncertain"),
        lambda s: s["table_scope_context"]["provenance"].update(
            review_sha256=["c" * 64, "c" * 64]
        ),
        lambda s: s["table_scope_context"]["provenance"].update(document_sha256="bad"),
        lambda s: s["table_scope_context"]["total_cell"].update(bbox=[1, 4, 1, 5]),
    ],
)
def test_reject_projection_binding_damage(change):
    _, _, source = scope_fixture()
    change(source)
    assert validated_table_scope_context(source) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t["cells"][1].update(text="Другая улица"),
        lambda t: t["cells"][0].update(text="Другой материал"),
        lambda t: t["cells"][-1].update(text="999"),
        lambda t: t["cells"][-1].update(bbox=[1, 4, 3, 5]),
        lambda t: t["visual_scope_transcription"]["row_labels"][0].update(
            text="Другая улица"
        ),
        lambda t: t["visual_scope_transcription"]["document_identity"].update(
            original_sha256="f" * 64
        ),
    ],
)
def test_prepared_projection_rechecks_actual_table_and_cell_geometry(change):
    table, cell, source = scope_fixture()
    table = deepcopy(table)
    change(table)
    assert table_scope_context(table, cell, source["id"], "pdf", 90) is None
