"""Evidence-backed PDF hierarchy. No rendering, OCR jobs or source mutations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pymupdf

VERSION = "pdf-structure-5"
NUMBER = re.compile(
    r"^(?:(?i:раздел|глава|подраздел)\s+)?(\d{1,2}(?:\.\d{1,3}){0,5})[.)]?\s+([А-ЯЁA-Z][^\n]{2,180})$"
)
APPENDIX = re.compile(r"^приложени[ея]\s+([А-ЯЁA-Z]|\d{1,2})(?:\s|$)", re.I)
TABLE = re.compile(
    r"^(?:(Продолжение|Окончание)\s+)?таблиц[аы]\s+(\d+(?:\.\d+)*)(?:\s|[–—:.-]|$)",
    re.I,
)


def _key(text):
    return re.sub(r"\W+", "", text.casefold().replace("ё", "е"))


def native_records(document, saved_pages=None):
    saved_pages = saved_pages or {}
    records = []
    for physical, page in enumerate(document, 1):
        lines = []
        for block in page.get_text(
            "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        )["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                lines.append(
                    {
                        "text": text,
                        "bbox": list(pymupdf.Rect(line["bbox"]) * page.rotation_matrix),
                        "size": max((s.get("size", 0) for s in spans), default=0),
                        "bold": any(s.get("flags", 0) & 16 for s in spans),
                        "source": "digital_text",
                    }
                )
        saved = saved_pages.get(physical, {})
        if not lines:
            for block in saved.get("blocks", []):
                text = str(block.get("text", "")).strip()
                if text:
                    lines.extend(
                        {
                            "text": t.strip(),
                            "bbox": block.get("bbox", []),
                            "size": 0,
                            "bold": False,
                            "source": "saved_ocr",
                        }
                        for t in text.splitlines()
                        if t.strip()
                    )
            if not lines:
                lines = [
                    {
                        "text": t.strip(),
                        "bbox": [],
                        "size": 0,
                        "bold": False,
                        "source": "saved_ocr",
                    }
                    for t in saved.get("text", "").splitlines()
                    if t.strip()
                ]
        records.append(
            {
                "physical_page": physical,
                "width": page.rect.width,
                "height": page.rect.height,
                "lines": lines,
                "pdf_label": page.get_label() if document.get_page_labels() else None,
            }
        )
    return records


def records_from_payload(pages):
    records = []
    for page in pages:
        lines = []
        for block in page.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(x.get("text", "") for x in spans).strip()
                if text:
                    lines.append(
                        {
                            "text": text,
                            "bbox": line.get("bbox", []),
                            "size": max((x.get("size", 0) for x in spans), default=0),
                            "bold": any(x.get("flags", 0) & 16 for x in spans),
                            "source": "digital_text",
                        }
                    )
        if not lines:
            lines = [
                {
                    "text": t.strip(),
                    "bbox": [],
                    "size": 0,
                    "bold": False,
                    "source": "registry_text",
                }
                for t in page.get("text", "").splitlines()
                if t.strip()
            ]
        records.append(
            {
                "physical_page": page["page_number"],
                "width": page.get("width", 600),
                "height": page.get("height", 800),
                "lines": lines,
                "pdf_label": page.get("pdf_label"),
            }
        )
    return records


def build_structure(records, bookmarks=(), *, sha256=""):
    """Shared pages stay inclusive; heading coordinates define exact boundaries."""
    count = max((p["physical_page"] for p in records), default=0)
    by_page = {p["physical_page"]: p for p in records}
    records = [
        by_page.get(n, {"physical_page": n, "width": 600, "height": 800, "lines": []})
        for n in range(1, count + 1)
    ]
    nodes = []
    warnings = []
    running = Counter(
        _key(line["text"])
        for p in records
        for line in p["lines"]
        if line.get("bbox") and line["bbox"][1] < p["height"] * 0.1
    )
    page_map = []
    contents_pages = set()
    for p in records:
        labels = []
        for line in p["lines"]:
            box = line.get("bbox", [])
            if box and (box[1] >= p["height"] * 0.92 or box[3] <= p["height"] * 0.08):
                if re.fullmatch(r"\d{1,4}", line["text"]):
                    labels.append(
                        {"label": line["text"], "source": line["source"], "bbox": box}
                    )
        if p.get("pdf_label"):
            labels.append(
                {"label": p["pdf_label"], "source": "pdf_page_label", "bbox": []}
            )
        unique = {x["label"] for x in labels}
        page_map.append(
            {
                "physical_page": p["physical_page"],
                "width": p["width"],
                "height": p["height"],
                "printed_page": next(iter(unique)) if len(unique) == 1 else None,
                "label_candidates": labels,
                "label_ambiguous": len(unique) > 1,
                "text_available": bool(p["lines"]),
                "node_ids": [],
            }
        )
        if len(unique) > 1:
            warnings.append(
                {"kind": "ambiguous_page_label", "physical_page": p["physical_page"]}
            )
        contents = (
            any(
                re.fullmatch(
                    r"содержание|оглавление|перечень приложений",
                    x["text"].strip(),
                    re.I,
                )
                for x in p["lines"]
            )
            or sum(bool(re.search(r"\.{3}|…{2}", x["text"])) for x in p["lines"]) >= 3
            or sum(bool(APPENDIX.match(x["text"])) for x in p["lines"]) >= 3
        )
        if contents:
            contents_pages.add(p["physical_page"])
            continue
        body = Counter(
            round(x.get("size", 0), 1) for x in p["lines"] if len(x["text"]) > 35
        )
        body_size = body.most_common(1)[0][0] if body else 0
        for index, line in enumerate(p["lines"]):
            text = re.sub(r"\s+", " ", line["text"]).strip()
            if re.search(r"\.{3}|…|\.{2}\s*\d+\s*$", text):
                continue
            box = line.get("bbox", [])
            if (
                box
                and box[1] < p["height"] * 0.1
                and running[_key(text)] >= max(3, count * 0.45)
            ):
                continue
            appendix, table, numbered = (
                APPENDIX.match(text),
                TABLE.match(text),
                NUMBER.fullmatch(text),
            )
            kind, number, level = None, None, 1
            if appendix:
                kind, number = "appendix", appendix[1].upper()
            elif table:
                kind, number = "table", table[2]
            elif numbered:
                number = numbered[1]
                styled = line.get("bold") or (
                    body_size > 0 and line.get("size", 0) > body_size * 1.08
                )
                # Unstyled integer lists and prose references are not section headings.
                if (
                    "." not in number
                    and not styled
                    and not re.match(r"раздел|глава", text, re.I)
                ):
                    continue
                if text.endswith((";", ".")) or len(text) > 180:
                    continue
                if "." not in number and re.match(r"^\d+[.)]\s", text):
                    bookmarked = any(
                        pg == p["physical_page"] and _key(title).startswith(_key(text))
                        for _, title, pg in bookmarks
                    )
                    if not numbered[2].isupper() and not bookmarked:
                        continue
                kind, level = "section", number.count(".") + 1
            elif re.fullmatch(
                r"Введение|Заключение|Список (?:литературы|источников)|Общие положения",
                text,
                re.I,
            ):
                kind = "section"
            if not kind:
                continue
            # PDF exporters may split one visual heading into several adjacent
            # lines/spans. Join only equally bold runs, before the next heading.
            if kind in {"section", "appendix", "table"}:
                last_box = box
                for following in p["lines"][index + 1 : index + 7]:
                    next_text, next_box = following["text"], following.get("bbox", [])
                    if not last_box or not next_box:
                        break
                    same_row = (
                        abs(next_box[1] - last_box[1]) < 3
                        and next_box[0] >= last_box[2] - 2
                    )
                    next_row = (
                        0
                        <= next_box[1] - last_box[3]
                        < max(8, line.get("size", 0) * 1.2)
                    )
                    if not (same_row or next_row) or not (
                        following.get("bold") and line.get("bold")
                    ):
                        break
                    if (
                        NUMBER.fullmatch(next_text)
                        or TABLE.match(next_text)
                        or APPENDIX.match(next_text)
                    ):
                        break
                    if len(text + next_text) >= 220:
                        break
                    if (
                        kind == "table"
                        and not same_row
                        and not re.search(r"[–—-]\s*$", text)
                        and len(text) > 35
                    ):
                        break
                    text += " " + next_text
                    last_box = next_box
            if text.endswith((";", ",")):
                continue
            if table and table[1]:
                previous = next(
                    (
                        n
                        for n in reversed(nodes)
                        if n["kind"] == "table" and n["number"] == number
                    ),
                    None,
                )
                if previous:
                    previous["continuation_pages"].append(p["physical_page"])
                    continue
            nodes.append(
                {
                    "kind": kind,
                    "number": number,
                    "title": text,
                    "level": level,
                    "physical_start": p["physical_page"],
                    "start_bbox": box,
                    "source": line["source"],
                    "confidence": "high" if line.get("bold") else "medium",
                    "continuation_pages": [],
                    "sources": [line["source"]],
                }
            )
    for entry in bookmarks:
        level, title, physical = entry[:3]
        if not 1 <= physical <= count or len(_key(title)) < 4:
            continue
        native = _key(" ".join(x["text"] for x in records[physical - 1]["lines"]))
        if physical in contents_pages or (_key(title) not in native and native):
            warnings.append(
                {
                    "kind": "bookmark_destination_unverified",
                    "title": title,
                    "physical_page": physical,
                }
            )
            continue
        match = next(
            (
                n
                for n in nodes
                if n["physical_start"] == physical
                and (
                    _key(n["title"]).startswith(_key(title))
                    or _key(title).startswith(_key(n["title"]))
                )
            ),
            None,
        )
        if match:
            if len(title) > len(match["title"]):
                match["title"] = title
            if "bookmark" not in match["sources"]:
                match["sources"].append("bookmark")
            match["confidence"] = "high"
            continue
        match_number = NUMBER.fullmatch(title)
        app = APPENDIX.match(title)
        nodes.append(
            {
                "kind": "appendix" if app else "section",
                "number": app[1].upper()
                if app
                else match_number[1]
                if match_number
                else None,
                "title": title,
                "level": level,
                "physical_start": physical,
                "start_bbox": [],
                "source": "bookmark",
                "sources": ["bookmark"],
                "confidence": "high",
                "continuation_pages": [],
            }
        )
    nodes.sort(
        key=lambda n: (
            n["physical_start"],
            n["start_bbox"][1] if n["start_bbox"] else -1,
        )
    )
    root_id = "document"
    stack, appendix_scope, last_section = [], None, None
    seen = set()
    unique_nodes = []
    for n in nodes:
        signature = (n["kind"], _key(n["title"]), n["physical_start"])
        if signature in seen:
            continue
        seen.add(signature)
        n["id"] = hashlib.sha256(
            json.dumps([sha256, signature], ensure_ascii=False).encode()
        ).hexdigest()[:20]
        n["children"] = []
        n["parent_id"] = root_id
        if n["kind"] == "appendix":
            appendix_scope = n
            stack = []
            last_section = n
        elif n["kind"] == "table":
            if last_section:
                n["parent_id"] = last_section["id"]
        else:
            while stack and stack[-1]["level"] >= n["level"]:
                stack.pop()
            if n["number"] and "." in n["number"]:
                prefix = n["number"].rsplit(".", 1)[0]
                parent = next(
                    (s for s in reversed(stack) if s["number"] == prefix), None
                )
                if not parent:
                    warnings.append(
                        {
                            "kind": "parent_heading_not_found",
                            "node_id": n["id"],
                            "number": prefix,
                        }
                    )
            else:
                parent = stack[-1] if stack else None
            n["parent_id"] = (parent or appendix_scope or {}).get("id", root_id)
            stack.append(n)
            last_section = n
        unique_nodes.append(n)
    nodes = unique_nodes
    by_id = {n["id"]: n for n in nodes}
    roots = []
    for index, n in enumerate(nodes):
        # Range ends on the next peer heading's page, because it may be shared.
        next_peer = next(
            (
                other
                for other in nodes[index + 1 :]
                if other["kind"] != "table"
                and (
                    other["kind"] == "appendix"
                    or (n["kind"] != "appendix" and other["level"] <= n["level"])
                )
            ),
            None,
        )
        n["physical_end"] = next_peer["physical_start"] if next_peer else count
        n["end_bbox"] = next_peer["start_bbox"] if next_peer else []
        n["shared_end_page"] = bool(next_peer)
        if n["kind"] == "table":
            n["physical_end"] = max([n["physical_start"], *n["continuation_pages"]])
            n["end_bbox"] = []
            n["shared_end_page"] = False
        n["printed_start"] = page_map[n["physical_start"] - 1]["printed_page"]
        parent = by_id.get(n["parent_id"])
        (parent["children"] if parent else roots).append(n["id"])
        for p in page_map[n["physical_start"] - 1 : n["physical_end"]]:
            p["node_ids"].append(n["id"])
    for p in page_map:
        p["node_ids"].sort(
            key=lambda i: (by_id[i]["level"], by_id[i]["physical_start"])
        )
    if not nodes:
        warnings.append({"kind": "no_headings_found"})
    return {
        "schema_version": VERSION,
        "sha256": sha256,
        "page_count": count,
        "roots": roots,
        "nodes": nodes,
        "pages": page_map,
        "warnings": warnings,
        "quantity_confirmation": False,
        "ocr_dispatched": False,
    }


def resolve_reference(structure, reference):
    matches = []
    by_id = {n["id"]: n for n in structure["nodes"]}
    appendix_targets = {x.casefold() for x in reference.get("appendices", [])}
    for n in structure["nodes"]:
        if appendix_targets and n["kind"] != "appendix":
            ancestor = by_id.get(n["parent_id"])
            while ancestor and ancestor["kind"] != "appendix":
                ancestor = by_id.get(ancestor["parent_id"])
            if (
                not ancestor
                or (ancestor["number"] or "").casefold() not in appendix_targets
            ):
                continue
        targets = (
            reference.get("appendices", [])
            if n["kind"] == "appendix"
            else reference.get("tables", [])
            if n["kind"] == "table"
            else reference.get("sections", [])
        )
        if n["number"] and n["number"].casefold() in {x.casefold() for x in targets}:
            matches.append(
                {
                    "node_id": n["id"],
                    "physical_start": n["physical_start"],
                    "physical_end": n["physical_end"],
                    "title": n["title"],
                    "kind": n["kind"],
                }
            )
    page_matches = []
    for hint in reference.get("page_hints", []):
        candidates = {
            p["physical_page"]
            for p in structure["pages"]
            if p["physical_page"] == hint
            or p["printed_page"] == str(hint)
            or any(x["label"] == str(hint) for x in p["label_candidates"])
        }
        page_matches.append(
            {
                "hint": hint,
                "physical_candidates": sorted(candidates),
                "ambiguous": len(candidates) > 1,
            }
        )
    return {
        "nodes": matches,
        "page_hints": page_matches,
        "ambiguous": len({x["node_id"] for x in matches if x["kind"] == "section"})
        > len(reference.get("sections", [])),
        "navigation_only": True,
    }


def cached_structure(path, sha256, directory, saved_pages=None):
    path, directory = Path(path), Path(directory)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError("PDF identity changed; reindex the project")
    saved_key = hashlib.sha256(
        json.dumps(saved_pages or {}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(f"{sha256}:{VERSION}:{saved_key}".encode()).hexdigest()[
        :32
    ]
    target = directory / f"{cache_key}.json"
    if target.is_file():
        return {**json.loads(target.read_text(encoding="utf-8")), "cache_hit": True}
    with pymupdf.open(path) as document:
        result = build_structure(
            native_records(document, saved_pages), document.get_toc(), sha256=sha256
        )
    pending = directory / f"{cache_key}.{uuid4().hex[:8]}.tmp"
    pending.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    pending.replace(target)
    return {**result, "cache_hit": False}
