"""Small raster symbols on otherwise digital pages are evidence, not logos by default.

Detection uses page geometry only, never VOR values. OCR readings remain
uncertain: agreement is navigation evidence, not a normative interpretation.
Unprocessed crops are retained so a failed budget cannot establish absence.
"""

from __future__ import annotations

import hashlib
import io
import json
import time

VERSION = "inline-raster-v1"
MAX_REGIONS = 32
PAGE_SECONDS = 20


def image_regions(page):
    """Find small image occurrences next to native words, including table labels.

    A word that already covers an image usually belongs to a scanned page's
    hidden text layer. Native sentence bboxes are deliberately not used: they
    can enclose a missing image in the middle of the sentence.
    """
    import pymupdf

    words = page.get_text("words")
    if not words:
        return []  # Whole-page scans belong to the existing page OCR route.
    result = []
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"])
        if box.is_empty or box.height > 48 or box.width > 240:
            continue
        covered = any(
            (box & pymupdf.Rect(word[:4])).get_area() >= box.get_area() * 0.5
            for word in words
        )
        if covered:
            continue
        neighbours = [
            word
            for word in words
            if abs((word[1] + word[3] - box.y0 - box.y1) / 2)
            <= max(box.height, word[3] - word[1])
            and min(abs(word[0] - box.x1), abs(box.x0 - word[2])) <= 300
        ]
        if not neighbours:
            continue
        result.append(
            dict(
                bbox=[box.x0, box.y0, box.x1, box.y1],
                image_number=info["number"],
                pixel_width=info["width"],
                pixel_height=info["height"],
                context=" ".join(
                    str(word[4]) for word in sorted(neighbours, key=lambda w: w[0])
                ),
            )
        )
    return result


def _read_image(image, language):
    from PIL import Image, ImageOps
    from ocr_engine import _to_png, parse_tsv, run_tsv

    variants = []
    for height in (72, 108):
        scaled = image.resize(
            (max(1, round(image.width * height / image.height)), height),
            Image.Resampling.LANCZOS,
        )
        scaled = ImageOps.expand(scaled, border=12, fill="white")
        words = parse_tsv(run_tsv(_to_png(scaled), language=language, psm=7, timeout=3))
        variants.append(
            dict(
                height=height,
                words=words,
                text=" ".join(word["text"] for word in words),
            )
        )
    return variants


def prepare_inline(
    page, pdf_sha256, *, language="rus+eng", recognize=True, reader=None
):
    import pymupdf
    from PIL import Image

    deadline = time.monotonic() + PAGE_SECONDS
    regions = image_regions(page)
    if not regions:
        return []
    images = {
        block["number"]: block
        for block in page.get_text("dict")["blocks"]
        if block["type"] == 1
    }
    readings = {}
    for index, region in enumerate(regions):
        region.update(
            version=VERSION,
            page=page.number + 1,
            pdf_sha256=pdf_sha256,
            state="unresolved",
            quality="uncertain",
            origin="ocr_inline_unreviewed",
            text="",
            variants=[],
            reason="not_recognized",
        )
        # Identity includes occurrence geometry. Reused images keep distinct
        # physical addresses even when their pixel content is identical.
        identity = [
            VERSION,
            pdf_sha256,
            page.number + 1,
            region["bbox"],
            region["image_number"],
        ]
        region["source_id"] = (
            "inline:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        )
        try:
            embedded = images.get(region["image_number"])
            if embedded and embedded.get("image"):
                raw = embedded["image"]
            else:
                box = pymupdf.Rect(region["bbox"]) * page.rotation_matrix
                raw = page.get_pixmap(
                    matrix=pymupdf.Matrix(4, 4), clip=box, alpha=False
                ).tobytes("png")
            region["image_sha256"] = hashlib.sha256(raw).hexdigest()
            if not recognize or index >= MAX_REGIONS or time.monotonic() >= deadline:
                region["reason"] = (
                    "recognition_disabled" if not recognize else "page_budget_exhausted"
                )
                continue
            key = region["image_sha256"]
            if key not in readings:
                image = Image.open(io.BytesIO(raw)).convert("RGB")
                readings[key] = (reader or _read_image)(image, language)
            region["variants"] = readings[key]
            texts = [v.get("text", "").strip() for v in region["variants"]]
            if len(texts) == 2 and texts[0] and texts[0] == texts[1]:
                region.update(
                    text=texts[0],
                    state="candidate",
                    reason="two_scale_ocr_agreement_requires_review",
                )
            else:
                region["reason"] = "empty_or_conflicting_readings"
        except Exception as exc:
            from billiard.exceptions import SoftTimeLimitExceeded

            if isinstance(exc, SoftTimeLimitExceeded):
                raise
            region["reason"] = "recognition_failed:" + type(exc).__name__
    return regions


def inline_text(regions):
    """Separate uncertain readings from literal native text and from each other."""
    return "\n".join(
        f"[Растровая вставка {r['source_id']}; "
        + (
            "визуальная транскрипция"
            if r["state"] == "visually_transcribed"
            else "OCR требует сверки"
        )
        + f"; bbox={r['bbox']}] "
        + (r["text"] or "[не распознано]")
        + "\nКонтекст строки: "
        + r["context"]
        for r in regions
    )


def coverage(regions):
    return dict(
        version=VERSION,
        detected=len(regions),
        candidates=sum(r["state"] == "candidate" for r in regions),
        visually_transcribed=sum(r["state"] == "visually_transcribed" for r in regions),
        unresolved=sum(r["state"] == "unresolved" for r in regions),
        visual_review_required=any(
            r["state"] != "visually_transcribed" for r in regions
        ),
        absence_proven=False,
    )


def apply_transcriptions(regions, transcriptions):
    """Explicit visual readings are bound to original bytes AND each occurrence.

    Never propagate a label by a similar number, image name or OCR result.
    Even byte-identical images in different sentences require explicit entries.
    """
    from copy import deepcopy

    result = deepcopy(regions)
    lookup = {r["source_id"]: r for r in result}
    seen = set()
    for reading in transcriptions:
        sid = reading["source_id"]
        region = lookup.get(sid)
        if sid in seen or region is None:
            raise ValueError("Unknown or duplicate inline transcription")
        seen.add(sid)
        for key in ("pdf_sha256", "page", "bbox", "image_sha256"):
            if not region.get(key) or reading.get(key) != region[key]:
                raise ValueError("Inline transcription source identity changed: " + key)
        if (
            not str(reading.get("text", "")).strip()
            or not reading.get("reviewer")
            or not reading.get("method")
        ):
            raise ValueError("Explicit text, reviewer and method required")
        region.update(
            text=reading["text"],
            state="visually_transcribed",
            reason="explicit_visual_transcription",
            transcription=deepcopy(reading),
        )
    return result
