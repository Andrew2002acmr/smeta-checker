from copy import deepcopy

import pytest

from pdf_saved_evidence import saved_numeric_value, header_geometry
from pdf_cell_ocr import NUMERIC_RECOVERY_VERSION
from pdf_table_verification import VERSION


def numeric():
    word = dict(text="123,45", confidence=0.997, bbox=[5, 5, 40, 15])
    reads = [dict(height=h, words=[dict(text="123,45", conf=82)]) for h in (40, 96)]
    return dict(
        text="123,45",
        bbox=[0, 0, 50, 20],
        words=[word],
        numeric_recovery=dict(
            version=NUMERIC_RECOVERY_VERSION,
            bbox=[0, 0, 50, 20],
            image_sha256="a" * 64,
            prior_text="123,45",
            prior_words=[deepcopy(word)],
            rapid_words=[dict(text="123,45", confidence=0.996)],
            variants=reads,
            prior_variants=[],
        ),
    )


def test_saved_complete_reads_corroborate_a_number_without_expected_vor_input():
    cell = numeric()
    before = deepcopy(cell)
    assert saved_numeric_value(cell) == "123.45"
    assert cell == before


@pytest.mark.parametrize(
    "mutation",
    [
        "bbox",
        "one_read",
        "one_size",
        "rapid",
        "prior",
        "conflict",
        "tokens",
        "image",
        "location",
    ],
)
def test_saved_numeric_consensus_fails_closed(mutation):
    cell = numeric()
    p = cell["numeric_recovery"]
    if mutation == "bbox":
        p["bbox"] = [0, 0, 51, 20]
    if mutation == "one_read":
        p["variants"].pop()
    if mutation == "one_size":
        p["variants"][1]["height"] = 40
    if mutation == "rapid":
        p["rapid_words"][0]["confidence"] = 0.98
    if mutation == "prior":
        p["prior_text"] = "123,46"
    if mutation == "conflict":
        p["variants"].append(dict(height=48, words=[dict(text="123,46", conf=80)]))
    if mutation == "tokens":
        p["variants"][1]["words"] = [
            dict(text="123,", conf=95),
            dict(text="45", conf=95),
        ]
    if mutation == "image":
        p["image_sha256"] = "unknown"
    if mutation == "location":
        cell["words"][0]["bbox"] = [90, 5, 130, 15]
    assert saved_numeric_value(cell) is None


def geometry():
    raw = dict(text="(0,24м)", confidence=0.99)
    return dict(
        cell_id="h",
        column=2,
        row=1,
        colspan=1,
        bbox=[0, 0, 50, 30],
        text="Основание ЩПС (0,24м)",
        words=[raw],
        header_refinement=dict(
            version=VERSION,
            header_id="h",
            column=2,
            row=1,
            colspan=1,
            bbox=[0, 0, 50, 30],
            variants=[
                dict(scale=s, words=[dict(text="(0,24m)", conf=86)]) for s in (1, 1.35)
            ],
        ),
    )


def test_header_depth_is_typed_and_tied_to_the_same_physical_header():
    h = geometry()
    fact = header_geometry(h)[0]
    assert (
        fact["parameter"] == "thickness"
        and fact["value"] == "0.24"
        and fact["unit"] == "м"
    )
    assert fact["quote"] in h["text"]
    h["header_refinement"]["bbox"] = [0, 0, 60, 30]
    assert header_geometry(h) == []


@pytest.mark.parametrize(
    "mutation", ["rapid", "tesseract", "conflict", "scale", "raw_text"]
)
def test_header_geometry_rejects_uncorroborated_digits(mutation):
    h = geometry()
    p = h["header_refinement"]
    if mutation == "rapid":
        h["words"][0]["confidence"] = 0.97
    if mutation == "tesseract":
        p["variants"][0]["words"][0]["conf"] = 79
    if mutation == "conflict":
        p["variants"][0]["words"][0]["text"] = "(0,25m)"
    if mutation == "scale":
        p["variants"][1]["scale"] = 1
    if mutation == "raw_text":
        h["text"] = "Основание ЩПС (0,25м)"
    assert header_geometry(h) == []
