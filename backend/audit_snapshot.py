"""Frozen preparation boundary: content-addressed PageTextIndex snapshots.

``prepare_run`` (CPU queue) owns every PDF handle and persists one compressed
canonical snapshot per bound position through ``BlobStore.put``. The LLM
worker only restores that snapshot via ``PageTextIndex.from_snapshot`` and
never reopens, re-extracts, or OCRs a PDF. The direct runner uses the same
encode/decode contract in memory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import OrderedDict
from threading import Lock
from typing import Any

SNAPSHOT_SCHEMA = 1

_COMPRESS_MTIME = 0
_PAGE_CACHE_LIMIT = 32 * 1024 * 1024
_page_cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
_page_cache_size = 0
_page_cache_lock = Lock()


def encode_snapshot(index: Any) -> bytes:
    """Serialize a prepared index to canonical compressed bytes."""
    snapshot = index.to_snapshot()
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(
            f"Unsupported extraction snapshot schema: {snapshot.get('schema')!r}"
        )
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return gzip.compress(encoded, mtime=_COMPRESS_MTIME)


def decode_snapshot(data: bytes) -> Any:
    """Restore a prepared index; stale/corrupt corpora raise, never refetch."""
    from audit_core import PageTextIndex

    try:
        raw = gzip.decompress(bytes(data))
    except (OSError, EOFError, ValueError) as exc:
        raise ValueError(f"Extraction snapshot is not valid gzip: {exc}") from exc
    try:
        snapshot = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Extraction snapshot is not valid JSON: {exc}") from exc
    return PageTextIndex.from_snapshot(snapshot)


def snapshot_ref(snapshot: dict[str, Any], blob_key: str, blob_sha256: str) -> dict[str, Any]:
    """Job-payload reference: key/SHA/schema only, never megabytes of text."""
    corpus_id = snapshot.get("corpus_id")
    if not corpus_id or not blob_key or not blob_sha256:
        raise ValueError("Extraction snapshot reference is incomplete")
    return {
        "snapshot_key": blob_key,
        "snapshot_sha256": blob_sha256,
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "corpus_id": corpus_id,
    }


def store_json(blob_store: Any, value: Any, *, cache=None) -> dict[str, str]:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    identity = hashlib.sha256(raw).hexdigest()
    if cache is not None and identity in cache:
        return dict(cache[identity])
    data = gzip.compress(raw, mtime=_COMPRESS_MTIME)
    ref = blob_store.put(data)
    if ref.sha256 != hashlib.sha256(data).hexdigest():
        raise ValueError("Blob store returned a mismatched snapshot hash")
    result = {"key": ref.key, "sha256": ref.sha256}
    if cache is not None:
        cache[identity] = result
    return result


def load_json(blob_store: Any, ref: dict[str, Any], *, cache_page=False) -> Any:
    global _page_cache_size
    namespace = getattr(blob_store, "cache_namespace", None) if cache_page else None
    key = (namespace, ref["key"], ref.get("sha256", "")) if namespace else None
    raw = None
    if key:
        with _page_cache_lock:
            raw = _page_cache.get(key)
            if raw is not None:
                _page_cache.move_to_end(key)
    if raw is None:
        data = blob_store.get(ref["key"])
        if hashlib.sha256(data).hexdigest() != ref.get("sha256"):
            raise ValueError("Shared blob hash mismatch")
        raw = gzip.decompress(data)
        if key and len(raw) <= _PAGE_CACHE_LIMIT:
            with _page_cache_lock:
                previous = _page_cache.pop(key, None)
                if previous is not None:
                    _page_cache_size -= len(previous)
                _page_cache[key] = raw
                _page_cache_size += len(raw)
                while _page_cache_size > _PAGE_CACHE_LIMIT:
                    _, removed = _page_cache.popitem(last=False)
                    _page_cache_size -= len(removed)
    # Cache immutable, verified bytes only. Each caller receives fresh mutable
    # structures; one position cannot alter another position's evidence.
    return json.loads(raw.decode("utf-8"))


def store_snapshot(blob_store: Any, index: Any, *, page_cache=None) -> dict[str, Any]:
    """Share immutable pages across positions; only changed evidence is written."""
    snapshot = index.to_snapshot()
    page_fields = {"texts", "errors", "quality", "pending_ocr", "table_rows", "locators", "tables"}
    pages = []
    pending = set(snapshot["pending_ocr"])
    for i, text in enumerate(snapshot["texts"]):
        page = {"text": text, "pending": i in pending}
        for field in ("errors", "quality"):
            if str(i) in snapshot[field]:
                page[field] = snapshot[field][str(i)]
        for field in ("table_rows", "locators"):
            if snapshot[field] is not None:
                page[field] = snapshot[field][i]
        if snapshot.get("tables") is not None:
            page["tables"] = snapshot["tables"][i]
        pages.append(store_json(blob_store, page, cache=page_cache))
    manifest = {
        "format": "page-snapshot-v1", "pages": pages,
        "has_table_rows": snapshot["table_rows"] is not None,
        "has_locators": snapshot["locators"] is not None,
        "has_tables": snapshot.get("tables") is not None,
        "snapshot": {k: v for k, v in snapshot.items() if k not in page_fields},
    }
    ref = store_json(blob_store, manifest)
    result = snapshot_ref(snapshot, ref["key"], ref["sha256"])
    result["snapshot_storage"] = "pages-v1"
    return result


def load_snapshot(blob_store: Any, payload: dict[str, Any]) -> Any:
    """Fetch and verify a snapshot reference; failures are explicit prep errors."""
    key = payload.get("snapshot_key", "")
    want_sha = payload.get("snapshot_sha256", "")
    if payload.get("snapshot_schema") != SNAPSHOT_SCHEMA or not key or not want_sha:
        raise ValueError("Подготовка позиции не выполнена: снимок корпуса отсутствует")
    try:
        data = blob_store.get(key)
    except FileNotFoundError as exc:
        raise ValueError(
            "Подготовка позиции не выполнена: байты снимка корпуса "
            f"отсутствуют в хранилище ({key})"
        ) from exc
    if hashlib.sha256(bytes(data)).hexdigest() != want_sha:
        raise ValueError("Подготовка позиции не выполнена: хеш снимка корпуса не совпал")
    if payload.get("snapshot_storage") == "pages-v1":
        from audit_core import PageTextIndex
        manifest = json.loads(gzip.decompress(bytes(data)).decode("utf-8"))
        if manifest.get("format") != "page-snapshot-v1":
            raise ValueError("Invalid page snapshot manifest")
        snapshot = dict(manifest["snapshot"])
        snapshot.update(texts=[], errors={}, quality={}, pending_ocr=[],
                        table_rows=[] if manifest["has_table_rows"] else None,
                        locators=[] if manifest["has_locators"] else None)
        if manifest.get("has_tables"):
            snapshot["tables"] = []
        for i, ref in enumerate(manifest["pages"]):
            page = load_json(blob_store, ref, cache_page=True)
            snapshot["texts"].append(page["text"])
            if page["pending"]:
                snapshot["pending_ocr"].append(i)
            for field in ("errors", "quality"):
                if field in page:
                    snapshot[field][str(i)] = page[field]
            for field in ("table_rows", "locators"):
                if snapshot[field] is not None:
                    snapshot[field].append(page[field])
            if manifest.get("has_tables"):
                snapshot["tables"].append(page["tables"])
        index = PageTextIndex.from_snapshot(snapshot)
    else:
        # Already queued jobs keep their original monolithic snapshots.
        index = decode_snapshot(bytes(data))
    corpus_id = payload.get("corpus_id") or ""
    actual = index.to_snapshot().get("corpus_id", "")
    if corpus_id and actual != corpus_id:
        raise ValueError("Подготовка позиции не выполнена: корпус снимка не совпал")
    return index
