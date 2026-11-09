"""Small raster symbols on otherwise digital pages are evidence, not logos by default.

Detection uses page geometry only, never VOR values. OCR readings remain
uncertain: agreement is navigation evidence, not a normative interpretation.
Unprocessed crops are retained so a failed budget cannot establish absence.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time

VERSION = "inline-raster-v1"
MAX_REGIONS = 32
PAGE_SECONDS = 20


def excerpt_origin(index, page, offset, length):
    """An unrelated raster insertion does not invalidate all native paragraphs.

    Unknown/unlocatable insertion provenance remains uncertain. This only
    scopes uncertainty; it neither recognizes a symbol nor certifies a number.
    """
    quality = index.quality.get(page - 1) or {}
    base = 'ocr_unverified' if quality.get('origin') == 'ocr' else 'native'
    if base != 'native' or not quality.get('inline_raster', {}).get('detected'):
        return base
    regions = [r for r in (index.locators[page - 1] if index.locators is not None else [])
        if r.get('role') == 'inline_image']
    if not regions:
        return 'ocr_inline_unreviewed'
    text = index.texts[page - 1]
    for region in regions:
        if region.get('state') == 'visually_transcribed':
            continue
        context = str(region.get('context') or '').strip()
        if not context:
            return 'ocr_inline_unreviewed'
        pattern = r'\s+'.join(re.escape(word) for word in context.split())
        matches = list(re.finditer(pattern, text))
        if not matches or any(m.start() < offset + length and m.end() > offset for m in matches):
            return 'ocr_inline_unreviewed'
    return base


def native_fragments(index, page, offset, length):
    """Read contiguous native spans outside unresolved insertion lines.

    The original wide excerpt remains uncertain. These smaller literal source
    spans carry their own addresses; no text is filled into a raster gap.
    """
    quality = index.quality.get(page - 1) or {}
    if quality.get('origin') != 'native' or not quality.get('inline_raster', {}).get('detected'):
        return []
    regions = [r for r in (index.locators[page - 1] if index.locators is not None else [])
        if r.get('role') == 'inline_image' and r.get('state') != 'visually_transcribed']
    if not regions:
        return []
    text = index.texts[page - 1]
    end = min(len(text), offset + length)
    blocked = []
    for region in regions:
        context = str(region.get('context') or '').strip()
        if not context:
            return []
        matches = list(re.finditer(r'\s+'.join(re.escape(w) for w in context.split()), text))
        if not matches:
            return []
        for match in matches:
            start = text.rfind('\n', 0, match.start()) + 1
            stop = text.find('\n', match.end())
            stop = len(text) if stop < 0 else stop + 1
            if start < end and stop > offset:
                blocked.append((max(offset, start), min(end, stop)))
    cursor = offset
    spans = []
    for start, stop in sorted(blocked):
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, stop)
    if cursor < end:
        spans.append((cursor, end))
    return [dict(offset=start, text=text[start:stop]) for start, stop in spans
        if len(text[start:stop].strip()) >= 80
        and excerpt_origin(index, page, start, stop - start) == 'native'][:4]


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
        covered = any((box & pymupdf.Rect(word[:4])).get_area() >= box.get_area() * .5
            for word in words)
        if covered:
            continue
        neighbours = [word for word in words
            if abs((word[1] + word[3] - box.y0 - box.y1) / 2) <= max(box.height, word[3] - word[1])
            and min(abs(word[0] - box.x1), abs(box.x0 - word[2])) <= 300]
        if not neighbours:
            continue
        result.append(dict(bbox=[box.x0, box.y0, box.x1, box.y1], image_number=info["number"],
            pixel_width=info["width"], pixel_height=info["height"],
            context=" ".join(str(word[4]) for word in sorted(neighbours, key=lambda w: w[0]))))
    return result


def _read_image(image, language):
    from PIL import Image, ImageOps
    from ocr_engine import _to_png, parse_tsv, run_tsv

    variants = []
    for height in (72, 108):
        scaled = image.resize((max(1, round(image.width * height / image.height)), height), Image.Resampling.LANCZOS)
        scaled = ImageOps.expand(scaled, border=12, fill="white")
        words = parse_tsv(run_tsv(_to_png(scaled), language=language, psm=7, timeout=3))
        variants.append(dict(height=height, words=words,
            text=" ".join(word["text"] for word in words)))
    return variants


def prepare_inline(page, pdf_sha256, *, language="rus+eng", recognize=True, reader=None):
    import pymupdf
    from PIL import Image

    deadline = time.monotonic() + PAGE_SECONDS
    regions = image_regions(page)
    if not regions:
        return []
    images = {block['number']: block for block in page.get_text('dict')['blocks'] if block['type'] == 1}
    readings = {}
    for index, region in enumerate(regions):
        region.update(version=VERSION, page=page.number + 1, pdf_sha256=pdf_sha256,
            state="unresolved", quality="uncertain", origin="ocr_inline_unreviewed", text="",
            variants=[], reason="not_recognized")
        # Identity includes occurrence geometry. Reused images keep distinct
        # physical addresses even when their pixel content is identical.
        identity = [VERSION, pdf_sha256, page.number + 1, region["bbox"], region["image_number"]]
        region["source_id"] = "inline:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        try:
            embedded = images.get(region['image_number'])
            if embedded and embedded.get('image'):
                raw = embedded['image']
            else:
                box = pymupdf.Rect(region["bbox"]) * page.rotation_matrix
                raw = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=box, alpha=False).tobytes('png')
            region["image_sha256"] = hashlib.sha256(raw).hexdigest()
            if not recognize or index >= MAX_REGIONS or time.monotonic() >= deadline:
                region["reason"] = "recognition_disabled" if not recognize else "page_budget_exhausted"
                continue
            key = region["image_sha256"]
            if key not in readings:
                image = Image.open(io.BytesIO(raw)).convert('RGB')
                readings[key] = (reader or _read_image)(image, language)
            region["variants"] = readings[key]
            texts = [v.get("text", "").strip() for v in region["variants"]]
            if len(texts) == 2 and texts[0] and texts[0] == texts[1]:
                region.update(text=texts[0], state="candidate", reason="two_scale_ocr_agreement_requires_review")
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
        + ('визуальная транскрипция' if r['state'] == 'visually_transcribed' else 'OCR требует сверки')
        + f"; bbox={r['bbox']}] "
        + (r["text"] or "[не распознано]") + "\nКонтекст строки: " + r["context"]
        for r in regions)


def coverage(regions):
    return dict(version=VERSION, detected=len(regions),
        candidates=sum(r["state"] == "candidate" for r in regions),
        visually_transcribed=sum(r['state'] == 'visually_transcribed' for r in regions),
        unresolved=sum(r["state"] == "unresolved" for r in regions),
        visual_review_required=any(r['state'] != 'visually_transcribed' for r in regions), absence_proven=False)


def apply_transcriptions(regions, transcriptions):
    """Explicit visual readings are bound to original bytes AND each occurrence.

    Never propagate a label by a similar number, image name or OCR result.
    Even byte-identical images in different sentences require explicit entries.
    """
    from copy import deepcopy
    result = deepcopy(regions)
    lookup = {r['source_id']: r for r in result}
    seen = set()
    for reading in transcriptions:
        sid = reading['source_id']
        region = lookup.get(sid)
        if sid in seen or region is None:
            raise ValueError('Unknown or duplicate inline transcription')
        seen.add(sid)
        for key in ('pdf_sha256', 'page', 'bbox', 'image_sha256'):
            if not region.get(key) or reading.get(key) != region[key]:
                raise ValueError('Inline transcription source identity changed: ' + key)
        if not str(reading.get('text', '')).strip() or not reading.get('reviewer') or not reading.get('method'):
            raise ValueError('Explicit text, reviewer and method required')
        region.update(text=reading['text'], state='visually_transcribed',
            reason='explicit_visual_transcription', transcription=deepcopy(reading))
    return result
