"""Verified, content-addressed SQLite copies on a native filesystem.

Only immutable normative bytes are cached. Source PDFs, release identity and
engineering decisions keep their original provenance. No integrity shortcut.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _stamp(source):
    stat = source.stat()
    return dict(
        source=str(source.resolve()),
        size=stat.st_size,
        mtime=stat.st_mtime_ns,
        ctime=stat.st_ctime_ns,
    )


def verified_database(source, expected_sha=None):
    folder = os.environ.get("NORMATIVE_SQLITE_CACHE_DIR", "").strip()
    if not folder or not expected_sha:
        return source, _sha(source)
    if not re.fullmatch("[0-9a-f]{64}", expected_sha):
        raise ValueError("Invalid normative database SHA")
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / (expected_sha + ".sqlite")
    if target.resolve() == source.resolve():
        return source, _sha(source)
    metadata = root / (expected_sha + ".json")
    stamp = _stamp(source)
    try:
        saved = json.loads(metadata.read_text(encoding="utf-8"))
    except OSError, ValueError:
        saved = None
    if saved == stamp and target.is_file() and _sha(target) == expected_sha:
        return target, expected_sha
    with tempfile.NamedTemporaryFile(
        dir=root, prefix=expected_sha + ".", suffix=".tmp", delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
            actual = digest.hexdigest()
            if actual != expected_sha or _stamp(source) != stamp:
                raise ValueError("Изменился сохранённый выпуск базы знаний")
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=expected_sha + ".",
            suffix=".json.tmp",
            delete=False,
        ) as output:
            marker = Path(output.name)
            json.dump(stamp, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(marker, metadata)
    finally:
        temporary.unlink(missing_ok=True)
        if "marker" in locals():
            marker.unlink(missing_ok=True)
    return target, actual
