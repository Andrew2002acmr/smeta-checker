from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import normative_saved_corpus as module


def active():
    return SimpleNamespace(
        root=Path("/knowledge"),
        identity=dict(
            database="releases/current/knowledge.sqlite",
            sha256="a" * 64,
            release_id="current",
            profile="roads",
        ),
    )


def test_historical_replay_checks_sha_and_retains_release_instead_of_relabelling(
    monkeypatch,
):
    module._load.cache_clear()
    pin = dict(
        database="releases/old/knowledge.sqlite",
        sha256="b" * 64,
        release_id="old",
        profile="roads",
    )
    calls = []

    def constructor(root, relative, expected_sha):
        calls.append((root, relative, expected_sha))
        return SimpleNamespace(identity=deepcopy(pin))

    monkeypatch.setattr(module, "Corpus", constructor)
    result = module.review_corpus(active(), dict(kb=pin))
    assert result.identity == pin
    assert calls == [(Path("/knowledge"), pin["database"], pin["sha256"])]
    assert module.review_corpus(active(), dict(kb=pin)) is result
    current = active()
    assert module.review_corpus(current, dict(kb=current.identity)) is current
    module._load.cache_clear()


@pytest.mark.parametrize(
    "change",
    [
        {"database": "../secret"},
        {"sha256": "bad"},
        {"profile": "electrical"},
        {"release_id": "../old"},
    ],
)
def test_invalid_or_cross_profile_pin_is_rejected(change):
    pin = dict(
        database="releases/old/knowledge.sqlite",
        sha256="b" * 64,
        release_id="old",
        profile="roads",
    )
    pin.update(change)
    with pytest.raises(ValueError):
        module.review_corpus(active(), dict(kb=pin))


def test_missing_changed_or_reidentified_release_never_uses_active_fallback(
    monkeypatch,
):
    module._load.cache_clear()
    pin = dict(
        database="releases/old/knowledge.sqlite",
        sha256="b" * 64,
        release_id="old",
        profile="roads",
    )

    def missing(*args, **kwargs):
        raise FileNotFoundError("historical release absent")

    monkeypatch.setattr(module, "Corpus", missing)
    with pytest.raises(FileNotFoundError):
        module.review_corpus(active(), dict(kb=pin))
    monkeypatch.setattr(module, "Corpus", lambda *args, **kwargs: active())
    with pytest.raises(ValueError):
        module.review_corpus(active(), dict(kb=pin))
    module._load.cache_clear()
