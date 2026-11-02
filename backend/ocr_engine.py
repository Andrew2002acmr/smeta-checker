"""Bounded Tesseract TSV OCR: orientation choice, quadrant crops, geometry.

Production OCR path for :class:`pdf_text.PDFTextSource`. A scan page is
rendered with PyMuPDF, the system ``tesseract`` binary runs directly on PNG
bytes (TSV output), and every kept word is mapped back to unrotated PDF
points with an inverse rotation/crop transform.

Budgets: at most 8 TSV passes per new page (4 overview orientations plus
4 quadrant crops), 15 s per pass, overview PSM 11, crop PSM 6, quadrant
render up to 300 dpi inside the same 12 MP / 4096 px limits as the base
render. Orientation scoring uses only confidence-weighted token mass and
never the expected work name, volume, or any other position content.

Readability gates (not truth probabilities): a text label word needs
confidence >= 60, a numeric/unit token needs >= 80. Words below their gate
are dropped from the merged text and counted explicitly.
"""

from __future__ import annotations

from billiard.exceptions import SoftTimeLimitExceeded

import csv
import io
import math
import re
import shutil
import subprocess
from functools import lru_cache

from PIL import Image

ALGO_VERSION = "ocr-tsv-v4"
CACHE_FORMAT = "ocr-tsv-v1"
CACHE_SCHEMA = 2
ORIENTATIONS = (0, 90, 180, 270)
OVERVIEW_PSM = 11
CROP_PSM = 6
PASS_TIMEOUT_S = 15
MAX_TSV_PASSES = 8
# Shared band between adjacent quadrants, as a fraction of the side.
QUADRANT_OVERLAP = 0.10
QUADRANT_DPI = 300
LABEL_MIN_CONF = 60
NUMERIC_MIN_CONF = 80

_ALNUM_RE = re.compile(r"[0-9A-Za-z\u0400-\u04FF]")
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


def _normalized_unit(text):
    return text.strip().lower().replace("²", "2").replace("³", "3").rstrip(".")


_UNIT_SET = frozenset({
    "м", "мм", "см", "дм", "км", "м2", "см2", "мм2", "м3", "см3", "мм3",
    "г", "кг", "т", "шт", "pcs", "%", "н", "кн",
    "г/м2", "кг/м2", "м3/м2", "кн/м",
})


def measurement_kind(text):
    """Classify a TSV token: ``value`` (number/unit, gate 80) or ``label`` (gate 60).

    Compound tokens that start with a number (``500г/м2``, ``45м``) or end
    with a known unit carry a numeric claim, so they gate at 80. Pure
    labels gate at 60.
    """
    cleaned = text.strip().rstrip(".,;:")
    if not cleaned:
        return "label"
    if _NUMBER_RE.match(cleaned):
        return "value"
    if _normalized_unit(cleaned) in _UNIT_SET:
        return "value"
    lowered = _normalized_unit(cleaned)
    if _NUMBER_RE.match(lowered.split()[0] if lowered.split() else ""):
        return "value"
    for unit in _UNIT_SET:
        if lowered == unit or lowered.endswith(unit):
            stem = lowered[:len(lowered) - len(unit)].strip()
            if stem and any(char.isdigit() for char in stem):
                return "value"
    return "label"


def gate_for(kind):
    return NUMERIC_MIN_CONF if kind == "value" else LABEL_MIN_CONF


@lru_cache(maxsize=1)
def tesseract_version():
    """System Tesseract version string; ``unknown`` when the binary is missing."""
    try:
        proc = subprocess.run(
            ["tesseract", "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10)
        first = proc.stdout.decode("utf-8", "replace").splitlines()
        if proc.returncode == 0 and first and first[0].strip():
            return first[0].strip()[:60]
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        pass
    return "unknown"


def bounded_render_dpi(width_pt, height_pt, dpi=200, max_pixels=12000000,
                       max_side=4096):
    """Bound a page render before allocating a pixmap (same rule as before)."""
    if min(float(width_pt), float(height_pt), dpi, max_pixels, max_side) <= 0:
        raise ValueError("Некорректные размеры страницы или лимиты изображения")
    scale = min(dpi / 72.0, max_side / max(width_pt, height_pt),
                math.sqrt(max_pixels / (width_pt * height_pt)))
    dpi = min(dpi, int(scale * 72))
    while dpi > 0:
        width = math.ceil(width_pt * dpi / 72)
        height = math.ceil(height_pt * dpi / 72)
        if width * height <= max_pixels and max(width, height) <= max_side:
            return dpi
        dpi -= 1
    raise ValueError("Размер страницы не позволяет рендер в пределах бюджета изображения")


def run_tsv(png_bytes, language="rus+eng", psm=OVERVIEW_PSM,
            timeout=PASS_TIMEOUT_S):
    """Run one Tesseract TSV pass; timeouts and nonzero exits raise."""
    if shutil.which("tesseract") is None:
        raise RuntimeError("Tesseract недоступен: бинарь tesseract не найден в PATH")
    cmd = ["tesseract", "stdin", "stdout", "--psm", str(psm),
           "-l", language, "tsv"]
    try:
        proc = subprocess.run(
            cmd, input=png_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("TSV pass timeout (%d с, PSM %d)" % (timeout, psm))
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError("Tesseract TSV (PSM %d): %s" % (psm, detail or "exit %d" % proc.returncode))
    return proc.stdout.decode("utf-8", "replace")


def parse_tsv(tsv_text):
    """Parse TSV words (level 5); skips empty texts and unparseable rows."""
    words = []
    # Tesseract emits literal text, not CSV-escaped quoted fields. A leading
    # quotation mark must never consume subsequent words/rows as one field.
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        try:
            if (row.get("level") or "").strip() != "5":
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            words.append({
                "text": text,
                "conf": int(float(row.get("conf", "-1"))),
                "left": int(row.get("left", 0)),
                "top": int(row.get("top", 0)),
                "width": int(row.get("width", 0)),
                "height": int(row.get("height", 0)),
            })
        except (ValueError, TypeError):
            continue
    return words


def score_orientation(words):
    """Confidence-weighted token mass; the only orientation signal."""
    total = 0.0
    for word in words:
        conf = word.get("conf", -1)
        if conf < 0:
            continue
        mass = min(len(_ALNUM_RE.findall(word.get("text", ""))), 20)
        total += max(conf, 0) * mass
    return total


def _pil_from_pixmap(pix):
    width, height, components = int(pix.width), int(pix.height), int(pix.n)
    data = bytes(pix.samples)
    if components >= 4:
        return Image.frombytes("RGBA", (width, height), data).convert("RGB")
    if components == 3:
        return Image.frombytes("RGB", (width, height), data)
    return Image.frombytes("L", (width, height), data).convert("RGB")


def _rotate(image, angle):
    angle = angle % 360
    if angle == 0:
        return image
    if angle == 90:
        return image.transpose(Image.Transpose.ROTATE_90)
    if angle == 180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if angle == 270:
        return image.transpose(Image.Transpose.ROTATE_270)
    raise ValueError("Ориентация OCR поддерживает только 0/90/180/270")


def _to_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _enhance_crop(crop):
    """Upscale + contrast for small/low-contrast crops.

    Small colored labels (blue '+81,28' on a line) vanish at native
    resolution: 2x LANCZOS + autocontrast lifts them into the readable
    range. Only crops smaller than 1200px on the long side pay for it.
    """
    from PIL import ImageOps
    if max(crop.size) >= 1200:
        return crop
    big = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
    gray = big.convert("L")
    return ImageOps.autocontrast(gray, cutoff=2).convert("RGB")


def quadrant_boxes(width, height, overlap=QUADRANT_OVERLAP):
    """Four overlapping quadrants; the shared band is ~``overlap`` of the side."""
    half_x, half_y = width // 2, height // 2
    margin_x = int(round(width * overlap / 2))
    margin_y = int(round(height * overlap / 2))
    return [
        ("q1", (0, 0, half_x + margin_x, half_y + margin_y)),
        ("q2", (half_x - margin_x, 0, width, half_y + margin_y)),
        ("q3", (0, half_y - margin_y, half_x + margin_x, height)),
        ("q4", (half_x - margin_x, half_y - margin_y, width, height)),
    ]


def _unrotate_box(box, angle, rotated_size):
    """Map a box from rotated image pixels back to base image pixels."""
    width, height = rotated_size
    x0, y0, x1, y1 = box
    angle = angle % 360
    if angle == 0:
        corners = [(x0, y0), (x1, y1)]
    elif angle == 90:
        # CCW: old x = H' - y', old y = x'.
        corners = [(height - y0, x0), (height - y1, x1)]
    elif angle == 180:
        corners = [(width - x0, height - y0), (width - x1, height - y1)]
    elif angle == 270:
        # CW: old x = y', old y = W' - x'.
        corners = [(y0, width - x0), (y1, width - x1)]
    else:
        raise ValueError("Ориентация OCR поддерживает только 0/90/180/270")
    xs = [corner[0] for corner in corners]
    ys = [corner[1] for corner in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _apply_matrix(box, matrix):
    """Apply a PyMuPDF 6-tuple matrix to an axis-aligned box via its corners."""
    a, b, c, d, e, f = (float(value) for value in matrix)
    corners = [(box[0], box[1]), (box[2], box[1]),
               (box[0], box[3]), (box[2], box[3])]
    mapped = [(a * x + c * y + e, b * x + d * y + f) for x, y in corners]
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    return (min(xs), min(ys), max(xs), max(ys))


def _word_to_pdf(word, rotation, rotated_size, crop_origin, base_size, page_rect,
                 page_derotation=None):
    """Inverse-map one TSV word to unrotated PDF points; returns (bbox, clamped).

    ``page_derotation`` is the intrinsic ``page.derotation_matrix`` 6-tuple:
    pixmap pixels live in the rotated display frame, so the final box must
    pass through it to land in unrotated PDF coordinates. Without it boxes
    on rotated pages are silently wrong.
    """
    box = (word["left"], word["top"],
           word["left"] + word["width"], word["top"] + word["height"])
    if crop_origin is not None:
        ox, oy = crop_origin
        box = (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)
    base = _unrotate_box(box, rotation, rotated_size)
    base_w, base_h = base_size
    scale_x = float(page_rect.width) / base_w
    scale_y = float(page_rect.height) / base_h
    mapped = [page_rect.x0 + base[0] * scale_x, page_rect.y0 + base[1] * scale_y,
              page_rect.x0 + base[2] * scale_x, page_rect.y0 + base[3] * scale_y]
    if page_derotation is not None:
        mapped = list(_apply_matrix(mapped, page_derotation))
    bounds = _apply_matrix((page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1), page_derotation) if page_derotation else (page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1)
    clamped = [min(max(mapped[0], bounds[0]), bounds[2]),
               min(max(mapped[1], bounds[1]), bounds[3]),
               min(max(mapped[2], bounds[0]), bounds[2]),
               min(max(mapped[3], bounds[1]), bounds[3])]
    drift = max(abs(mapped[i] - clamped[i]) for i in range(4))
    return clamped, drift > 1.0


def _box_overlap(box_a, box_b):
    inter_x = max(0.0, min(box_a[2], box_b[2]) - max(box_a[0], box_b[0]))
    inter_y = max(0.0, min(box_a[3], box_b[3]) - max(box_a[1], box_b[1]))
    inter = inter_x * inter_y
    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _normalize_text(text):
    import unicodedata
    return unicodedata.normalize("NFKC", text).casefold().strip()


def deduplicate_words(words):
    """Drop overlapping same-text words, keeping the highest confidence."""
    best = {}
    for word in words:
        key = (_normalize_text(word.get("text", "")),
               round(word["bbox"][0], 1), round(word["bbox"][1], 1),
               round(word["bbox"][2], 1), round(word["bbox"][3], 1))
        current = best.get(key)
        if current is None or word.get("conf", -1) > current.get("conf", -1):
            best[key] = word
    grouped = {}
    for word in best.values():
        grouped.setdefault(_normalize_text(word.get("text", "")), []).append(word)
    kept = []
    for same in grouped.values():
        same = sorted(same, key=lambda item: -item.get("conf", -1))
        for word in same:
            if all(_box_overlap(word["bbox"], other["bbox"]) <= 0.5 for other in kept
                   if _normalize_text(other.get("text", "")) == _normalize_text(word.get("text", ""))):
                kept.append(word)
    return kept


def _row_bands(words, reading_angle=0):
    """Group words into row bands along the recognized reading direction.

    ``reading_angle`` is the chosen OCR orientation: at 90/270 the TSV
    pixel frame is transposed relative to the page, so bands must run
    along TSV x (not the already-mapped PDF y). Banding in the wrong
    frame merges columns into rows on landscape content.
    """
    bands = []
    transposed = bool(words) and all('_tbox' in word for word in words)
    key = (lambda item: (item["_tx"], item["_ty"])) if transposed else (
        lambda item: (item["bbox"][1], item["bbox"][0]))
    for word in sorted(words, key=key):
        box = word["bbox"] if not transposed else word["_tbox"]
        placed = False
        for band in bands:
            overlap = max(0.0, min(box[3], band[3]) - max(box[1], band[1]))
            height = min(box[3] - box[1], band[3] - band[1])
            if height > 0 and overlap > 0.5 * height:
                band[2].append(word)
                band[0] = min(band[0], box[0])
                band[1] = min(band[1], box[1])
                band[3] = max(band[3], box[3])
                placed = True
                break
        if not placed:
            bands.append([box[0], box[1], [word], box[3]])
    bands.sort(key=lambda band: band[1])
    rows = []
    for number, band in enumerate(bands, start=1):
        members = sorted(band[2],
                         key=lambda item: item["_tx"] if transposed else item["bbox"][0])
        rows.append({"id": "r%d" % number, "words": members})
    return rows


_GLUE_LEFT_RE = re.compile(r"(?:\d{2}\.\d{2}\.\d{2}|\bПК\s*\d+\+\d+|\d+,\d+)$")
_GLUE_RIGHT_RE = re.compile(r"^(?:\d{2}\b|\+\d+|\d)")


def _glue_line(words):
    """Join date/PK fragments split across adjacent TSV words.

    Engines cut '22.03.2024' into '22.03.20'+'24' and 'ПК 5+90,00' into
    pieces when glyphs touch cell borders. Neighbours join without a
    space only when horizontally adjacent (gap < half height) and the
    pair matches a date/PK/decimal shape; everything else keeps spaces.
    """
    if len(words) < 2:
        return " ".join(word["text"] for word in words)
    parts = [words[0]["text"]]
    for prev, word in zip(words, words[1:]):
        left, right = prev["text"], word["text"]
        previous_box, current_box = prev.get('_tbox', prev['bbox']), word.get('_tbox', word['bbox'])
        gap = current_box[0] - previous_box[2]
        height = max(previous_box[3] - previous_box[1], current_box[3] - current_box[1], 1)
        if (gap < height * 0.5
                and _GLUE_LEFT_RE.search(left)
                and _GLUE_RIGHT_RE.match(right)):
            parts[-1] += right
        else:
            parts.append(right)
    return " ".join(parts)


def _column_strips(words, reading_angle=0, min_gap=8.0):
    """Infer repeated x-column strips shared across row bands.

    Conservative on purpose, fail-closed:
    - a strip edge must repeat across >=3 row boundaries within ±6 pt;
    - the page must have >=3 row bands with >=2 words each (real tables,
      not scattered drawing labels);
    - a single text line with one large gap is NOT a table.
    Returns ``[]`` when nothing repeats. Geometry only, never content.
    """
    by_row: dict[str, list[dict]] = {}
    for word in words:
        row = str(word.get("row_id") or "")
        if row:
            by_row.setdefault(row, []).append(word)
    rows = [sorted(members, key=lambda item: item["_tx"]
                   if '_tbox' in item else item["bbox"][0])
            for members in by_row.values() if len(members) > 1]
    if len(rows) < 3:
        return []
    votes: list[float] = []
    for members in rows:
        for left, right in zip(members, members[1:]):
            if '_tbox' in left and '_tbox' in right:
                gap = right["_tx"] - left["_tx1"]
                if gap >= max(min_gap, 20.0):
                    votes.append((left["_tx1"] + right["_tx"]) / 2.0)
            else:
                gap = right["bbox"][0] - left["bbox"][2]
                if gap >= max(min_gap, 20.0):
                    votes.append((left["bbox"][2] + right["bbox"][0]) / 2.0)
    edges: list[float] = []
    for vote in sorted(votes):
        if all(abs(vote - edge) > 6.0 for edge in edges):
            supporters = sum(1 for other in votes if abs(other - vote) <= 6.0)
            if supporters >= 3:
                edges.append(vote)
    edges.sort()
    if not edges or len(edges) > 6:
        # More than 6 strips is scattered-label noise, not a table grid.
        return []
    strips = []
    bounds = [float("-inf")] + edges + [float("inf")]
    for number in range(len(bounds) - 1):
        strips.append({"id": "o:c%d" % (number + 1),
                       "x0": bounds[number], "x1": bounds[number + 1]})
    return strips


def _strip_for(word, strips, reading_angle=0):
    center = word["_txc"] if '_tbox' in word else (
        (word["bbox"][0] + word["bbox"][2]) / 2.0)
    for strip in strips:
        if strip["x0"] <= center < strip["x1"]:
            return strip["id"]
    return strips[-1]["id"] if strips else ""


def recognize_page(page, *, language="rus+eng", dpi=200, max_pixels=12000000,
                   max_side=4096, timeout=PASS_TIMEOUT_S):
    """OCR one scan page; returns text, PDF-mapped words, and a quality record.

    Raises only when no recognition could run at all (missing Tesseract
    binary, failed render). Exhausted or unreadable passes yield a
    ``failed`` quality with explicit reasons instead of an exception, so
    the caller records an error and never caches a failure as text.
    """
    if shutil.which("tesseract") is None:
        raise RuntimeError("Tesseract недоступен: бинарь tesseract не найден в PATH")
    rect = page.rect
    page_rotation = int(getattr(page, "rotation", 0) or 0)
    try:
        derotation = page.derotation_matrix
        page_derotation = (derotation.a, derotation.b, derotation.c,
                           derotation.d, derotation.e, derotation.f)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        page_derotation = None
    dpi_overview = bounded_render_dpi(
        float(rect.width), float(rect.height), dpi, max_pixels, max_side)
    pix = page.get_pixmap(dpi=dpi_overview, alpha=False)
    base = _pil_from_pixmap(pix)
    base_size = base.size
    scale = (float(rect.width) / base_size[0], float(rect.height) / base_size[1])
    passes_used = 0
    pass_failures = []
    overviews = {}
    for angle in ORIENTATIONS:
        if passes_used >= MAX_TSV_PASSES:
            break
        rotated = _rotate(base, angle)
        try:
            raw = parse_tsv(run_tsv(_to_png(rotated), language, OVERVIEW_PSM, timeout))
            passes_used += 1
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            passes_used += 1
            pass_failures.append("overview rot%d: %s" % (angle, str(exc)[:160]))
            continue
        overviews[angle] = {"size": rotated.size, "words": raw}
    if not overviews:
        return {
            "text": "",
            "words": [],
            "passes_used": passes_used,
            "dpi_overview": dpi_overview,
            "dpi_crop": None,
            "quality": {
                "state": "failed",
                "origin": "ocr",
                "engine": "tesseract",
                "tesseract": tesseract_version(),
                "language": language,
                "algo": ALGO_VERSION,
                "dpi_overview": dpi_overview,
                "dpi_crop": None,
                "psm_overview": OVERVIEW_PSM,
                "psm_crop": CROP_PSM,
                "rotation": 0,
                "page_rotation": page_rotation,
                "passes": passes_used,
                "passes_max": MAX_TSV_PASSES,
                "words_kept": 0,
                "words_dropped_low_conf": 0,
                "confidence_mean": None,
                "reasons": ["all_overview_passes_failed"] + pass_failures,
                "layout": {"rows": 0, "cols": 0, "resolved": False,
                           "reason": "no_overview"},
                "transform": "",
                "crops": [],
            },
        }

    scored = sorted(
        ((score_orientation(entry["words"]), ORIENTATIONS.index(angle), angle)
         for angle, entry in overviews.items()),
        key=lambda item: (-item[0], item[1]))
    best_angle = scored[0][2]
    best = overviews[best_angle]

    candidates = []
    clamped_boxes = 0
    for word in best["words"]:
        # TSV-frame box for layout: transpose-aware, independent of the
        # final PDF mapping below.
        tx0, ty0 = float(word["left"]), float(word["top"])
        tx1, ty1 = tx0 + float(word["width"]), ty0 + float(word["height"])
        bbox, clamped = _word_to_pdf(word, best_angle, best["size"], None,
                                     base_size, rect, page_derotation)
        clamped_boxes += 1 if clamped else 0
        kind = measurement_kind(word["text"])
        candidates.append({
            "text": word["text"], "conf": word["conf"], "bbox": bbox,
            "kind": kind, "rotation": best_angle, "crop": "full",
            "row_id": "", "column_id": "",
            "_tbox": [tx0, ty0, tx1, ty1],
            "_tx": tx0, "_ty": ty0, "_tx1": tx1, "_txc": (tx0 + tx1) / 2.0,
        })

    def gate(words):
        kept, dropped = [], 0
        for word in words:
            if word["conf"] >= gate_for(word["kind"]):
                kept.append(word)
            else:
                dropped += 1
        return kept, dropped

    kept, dropped = gate(candidates)
    mean_conf = (sum(word["conf"] for word in kept) / len(kept)) if kept else None
    needs_crops = (not kept or dropped > 0
                   or (mean_conf is not None and mean_conf < 70))

    crops_done = []
    dpi_crop = None
    recovered = False
    if needs_crops and passes_used < MAX_TSV_PASSES:
        dpi_crop = bounded_render_dpi(
            float(rect.width), float(rect.height),
            min(QUADRANT_DPI, max(dpi, QUADRANT_DPI)), max_pixels, max_side)
        hires = _pil_from_pixmap(page.get_pixmap(dpi=dpi_crop, alpha=False))
        rotated = _rotate(hires, best_angle)
        rotated_size = rotated.size
        for name, box in quadrant_boxes(rotated_size[0], rotated_size[1]):
            if passes_used >= MAX_TSV_PASSES:
                break
            crop = rotated.crop(box)
            enhanced = _enhance_crop(crop)
            rescale = crop.width / enhanced.width
            try:
                raw = parse_tsv(run_tsv(_to_png(enhanced), language, CROP_PSM, timeout))
                passes_used += 1
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                passes_used += 1
                pass_failures.append("crop %s: %s" % (name, str(exc)[:160]))
                continue
            crops_done.append(name)
            origin = (box[0], box[1])
            for word in raw:
                box_px = (word["left"] * rescale, word["top"] * rescale,
                          (word["left"] + word["width"]) * rescale,
                          (word["top"] + word["height"]) * rescale)
                # Rescale crop pixels (hi-res) into the rotated overview frame.
                factor_x = best["size"][0] / rotated_size[0]
                factor_y = best["size"][1] / rotated_size[1]
                frame_box = ((box_px[0] + origin[0]) * factor_x,
                             (box_px[1] + origin[1]) * factor_y,
                             (box_px[2] + origin[0]) * factor_x,
                             (box_px[3] + origin[1]) * factor_y)
                # TSV-frame coords for banding (transpose-aware).
                tx0, ty0, tx1, ty1 = frame_box
                base_box = _unrotate_box(frame_box, best_angle, best["size"])
                mapped = [rect.x0 + base_box[0] * scale[0],
                          rect.y0 + base_box[1] * scale[1],
                          rect.x0 + base_box[2] * scale[0],
                          rect.y0 + base_box[3] * scale[1]]
                if page_derotation is not None:
                    mapped = list(_apply_matrix(mapped, page_derotation))
                bounds = _apply_matrix((rect.x0, rect.y0, rect.x1, rect.y1), page_derotation) if page_derotation else (rect.x0, rect.y0, rect.x1, rect.y1)
                clamped_box = [min(max(mapped[0], bounds[0]), bounds[2]),
                               min(max(mapped[1], bounds[1]), bounds[3]),
                               min(max(mapped[2], bounds[0]), bounds[2]),
                               min(max(mapped[3], bounds[1]), bounds[3])]
                if max(abs(mapped[i] - clamped_box[i]) for i in range(4)) > 1.0:
                    clamped_boxes += 1
                kind = measurement_kind(word["text"])
                candidates.append({
                    "text": word["text"], "conf": word["conf"], "bbox": clamped_box,
                    "kind": kind, "rotation": best_angle, "crop": name,
                    "row_id": "", "column_id": "",
                    "_tbox": [tx0, ty0, tx1, ty1],
                    "_tx": tx0, "_ty": ty0, "_tx1": tx1,
                    "_txc": (tx0 + tx1) / 2.0,
                })
        merged = deduplicate_words(candidates)
        requant, redropped = gate(merged)
        if len(requant) > len(kept):
            recovered = True
        # Final gate over the merged set; the counter reflects the emitted
        # words only, not the pre-crop overview pass.
        kept, dropped = requant, redropped

    rows = _row_bands(kept, best_angle)
    for row in rows:
        for word in row["words"]:
            word["row_id"] = row["id"]
    strips = _column_strips(kept, best_angle)
    for word in kept:
        word["column_id"] = _strip_for(word, strips, best_angle) if strips else ""
    multi_row = len(rows) > 1
    ambiguous = multi_row and not strips
    layout_resolved = not ambiguous
    lines = [_glue_line(row["words"]) for row in rows]
    text = "\n".join(line for line in lines if line.strip())
    raw_total = len(candidates)
    mean_conf = (sum(word["conf"] for word in kept) / len(kept)) if kept else None

    reasons = []
    if clamped_boxes:
        reasons.append("bbox_clamped:%d" % clamped_boxes)
    if page_rotation:
        reasons.append("page_rotated:%d" % page_rotation)
    if recovered:
        reasons.append("quadrant_recovery")
    if dropped:
        reasons.append("low_confidence_dropped:%d" % dropped)
    if not layout_resolved:
        reasons.append("layout_unresolved:multi_row_no_repeated_columns")
    for failure in pass_failures:
        reasons.append("pass_incomplete:%s" % failure)

    if kept:
        clean = (not recovered and not pass_failures and dropped == 0
                 and layout_resolved and not page_rotation
                 and mean_conf is not None and mean_conf >= 70)
        state = "usable" if clean else "uncertain"
        if not clean and not reasons:
            reasons.append("below_usable_gate")
    elif raw_total:
        state = "uncertain"
        reasons.insert(0, "all_words_below_gate")
    elif pass_failures:
        state = "failed"
        reasons.insert(0, "passes_failed_no_text")
    else:
        state = "usable"
        reasons.insert(0, "empty_no_text")

    words_out = [{
        "text": word["text"], "conf": word["conf"],
        "bbox": [float(value) for value in word["bbox"]],
        "kind": word["kind"], "rotation": word["rotation"], "crop": word["crop"],
        "row_id": word.get("row_id", ""), "column_id": word.get("column_id", ""),
    } for word in sorted(kept, key=lambda item: (item["bbox"][1], item["bbox"][0]))]

    quality = {
        "state": state,
        "origin": "ocr",
        "engine": "tesseract",
        "tesseract": tesseract_version(),
        "language": language,
        "algo": ALGO_VERSION,
        "dpi_overview": dpi_overview,
        "dpi_crop": dpi_crop,
        "psm_overview": OVERVIEW_PSM,
        "psm_crop": CROP_PSM,
        "rotation": best_angle,
        "page_rotation": page_rotation,
        "passes": passes_used,
        "passes_max": MAX_TSV_PASSES,
        "words_kept": len(kept),
        "words_dropped_low_conf": dropped,
        "confidence_mean": mean_conf,
        "reasons": reasons,
        "layout": {"rows": len(rows),
                   "cols": len(strips),
                   "resolved": bool(layout_resolved),
                   "reason": "" if layout_resolved else "multi_row_no_repeated_columns"},
        "transform": "rot:%d scale:%.5f,%.5f%s" % (
            best_angle, scale[0], scale[1],
            (" page_rot:%d" % page_rotation) if page_rotation else ""),
        "crops": crops_done,
    }
    return {
        "text": text,
        "words": words_out,
        "passes_used": passes_used,
        "dpi_overview": dpi_overview,
        "dpi_crop": dpi_crop,
        "quality": quality,
    }
