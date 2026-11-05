"""Re-evaluate saved physical unit reads without opening a PDF or doing OCR.

The immutable snapshot remains unchanged. Derived proofs still have to pass
the physical-column validator; neither VOR values nor expected units are inputs.
"""

from copy import deepcopy
import re

from pdf_cell_ocr import literal_unit, recovered_numeric_value, NUMERIC_RECOVERY_VERSION
from pdf_table_verification import (
    VERSION,
    _ordered_headers,
    header_unit,
    plain_m_proof,
    verified_column_unit,
)

VERSION_REPLAY = "saved-physical-unit-proof-v1"
NUMERIC_REPLAY = "saved-multi-engine-number-v1"
GEOMETRY_REPLAY = "saved-physical-header-geometry-v1"
DEPTH = re.compile(
    r"(?:[hн]\s*=\s*|[([]\s*)(\d+(?:[.,]\d+)?)\s*(мм|mm|см|cm|м|m)\b", re.I
)


def header_geometry(header):
    """Literal depth, corroborated in the same physical header by both engines."""
    from normative_parameters import measure, number

    from pdf_table_verification import visual_header_text, VISUAL_VERSION

    visual = visual_header_text(header)
    if visual:
        matches = list(DEPTH.finditer(visual))
        if len(matches) != 1:
            return []
        match = matches[0]
        end = match.end() + (
            1 if visual[match.end() : match.end() + 1] in {")", "]"} else 0
        )
        return [
            dict(
                parameter="thickness",
                value=str(number(match[1])),
                unit=match[2].casefold(),
                quote=visual[match.start() : end],
                version=VISUAL_VERSION,
            )
        ]

    proof = header.get("header_refinement") or {}
    if (
        proof.get("version") != VERSION
        or proof.get("header_id") != header.get("cell_id")
        or proof.get("bbox") != header.get("bbox")
        or proof.get("column") != header.get("column")
        or proof.get("row") != header.get("row")
        or proof.get("colspan", 1) != header.get("colspan", 1)
        or [v.get("scale") for v in proof.get("variants", [])] != [1, 1.35]
    ):
        return []

    def found(words, confidence, key):
        output = set()
        for word in words:
            if word.get(key, 0) < confidence:
                continue
            for match in DEPTH.finditer(word.get("text", "")):
                value = measure(match[1], match[2].casefold())
                if value:
                    output.add(value)
        return output

    rapid = found(header.get("words", []), 0.98, "confidence")
    reads = [found(v.get("words", []), 80, "conf") for v in proof["variants"]]
    if len(rapid) != 1 or any(read != rapid for read in reads):
        return []
    result = []
    for match in DEPTH.finditer(header.get("text", "")):
        if measure(match[1], match[2].casefold()) not in rapid:
            continue
        quote = match[0]
        result.append(
            dict(
                parameter="thickness",
                value=str(number(match[1])),
                unit=match[2].casefold(),
                quote=quote,
                version=GEOMETRY_REPLAY,
            )
        )
    return result if len(result) == 1 else []


def saved_numeric_value(cell):
    """Require whole-cell agreement of both OCR engines, never a VOR value.

    The strict original 90% rule remains available. The additional route
    requires TWO >=99% RapidOCR reads (page cell and independent crop), plus
    two complete Tesseract reads at >=80% at different image heights. Any
    conflicting numeric read at >=75% vetoes consensus. Fragment concatenation
    and repeated copies of one read never count as corroboration.
    """
    from normative_parameters import number

    proof = cell.get("numeric_recovery") or {}
    if (
        proof.get("version") != NUMERIC_RECOVERY_VERSION
        or proof.get("bbox") != cell.get("bbox")
        or not re.fullmatch("[0-9a-f]{64}", proof.get("image_sha256", ""))
    ):
        return None
    strict = recovered_numeric_value(cell, proof)
    if strict is not None:
        return strict
    value = number(cell.get("text", ""))
    if value is None:
        return None
    raw = cell.get("words") or []
    fresh = proof.get("rapid_words") or []
    if (
        len(raw) != 1
        or len(fresh) != 1
        or min(raw[0].get("confidence", 0), fresh[0].get("confidence", 0)) < 0.99
        or any(number(w.get("text", "")) != value for w in [*raw, *fresh])
        or proof.get("prior_words") != raw
        or number(proof.get("prior_text")) != value
    ):
        return None
    box = cell.get("bbox") or []
    original_box = raw[0].get("bbox") or []
    if (
        len(box) != 4
        or len(original_box) != 4
        or not (
            box[0] <= (original_box[0] + original_box[2]) / 2 <= box[2]
            and box[1] <= (original_box[1] + original_box[3]) / 2 <= box[3]
        )
    ):
        return None
    variants = proof.get("variants", [])
    readings = set()
    for variant in [*proof.get("prior_variants", []), *variants]:
        words = variant.get("words") or []
        actual = number("".join(w.get("text", "") for w in words))
        confidence = min((w.get("conf", 0) for w in words), default=0)
        if actual is not None and confidence >= 75 and actual != value:
            return None
    for variant in variants:
        words = variant.get("words") or []
        if (
            len(words) == 1
            and words[0].get("conf", 0) >= 80
            and number(words[0].get("text", "")) == value
            and isinstance(variant.get("height"), int)
        ):
            readings.add(variant["height"])
    return str(value) if len(readings) >= 2 else None


def _candidate(saved):
    if saved.get("version") != VERSION or len(saved.get("variants", [])) != 2:
        return None
    proof = deepcopy(saved)
    votes = [header_unit(v["words"]) for v in proof["variants"]]
    crop = proof.get("unit_crop")
    unit = None
    if crop:
        attempts = crop.get("attempts", crop.get("variants", []))
        readable = [
            (literal_unit(v["words"]), v) for v in attempts if literal_unit(v["words"])
        ]
        if len(readable) >= 2 and len({u for u, _ in readable}) == 1:
            unit = readable[0][0]
            crop["variants"] = [v for _, v in readable[:2]]
        elif plain_m_proof(proof, crop):
            unit = "м"
    elif votes[0] and votes[0] == votes[1]:
        unit = votes[0]
    if not unit or any(v and v != unit for v in votes):
        return None
    proof.update(unit=unit, replay_version=VERSION_REPLAY)
    return proof


def replay_unit_proofs(tables):
    """Return a derived view and an audit trail; never change source snapshots."""
    output, restored = [], []
    for original in tables:
        if not original.get("recovery"):
            output.append(original)
            continue
        table = deepcopy(original)
        for cell in table["cells"]:
            if (
                cell.get("role") != "cell"
                or cell.get("numeric_verified")
                or cell.get("quality") != "usable"
            ):
                continue
            value = saved_numeric_value(cell)
            if value is not None:
                cell["numeric_verified"] = True
                cell["numeric_saved_evidence"] = dict(
                    version=NUMERIC_REPLAY,
                    value=value,
                    image_sha256=cell["numeric_recovery"]["image_sha256"],
                )
                restored.append(
                    dict(
                        kind=NUMERIC_REPLAY,
                        table_id=table["table_id"],
                        cell_id=cell["cell_id"],
                        value=value,
                        image_sha256=cell["numeric_recovery"]["image_sha256"],
                    )
                )
        # Replaying OCR votes must not rebuild or repair the captured scope
        # tree. Header ancestry belongs to the immutable prepared snapshot.
        seen = set()
        for header in _ordered_headers(table):
            column = header["column"]
            if header.get("colspan", 1) != 1 or column in seen:
                continue
            seen.add(column)  # Only the deepest physical unit leaf may supply it.
            body = [
                c
                for c in table["cells"]
                if c.get("role") == "cell" and c["column"] == column
            ]
            existing = (table.get("column_units") or {}).get(str(column), {})
            if any(verified_column_unit(table, c, existing) for c in body):
                continue
            proof = _candidate(header.get("header_refinement") or {})
            if proof is None or not any(
                verified_column_unit(table, c, proof) for c in body
            ):
                continue
            table.setdefault("column_units", {})[str(column)] = proof
            restored.append(
                dict(
                    kind=VERSION_REPLAY,
                    table_id=table["table_id"],
                    header_id=header["cell_id"],
                    column=column,
                    unit=proof["unit"],
                    image_sha256=proof.get("image_sha256"),
                )
            )
        output.append(table)
    return output, restored
