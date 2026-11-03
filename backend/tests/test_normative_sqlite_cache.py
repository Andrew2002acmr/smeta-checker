import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from normative_sqlite_cache import verified_database


def data(tmp_path, monkeypatch):
    source = tmp_path / "original" / "knowledge.sqlite"
    source.parent.mkdir()
    source.write_bytes(b"Immutable normative database")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = tmp_path / "native-volume"
    monkeypatch.setenv("NORMATIVE_SQLITE_CACHE_DIR", str(cache))
    return source, expected, cache


def test_cold_copy_is_byte_identical_and_warm_read_does_not_scan_windows_source(
    tmp_path, monkeypatch
):
    source, sha, cache = data(tmp_path, monkeypatch)
    frozen = source.read_bytes()
    local, actual = verified_database(source, sha)
    assert local.parent == cache and actual == sha and local.read_bytes() == frozen
    real_open = Path.open

    def guarded(path, *args, **kwargs):
        if path == source:
            raise AssertionError(
                "Warm lookup must hash the native cache, not the Windows source"
            )
        return real_open(path, *args, **kwargs)

    with patch.object(Path, "open", guarded):
        assert verified_database(source, sha) == (local, sha)
    assert source.read_bytes() == frozen


def test_changed_original_is_rejected_despite_existing_valid_cache(
    tmp_path, monkeypatch
):
    source, sha, _ = data(tmp_path, monkeypatch)
    local, _ = verified_database(source, sha)
    source.write_bytes(b"Changed edition without pointer update")
    with pytest.raises(ValueError, match="Изменился"):
        verified_database(source, sha)
    assert hashlib.sha256(local.read_bytes()).hexdigest() == sha


def test_corrupt_cache_is_rebuilt_from_verified_original(tmp_path, monkeypatch):
    source, sha, _ = data(tmp_path, monkeypatch)
    local, _ = verified_database(source, sha)
    local.write_bytes(b"corrupted cached bytes")
    restored, actual = verified_database(source, sha)
    assert restored == local and actual == sha
    assert restored.read_bytes() == source.read_bytes()


def test_invalid_original_leaves_no_partial_cache_and_no_fake_sha(
    tmp_path, monkeypatch
):
    source, _, cache = data(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Изменился"):
        verified_database(source, "f" * 64)
    assert not list(cache.iterdir())


def test_disabled_or_unpinned_corpus_keeps_direct_integrity_check(
    tmp_path, monkeypatch
):
    source, sha, cache = data(tmp_path, monkeypatch)
    assert verified_database(source) == (source, sha)
    assert not cache.exists()
    monkeypatch.delenv("NORMATIVE_SQLITE_CACHE_DIR")
    assert verified_database(source, sha) == (source, sha)


def test_corpus_uses_native_copy_but_preserves_release_identity_and_pdf_root(
    tmp_path, monkeypatch
):
    from test_normative_corpus_cache import database
    from normative_corpus import Corpus

    original = tmp_path / "norms"
    original.mkdir()
    path = database(original)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    before = Corpus(original, expected_sha=sha)
    monkeypatch.setenv("NORMATIVE_SQLITE_CACHE_DIR", str(tmp_path / "native"))
    cached = Corpus(original, expected_sha=sha)
    assert cached.root == before.root and cached.identity == before.identity
    assert cached.path != before.path and cached.counts == before.counts
    assert cached.entity("norm", "n000") == before.entity("norm", "n000")
