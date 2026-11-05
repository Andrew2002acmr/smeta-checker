"""Synthetic image labels exercise the real geometry, not the expected VOR text."""

import hashlib
import io
from copy import deepcopy
from contextlib import closing
from unittest.mock import patch

import pymupdf
import pytest
from PIL import Image, ImageDraw

from pdf_inline_evidence import (
    apply_transcriptions,
    coverage,
    image_regions,
    prepare_inline,
)


def mixed_pdf():
    image = Image.new("RGB", (100, 60), "white")
    ImageDraw.Draw(image).text((15, 20), "Q1", fill="black")
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 220), "Grade")
    page.insert_image(pymupdf.Rect(95, 207, 120, 222), stream=raw.getvalue())
    page.insert_text((130, 220), "stone M800.")
    return doc


def reading(_image, _language):
    return [
        dict(height=height, text="Q1", words=[dict(text="Q1", conf=99)])
        for height in (72, 108)
    ]


def test_small_symbol_is_detected_below_old_five_percent_threshold():
    with mixed_pdf() as doc:
        page = doc[0]
        assert "Q1" not in page.get_text()
        assert (
            sum(pymupdf.Rect(i["bbox"]).get_area() for i in page.get_image_info())
            < 0.05 * page.rect.get_area()
        )
        regions = prepare_inline(page, "a" * 64, reader=reading)
        assert len(regions) == 1
        assert regions[0]["text"] == "Q1"
        assert regions[0]["quality"] == "uncertain"
        assert regions[0]["image_sha256"]
        assert "M800" in regions[0]["context"]
        assert coverage(regions)["visual_review_required"]


def test_empty_conflicting_and_exhausted_reads_remain_addressed():
    with mixed_pdf() as doc:

        def conflict(*_):
            return [dict(height=72, text="Q1"), dict(height=108, text="Q7")]

        result = prepare_inline(doc[0], "a" * 64, reader=conflict)
        assert result[0]["state"] == "unresolved" and not result[0]["text"]
        with patch("pdf_inline_evidence.MAX_REGIONS", 0):
            result = prepare_inline(
                doc[0], "a" * 64, reader=lambda *_: pytest.fail("over budget")
            )
        assert result[0]["reason"] == "page_budget_exhausted"
        assert result[0]["source_id"] and result[0]["image_sha256"]
        assert not coverage(result)["absence_proven"]


def test_occurrences_keep_identity_across_rotation_and_different_source_bytes():
    with mixed_pdf() as doc:
        first = prepare_inline(doc[0], "a" * 64, recognize=False)
        doc[0].set_rotation(90)
        rotated = prepare_inline(doc[0], "a" * 64, recognize=False)
        assert first[0]["source_id"] == rotated[0]["source_id"]
        assert first[0]["bbox"] == rotated[0]["bbox"]
        assert first[0]["image_sha256"] == rotated[0]["image_sha256"]
        assert (
            first[0]["source_id"]
            != prepare_inline(doc[0], "b" * 64, recognize=False)[0]["source_id"]
        )


def test_visual_transcription_requires_exact_pdf_occurrence_and_pixels():
    with mixed_pdf() as doc:
        original = prepare_inline(doc[0], "a" * 64, recognize=False)
    region = original[0]
    proof = {
        key: region[key]
        for key in ("source_id", "pdf_sha256", "page", "bbox", "image_sha256")
    }
    proof.update(text="Q1", reviewer="test reviewer", method="visual")
    corrected = apply_transcriptions(original, [proof])
    assert corrected[0]["state"] == "visually_transcribed"
    assert original[0]["state"] == "unresolved"
    for field in ("pdf_sha256", "page", "bbox", "image_sha256"):
        wrong = deepcopy(proof)
        wrong[field] = "changed"
        with pytest.raises(ValueError, match="identity changed"):
            apply_transcriptions(original, [wrong])
    with pytest.raises(ValueError, match="duplicate"):
        apply_transcriptions(original, [proof, proof])


def test_full_document_preparation_retains_crop_quality_and_snapshot_locator(tmp_path):
    from audit_core import PageTextIndex
    from pdf_text import PDFTextSource

    with mixed_pdf() as doc, patch("pdf_inline_evidence._read_image", reading):
        with closing(PDFTextSource(doc, cache_dir=tmp_path / "cache")) as source:
            prepared = source.prepare_full()
    restored = PageTextIndex.from_snapshot(prepared.to_snapshot())
    assert "Q1" in restored.texts[0]
    assert restored.metadata["preparation_version"] == 2
    assert restored.quality[0]["inline_raster"]["detected"] == 1
    assert restored.quality[0]["state"] in {"uncertain", "failed"}
    locator = next(v for v in restored.locators[0] if v["role"] == "inline_image")
    assert len(locator["bbox"]) == 4 and locator["origin"] == "ocr_inline_unreviewed"
    assert len(locator["image_sha256"]) == len(hashlib.sha256(b"").hexdigest())


def test_scan_with_no_native_words_is_left_to_whole_page_ocr():
    with mixed_pdf() as doc:
        page = doc.new_page(width=600, height=800)
        pix = doc[0].get_pixmap()
        page.insert_image(page.rect, pixmap=pix)
        assert image_regions(page) == []
