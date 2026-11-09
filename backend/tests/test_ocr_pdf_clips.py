"""Direct PDF quadrant renders preserve local resolution and inverse geometry."""

from pathlib import Path
import io

from PIL import Image, ImageOps
import pymupdf
import pytest

import ocr_engine as ocr


def tsv(text="", conf=95, box=(0, 0, 10, 10)):
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    if not text:
        return header
    x0, y0, x1, y1 = box
    return (
        header + f"5\t1\t1\t1\t1\t1\t{x0}\t{y0}\t{x1 - x0}\t{y1 - y0}\t{conf}\t{text}\n"
    )


class RecordingPage:
    def __init__(self, page):
        self.page = page
        self.renders = []

    def __getattr__(self, key):
        return getattr(self.page, key)

    def get_pixmap(self, **kwargs):
        pix = self.page.get_pixmap(**kwargs)
        self.renders.append(
            dict(**kwargs, size=(pix.width, pix.height), origin=(pix.x, pix.y))
        )
        return pix


def fake_engine(monkeypatch, angle=0):
    calls = []

    def recognize(png, language, psm, timeout):
        count = len(calls)
        calls.append(dict(psm=psm, timeout=timeout))
        if count < 4:
            # Force all words below gate, and choose a deterministic orientation.
            return tsv(
                "orientationchoice" if ocr.ORIENTATIONS[count] == angle else "x", 10
            )
        image = Image.open(io.BytesIO(png)).convert("L")
        box = ImageOps.invert(image).point([0] * 129 + [255] * 127).getbbox()
        return tsv("marker", 95, box) if box else tsv()

    monkeypatch.setattr(ocr.shutil, "which", lambda binary: "test-tesseract")
    monkeypatch.setattr(ocr, "run_tsv", recognize)
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "test")
    return calls


@pytest.mark.parametrize("page_rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("ocr_rotation", [0, 90, 180, 270])
def test_clip_words_map_back_through_both_rotations_and_nonzero_cropbox(
    monkeypatch, page_rotation, ocr_rotation
):
    calls = fake_engine(monkeypatch, ocr_rotation)
    with pymupdf.open() as doc:
        page = doc.new_page(width=190, height=140)
        page.set_cropbox(pymupdf.Rect(10, 20, 180, 130))
        marker = pymupdf.Rect(27, 23, 39, 33)
        page.draw_rect(marker, color=None, fill=(0, 0, 0))
        page.set_rotation(page_rotation)
        recording = RecordingPage(page)
        output = ocr.recognize_page(recording, timeout=7)
        assert page.rotation == page_rotation
    assert output["quality"]["rotation"] == ocr_rotation
    assert len(calls) == 8 and all(c["timeout"] == 7 for c in calls)
    assert [c["psm"] for c in calls] == [11] * 4 + [6] * 4
    assert len(recording.renders) == 5
    assert "clip" not in recording.renders[0]
    assert all("clip" in r for r in recording.renders[1:])
    words = [w for w in output["words"] if w["text"] == "marker"]
    assert len(words) == 1
    assert words[0]["bbox"] == pytest.approx(
        [marker.x0, marker.y0, marker.x1, marker.y1], abs=0.4
    )
    assert words[0]["conf"] == 95 and words[0]["kind"] == "label"
    assert not {"numeric_verified", "unit_verified"} & words[0].keys()


def test_large_cad_quadrants_spend_their_own_pixel_budget(monkeypatch):
    calls = fake_engine(monkeypatch)
    with pymupdf.open() as doc:
        page = RecordingPage(doc.new_page(width=2979.24, height=841.92))
        output = ocr.recognize_page(page)
    assert output["dpi_overview"] == 98
    assert output["dpi_crop"] > output["dpi_overview"]
    assert len(calls) == 8 and len(page.renders) == 5
    for render in page.renders:
        width, height = render["size"]
        assert max(width, height) <= 4096 and width * height <= 12000000
    assert all(r["dpi"] > 170 for r in page.renders[1:])
    assert all(r["clip"].width < 1800 for r in page.renders[1:])


@pytest.mark.parametrize("limit", [5, 6, 8])
def test_clip_rendering_does_not_increase_pass_or_timeout_budget(monkeypatch, limit):
    calls = fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "MAX_TSV_PASSES", limit)
    with pymupdf.open() as doc:
        page = RecordingPage(doc.new_page(width=120, height=80))
        output = ocr.recognize_page(page)
    assert len(calls) == limit == output["passes_used"]
    assert len(page.renders) == 1 + limit - 4
    assert all(c["timeout"] == ocr.PASS_TIMEOUT_S == 15 for c in calls)


def test_fractional_clip_origin_does_not_exceed_render_caps():
    clip = pymupdf.Rect(13.21, 19.71, 1013.21, 219.71)
    naive = ocr.bounded_render_dpi(
        clip.width, clip.height, 72, max_pixels=300000, max_side=1000
    )
    dpi = ocr._bounded_clip_dpi(clip, 72, max_pixels=300000, max_side=1000)
    with pymupdf.open() as doc:
        page = doc.new_page(width=1300, height=300)
        oversized = page.get_pixmap(clip=clip, dpi=naive, alpha=False)
        pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    assert oversized.width == 1001
    assert max(pix.width, pix.height) <= 1000 and pix.width * pix.height <= 300000
    # The origin contributes an extra pixel beyond ceil(width*dpi/72).
    assert dpi == 71 < naive == 72


def test_clip_render_failures_are_bounded_and_do_not_change_quality_gates(monkeypatch):
    calls = fake_engine(monkeypatch)
    with pymupdf.open() as doc:
        page = RecordingPage(doc.new_page(width=120, height=80))
        original = page.get_pixmap

        def fail_clip(**kwargs):
            if "clip" in kwargs:
                raise RuntimeError("render failure")
            return original(**kwargs)

        monkeypatch.setattr(page, "get_pixmap", fail_clip)
        output = ocr.recognize_page(page)
    assert len(calls) == 4 and output["passes_used"] == 8
    assert output["words"] == [] and output["quality"]["state"] != "usable"
    assert len([r for r in output["quality"]["reasons"] if "render failure" in r]) == 4
    assert ocr.LABEL_MIN_CONF == 60 and ocr.NUMERIC_MIN_CONF == 80


def test_actual_pd74_render_recovers_resolution_at_the_pavilion_legend(monkeypatch):
    pdf = (
        Path(__file__).resolve().parents[3]
        / "fact-pipeline-inputs/blobs/e4/81/e48108c661864b6e086f4c8c265a7ac0edd398cfc22cf0f6155c986751154c9f"
    )
    if not pdf.exists():
        pytest.skip("Optional immutable primary project PDF")
    fake_engine(monkeypatch)
    with pymupdf.open(pdf) as doc:
        recording = RecordingPage(doc[73])
        output = ocr.recognize_page(recording)
    legend = pymupdf.Rect(1615, 791, 1850, 816)
    containing = [r for r in recording.renders[1:] if r["clip"].contains(legend)]
    assert len(containing) == 1
    assert output["dpi_overview"] == 98 and containing[0]["dpi"] > 170
    assert output["passes_used"] == 8
