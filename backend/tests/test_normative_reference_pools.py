"""Explicit base/adjustment references share the bounded retrieval result."""

import sqlite3

import pytest

from normative_corpus import Corpus
from test_normative_corpus_cache import database


@pytest.fixture
def references(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        for identity, code, table in [
            ("n001", "27-06-031-01", "27-06-031"),
            ("n002", "27-06-031-02", "27-06-031"),
            ("n003", "27-06-032-01", "27-06-032"),
            ("n004", "27-06-032-02", "27-06-032"),
        ]:
            db.execute(
                "UPDATE norms SET code=?,table_code=? WHERE id=?",
                (code, table, identity),
            )
    return Corpus(tmp_path)


@pytest.mark.parametrize(
    "query",
    [
        "27-06-031-01 и таблица 27-06-032",
        "27-06-031 + 27-06-032",
        "27–06–031–01 и 27–06–032",
    ],
)
def test_adjustment_table_is_not_starved_by_base_alternatives(references, query):
    hits = references.search(query, limit=2)
    assert [h["code"] for h in hits] == ["27-06-031-01", "27-06-032-01"]
    assert all(h["exact_code"] and not h["automatic_application_allowed"] for h in hits)


def test_each_explicit_full_code_retains_its_requested_order(references):
    hits = references.search(
        "27-06-032-02, 27-06-031-02", facts={"mass_kg_per_m": 50}, limit=2
    )
    assert [h["code"] for h in hits] == ["27-06-032-02", "27-06-031-02"]
    assert hits[0]["checks"]  # Retrieval priority does not waive applicability.


def test_table_reference_does_not_extract_a_prefix_inside_full_code(references):
    hits = references.search("27-06-031-02", limit=2)
    assert hits[0]["code"] == "27-06-031-02"
    assert not any(h["exact_code"] for h in hits[1:])


def test_explicit_reference_respects_configured_family_scope(references):
    assert references.search("47-01-047") == []
