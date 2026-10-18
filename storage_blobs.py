"""Content-addressed blob storage for source files and audit artifacts.

PostgreSQL holds metadata only; bytes live here. Keys embed ``sha256`` so the
same content is stored once and documents are identified by hash, not basename.
The local layout is ``<root>/<aa>/<bb>/<sha256>``; ``open_blob_store`` is the
seam where an S3-compatible backend can replace ``FileBlobStore``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


@dataclass(frozen=True, slots=True)
class BlobRef:
    key: str
    sha256: str
    size: int


class BlobStore:
    """Byte store seam; S3-compatible backends implement this interface."""

    def put(self, data: bytes) -> BlobRef:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError


class FileBlobStore(BlobStore):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    def put(self, data: bytes) -> BlobRef:
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        key = f"{digest[:2]}/{digest[2:4]}/{digest}"
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        return BlobRef(key=key, sha256=digest, size=len(payload))

    def get(self, key: str) -> bytes:
        path = self._root / key
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()


def open_blob_store(url: str) -> BlobStore:
    """Open ``file://`` blob storage; other schemes are a future S3 seam."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported blob store scheme: {parsed.scheme or url!r}")
    root = url2pathname(parsed.path) or "."
    return FileBlobStore(root)
