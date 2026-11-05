"""Literal visual readings supplement OCR; they never establish a VOR verdict.

Coordinates are unrotated PDF points, matching existing source locators.
Readings are immutable, page- and byte-bound and retain their review method.
"""

from copy import deepcopy
import math
import re
from uuid import UUID

from analysis_replay import digest

STAGE = "project_drawing_reading_v1"


def identity(project, version, sha256, page):
    return dict(schema=1, stage=STAGE, project_id=str(project),
                version_id=str(version), pdf_sha256=sha256, page=page)


def validate(reading, ident, index):
    if any(reading.get(k) != ident[k] for k in ("version_id", "pdf_sha256", "page")):
        raise ValueError("Drawing reading belongs to another source")
    page = reading["page"]
    if type(page) is not int or not 1 <= page <= len(index.texts):
        raise ValueError("Invalid drawing page")
    if reading.get("extraction_sha256") != index.metadata.get("pdf_sha256"):
        raise ValueError("Drawing extraction identity changed")
    for key in ("pdf_sha256", "extraction_sha256", "render_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(reading.get(key, ""))):
            raise ValueError("Missing drawing source hash")
    if not reading.get("reader") or not reading.get("method"):
        raise ValueError("Drawing reader and method required")
    bounds = reading.get("unrotated_page_size", [])
    if len(bounds) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in bounds):
        raise ValueError("Invalid page size")
    facts = reading.get("facts") or []
    if not facts or len({f.get("id") for f in facts}) != len(facts):
        raise ValueError("Nonempty unique drawing facts required")
    for fact in facts:
        box = fact.get("bbox", [])
        if (not fact.get("id") or not isinstance(fact.get("text"), str) or not fact["text"].strip()
                or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                or not 0 <= box[0] < box[2] <= bounds[0] + .01
                or not 0 <= box[1] < box[3] <= bounds[1] + .01):
            raise ValueError("Invalid literal drawing fact or geometry")
    return deepcopy(reading)


def save(context, record, version, reading, index):
    if str(version) not in (record["config"].get("pdf_versions") or {}).values():
        raise ValueError("Drawing requires a pinned project PDF")
    document = context.repository.load_version(UUID(str(version)))
    ident = identity(record["project_id"], version, document.sha256, reading["page"])
    value = validate(reading, ident, index)
    if context.repository.save_analysis_artifact(record["project_id"], ident, value) != value:
        raise ValueError("Different drawing reading already exists; preserve history")
    return digest(ident)


def apply(context, record, version, index):
    """Overlay saved literals on an immutable copy, retaining every OCR error.

The review is partial, not CAD reconstruction or complete quantity extraction.
Its source IDs/coordinates can be shown and read by the evidence agent.
"""
    if (getattr(context.repository, "supports_analysis_replay", False) is not True
            or str(version) not in (record["config"].get("pdf_versions") or {}).values()):
        return index
    document = context.repository.load_version(UUID(str(version)))
    if index.metadata.get("document_version_id") != str(version):
        raise ValueError("Drawing corpus version mismatch")
    from audit_core import PageTextIndex
    texts = list(index.texts)
    locators = [list(p) for p in index.locators] if index.locators is not None else [[] for _ in texts]
    metadata = deepcopy(index.metadata)
    readings = dict(metadata.get("drawing_readings") or {})
    changed = False
    lookup = getattr(context.repository, "drawing_reading_artifacts", None)
    if not callable(lookup):
        return index
    for artifact in lookup(record["project_id"], str(version)):
        page = artifact["identity"]["page"]
        ident = identity(record["project_id"], version, document.sha256, page)
        if artifact["identity"] != ident:
            raise ValueError("Drawing artifact identity mismatch")
        reading = validate(artifact["payload"], ident, index)
        key = digest(reading)
        previous = readings.get(str(page))
        if previous:
            if previous["reading_sha256"] != key:
                raise ValueError("Conflicting drawing overlay")
            continue
        texts[page - 1] += "\n\n[Визуальный разбор чертежа; частичный; исходный OCR сохранён]\n" + "\n".join(f["text"] for f in reading["facts"])
        for fact in reading["facts"]:
            locators[page - 1].append(dict(source_id=f"drawing:{key[:20]}:{fact['id']}",
                document_id="pdf-" + reading["extraction_sha256"][:12], page=page,
                text=fact["text"], bbox=fact["bbox"], origin="visual_transcription",
                role="drawing_reading", quality="requires_engineer_review"))
        readings[str(page)] = dict(reading_sha256=key, artifact_key=digest(ident),
            state="partially_visually_read", fact_count=len(reading["facts"]),
            limitations=reading.get("limitations", []), reader=reading["reader"])
        changed = True
    if not changed:
        return index
    metadata["drawing_readings"] = readings
    return PageTextIndex(texts, index.errors, index.pending_ocr, index.table_rows,
        locators=locators, quality=index.quality, metadata=metadata,
        tables=index.tables, repeated_lines=index.repeated_lines)
