"""Read-only helpers for the standalone normative workbench."""
from __future__ import annotations

import json
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from normative_corpus import Corpus


TABLES = (
    "sources", "pages", "clauses", "norm_tables", "norms", "resources",
    "rules", "relations", "gaps", "review_queue", "chunks",
)
CODE_RE = re.compile(r"(?<!\d)(\d{2}-\d{2}-\d{3}-\d{2,3})(?!\d)")


def _metadata(db: sqlite3.Connection) -> dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM metadata")}


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return fallback


def manager_profile(profile: str) -> dict[str, Any]:
    import normative_service

    kb: Corpus = normative_service.corpus(profile=profile)
    with kb.connect() as db:
        _metadata(db)
        counts = _counts(db)
        sources = [dict(row) for row in db.execute(
            "SELECT s.id,s.designation,s.kind,s.role,s.pages, "
            "(SELECT count(*) FROM pages p WHERE p.source_id=s.id AND length(trim(p.clean_text))>50) AS readable_pages "
            "FROM sources s ORDER BY s.kind,s.designation"
        )]
        review_queue = []
        for row in db.execute(
            "SELECT q.id,q.source_id,s.designation,q.entity_id,q.reason,q.details_json "
            "FROM review_queue q LEFT JOIN sources s ON s.id=q.source_id "
            "ORDER BY q.reason,q.entity_id LIMIT 100"
        ):
            item = dict(row)
            details = _read_json_text(item.pop("details_json", ""))
            citation = details.get("citation") if isinstance(details, dict) else None
            item["page"] = citation.get("physical_page") if isinstance(citation, dict) else None
            review_queue.append(item)
        gaps = [dict(row) for row in db.execute("SELECT * FROM gaps ORDER BY designation LIMIT 100")]

    current_path = kb.path.resolve()
    revisions = []
    for database in sorted(kb.root.glob("releases/*/knowledge.sqlite"), reverse=True):
        resolved = database.resolve()
        if not resolved.is_relative_to(kb.root):
            continue
        try:
            quality = _read_json(resolved.parent / "quality-report.json", {})
            with sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True) as revision_db:
                revision_db.row_factory = sqlite3.Row
                revision_meta = _metadata(revision_db)
                revision_counts = _counts(revision_db)
                if "integrity" in quality and "foreign_keys" in quality:
                    integrity = quality["integrity"]
                    foreign_keys = quality["foreign_keys"]
                else:
                    integrity = revision_db.execute("PRAGMA integrity_check").fetchone()[0]
                    foreign_keys = "ok" if not revision_db.execute("PRAGMA foreign_key_check").fetchall() else "errors"
            revisions.append({
                "release_id": revision_meta.get("release_id", resolved.parent.name),
                "database": resolved.relative_to(kb.root).as_posix(),
                "observed_on": revision_meta.get("observed_on", ""),
                "parser_version": revision_meta.get("parser_version", ""),
                "current": resolved == current_path,
                "counts": revision_counts,
                "integrity": str(quality.get("integrity", integrity)),
                "foreign_keys": str(quality.get("foreign_keys", foreign_keys)),
                "engineering_validation": str(quality.get("engineering_validation", "not_full")),
            })
        except (OSError, sqlite3.Error, ValueError, KeyError):
            continue

    return {
        "profile": profile,
        "title": "Дорожные нормы" if profile == "roads" else "Электротехнические нормы",
        "identity": kb.identity,
        "counts": counts,
        "releases": revisions,
        "sources": sources,
        "gaps": gaps,
        "review_queue": review_queue,
        "review_queue_total": counts["review_queue"],
    }


def _read_json_text(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def code_from_position(work_name: str, reference: str) -> str:
    match = CODE_RE.search(f"{work_name} {reference}")
    return match.group(1) if match else ""


def title_similarity(left: str, right: str) -> int:
    def words(value: str) -> set[str]:
        return set(re.findall(r"[а-яёa-z0-9]+", value.casefold()))

    a, b = words(left), words(right)
    token_score = (2 * len(a & b) / (len(a) + len(b))) if a and b else 0
    sequence_score = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return round(100 * max(token_score, sequence_score))

