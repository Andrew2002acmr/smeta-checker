"""Read-only catalog of source PDFs kept in the repository's base directory."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any


_KIND_ORDER = ("ГЭСН", "СП", "СНиП", "ГОСТ", "ПУЭ", "ДРУГОЕ")
_CACHE_ROOT: str | None = None
_CACHE_SIGNATURE: tuple[tuple[str, int, int], ...] | None = None
_CACHE_FILES: list[dict[str, Any]] = []


def catalog_root() -> Path:
    configured = os.environ.get("NORMATIVE_CATALOG_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[1] / "base"


def _source_kind(path: Path, root: Path) -> str:
    text = " ".join(path.relative_to(root).parts).upper()
    for kind in _KIND_ORDER[:-1]:
        if kind.upper() in text:
            return kind
    return "ДРУГОЕ"


def _section_title(directory: str) -> str:
    if not directory:
        return "Без раздела"
    titles = []
    for part in directory.split("/"):
        match = re.match(r"^(\d{2})\s+(.*)$", part)
        if not match:
            titles.append(part)
            continue
        description = re.sub(
            r"^Государственные элементные сметные нормы на\s*", "", match.group(2), flags=re.I
        )
        titles.append(f"{match.group(1)} · {description}")
    return " / ".join(titles)


def _catalog_files(root: Path) -> list[dict[str, Any]]:
    global _CACHE_ROOT, _CACHE_SIGNATURE, _CACHE_FILES
    if not root.is_dir():
        _CACHE_ROOT, _CACHE_SIGNATURE, _CACHE_FILES = str(root), (), []
        return []

    paths = []
    for path in root.rglob("*.pdf"):
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(root) and resolved.is_file():
                paths.append((path, resolved))
        except OSError:
            continue
    paths.sort(key=lambda pair: pair[0].relative_to(root).as_posix().casefold())
    signature = tuple(
        (path.relative_to(root).as_posix(), resolved.stat().st_size, resolved.stat().st_mtime_ns)
        for path, resolved in paths
    )
    if _CACHE_ROOT == str(root) and _CACHE_SIGNATURE == signature:
        return _CACHE_FILES

    entries = []
    for path, resolved in paths:
        relative = path.relative_to(root)
        with resolved.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entries.append(
            {
                "id": relative.as_posix(),
                "designation": path.stem,
                "section": relative.parent.as_posix() if len(relative.parts) > 1 else "",
                "kind": _source_kind(path, root),
                "size_bytes": resolved.stat().st_size,
                "sha256": digest,
            }
        )
    _CACHE_ROOT, _CACHE_SIGNATURE, _CACHE_FILES = str(root), signature, entries
    return entries


def collections(imported_hashes: set[str]) -> list[dict[str, Any]]:
    root = catalog_root()
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in _catalog_files(root):
        kind = entry["kind"]
        section = entry["section"]
        file_info = {
            "id": entry["id"],
            "designation": entry["designation"],
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
            "imported": entry["sha256"] in imported_hashes,
        }
        grouped.setdefault(kind, {}).setdefault(section, []).append(file_info)

    return [
        {
            "kind": kind,
            "file_count": sum(len(files) for files in sections.values()),
            "sections": [
                {"title": _section_title(section), "files": files}
                for section, files in sorted(sections.items(), key=lambda item: item[0].casefold())
            ],
        }
        for kind in _KIND_ORDER
        if (sections := grouped.get(kind))
    ]


def pdf_path(relative_path: str) -> Path:
    root = catalog_root()
    path = (root / relative_path).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.casefold() != ".pdf" or not path.is_file():
        raise FileNotFoundError(relative_path)
    return path
