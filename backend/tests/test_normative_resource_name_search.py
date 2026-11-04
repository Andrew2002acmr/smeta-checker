"""A material name must reach its owning norms without scoring by VOR amount."""

import sqlite3

import pytest

from normative_corpus import Corpus
from test_normative_corpus_cache import database


@pytest.fixture
def materials(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE resources SET name='Семена газонных трав' WHERE id IN ('r83','r84')"
        )
        db.execute(
            "UPDATE resources SET name='Семена многолетних трав' WHERE id='r82'"
        )
    return Corpus(tmp_path)


def test_resource_name_reaches_norm_outside_work_title_fts_pool(materials):
    hits = materials.search("Семена газонных трав (смесь)", limit=3)
    assert [h["id"] for h in hits] == ["n083", "n084"]
    assert all(
        not h["exact_code"] and not h["automatic_application_allowed"] for h in hits
    )
    assert all(h["resources"][0]["name"] == "Семена газонных трав" for h in hits)
    assert all(h["checks"] and h["missing_facts"] for h in hits)


def test_expected_resource_amount_and_unit_do_not_select_the_norm(materials):
    first = materials.search("Семена газонных трав 2 кг/100м2", limit=3)
    second = materials.search("Семена газонных трав 999999 кг/999м2", limit=3)
    assert [h["id"] for h in first] == [h["id"] for h in second] == ["n083", "n084"]
    assert materials._cached_resource_names.cache_info().misses == 1


def test_resource_does_not_cross_explicit_collection_scope(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE resources SET name='Семена газонных трав' WHERE id='r84'")
        db.execute("UPDATE norms SET code='47-01-047-01' WHERE id='n084'")
    assert Corpus(tmp_path).search("Семена газонных трав", limit=3) == []


def test_resource_name_does_not_replace_explicit_norm_or_document_search(materials):
    exact = materials.search("27-01-001-01 Семена газонных трав", limit=1)
    assert exact[0]["id"] == "n001" and exact[0]["exact_code"]
    assert materials.search("Семена газонных трав", kind="ALL") == []
    assert materials.search("смесь материал ресурс", limit=3) == []
