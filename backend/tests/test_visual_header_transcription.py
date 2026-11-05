"""Explicit independent readings retain physical provenance beside old OCR."""

from copy import deepcopy

import pytest

from normative_table_headers import apply, save
from pdf_saved_evidence import header_geometry
from pdf_table_verification import VISUAL_VERSION, verified_header_text
from test_normative_table_headers import setup


def fixture():
    ctx, record, table, header, artifacts = setup()
    header["text"] = "Основание — ЩПС 0/31,5, м² (20см)"
    header["visual_transcription"] = dict(
        version=VISUAL_VERSION,
        **{
            k: deepcopy(header[k])
            for k in ("cell_id", "bbox", "row", "column", "colspan", "text")
        },
        original_text="",
        crop_image_sha256="original-crop",
        page=89,
        document_version_id="pdf-version",
        pdf_sha256="a" * 64,
        review_sha256="b" * 64,
        rendered_page_sha256="c" * 64,
        reviewer="independent visual reader",
        method="original PDF raster, no expected VOR quantities",
    )
    return ctx, record, table, header, artifacts


def test_verified_visual_header_supplies_only_its_literal_geometry_and_keeps_ocr():
    ctx, record, table, header, _ = fixture()
    before = deepcopy(table)
    save(
        ctx,
        record,
        "pdf-version",
        89,
        table,
        [header],
        provenance="independent_visual_header_transcription",
    )
    result = apply(ctx, record, "pdf-version", 89, [table], [])[0]["cells"][0]
    assert table == before and result["raw_recovered_text"] == ""
    assert verified_header_text(result) == header["text"]
    (fact,) = header_geometry(result)
    assert (fact["parameter"], fact["value"], fact["unit"], fact["quote"]) == (
        "thickness",
        "20",
        "см",
        "(20см)",
    )
    assert fact["version"] == VISUAL_VERSION


@pytest.mark.parametrize(
    "change",
    [
        "bbox",
        "cell_id",
        "text",
        "raw",
        "crop",
        "page",
        "pdf_version",
        "reviewer",
        "source_hash",
    ],
)
def test_visual_reading_cannot_be_relocated_or_silently_relabelled(change):
    ctx, record, table, header, _ = fixture()
    if change == "bbox":
        header["bbox"] = [0, 0, 11, 5]
    elif change == "cell_id":
        header["cell_id"] = "other"
    elif change == "text":
        header["text"] = "Основание 30см"
    elif change == "raw":
        header["raw_recovered_text"] = "invented"
    elif change == "crop":
        header["visual_transcription"]["crop_image_sha256"] = "another"
    elif change == "page":
        header["visual_transcription"]["page"] = 90
    elif change == "pdf_version":
        header["visual_transcription"]["document_version_id"] = "other"
    elif change == "reviewer":
        header["visual_transcription"]["reviewer"] = ""
    else:
        header["visual_transcription"]["pdf_sha256"] = "unknown"
    with pytest.raises(ValueError):
        save(
            ctx,
            record,
            "pdf-version",
            89,
            table,
            [header],
            provenance="independent_visual_header_transcription",
        )
