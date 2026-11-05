"""Source hashes survive both the corpus manager and its HTTP response."""

import json
import sqlite3

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import normative_service
from normative_api import register_normative_routes
from normative_corpus import Corpus
from normative_workbench import manager_profile


ROAD_HASH = "6aa5203d913dd5eb1538a4c23577e90d8ce95fa741547058429c31d74f9cb8a5"
ELECTRICAL_HASH = "e3c09a64a7000000000000000000000000000000000000000000000000000000"


@pytest.fixture(params=["", None], ids=["empty-hash", "nullable-hash"])
def source_corpora(request, tmp_path, monkeypatch):
    # The shipped schema requires a SHA string; some isolated corpus schemas
    # permit NULL. Exercise both without accessing an installed corpus.
    missing_hash = request.param
    corpora = {}
    for profile, digest in (("roads", ROAD_HASH), ("electrical", ELECTRICAL_HASH)):
        root = tmp_path / profile
        root.mkdir()
        path = root / "knowledge.sqlite"
        sha_constraint = "NOT NULL" if missing_hash == "" else ""
        with sqlite3.connect(path) as db:
            db.executescript(f"""
                CREATE TABLE metadata(key TEXT, value TEXT);
                CREATE TABLE sources(
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, designation TEXT NOT NULL,
                    kind TEXT NOT NULL, role TEXT NOT NULL, title TEXT NOT NULL,
                    sha256 TEXT {sha_constraint}, blob_path TEXT NOT NULL,
                    pages INTEGER, metadata_json TEXT NOT NULL
                );
                CREATE TABLE pages(source_id TEXT, clean_text TEXT);
                CREATE TABLE review_queue(id TEXT, source_id TEXT, entity_id TEXT,
                                          reason TEXT, details_json TEXT);
                CREATE TABLE gaps(designation TEXT);
                CREATE TABLE clauses(id TEXT);
                CREATE TABLE norm_tables(id TEXT);
                CREATE TABLE norms(id TEXT);
                CREATE TABLE resources(id TEXT);
                CREATE TABLE rules(id TEXT);
                CREATE TABLE relations(id TEXT);
                CREATE TABLE chunks(id TEXT);
            """)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("release_id", json.dumps("fixture-release")),
                ("profile", json.dumps(profile)),
            ])
            # The blob name deliberately differs from the source SHA: the
            # exposed identity must be the source field, not an inferred path.
            db.executemany("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("known", "known", "СП 1", "SP", "core", "СП 1", digest,
                 "blobs/different-name.pdf", 2, "{}"),
                ("missing", "missing", "ГОСТ 2", "GOST", "core", "ГОСТ 2", missing_hash,
                 "blobs/no-hash.pdf", 1, "{}"),
            ])
        (root / "CURRENT.json").write_text(json.dumps({"database": path.name}), encoding="utf-8")
        corpora[profile] = Corpus(root)

    monkeypatch.setattr(normative_service, "corpus", lambda *, profile: corpora[profile])
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.setenv("NORMATIVE_CATALOG_ROOT", str(catalog))
    return missing_hash


@pytest.mark.parametrize(("profile", "digest"), [
    ("roads", ROAD_HASH),
    ("electrical", ELECTRICAL_HASH),
])
def test_manager_profile_returns_source_sha256(source_corpora, profile, digest):
    sources = {source["id"]: source for source in manager_profile(profile)["sources"]}
    assert sources["known"]["sha256"] == digest
    assert sources["missing"]["sha256"] == source_corpora


def test_manager_api_serializes_source_sha256(source_corpora):
    app = FastAPI()
    router = APIRouter()
    register_normative_routes(router, lambda: object(), lambda *_: {})
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/v1/normative/manager")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile_errors"] == []
    profiles = {profile["profile"]: profile for profile in body["profiles"]}
    assert set(profiles) == {"roads", "electrical"}
    for profile, digest in (("roads", ROAD_HASH), ("electrical", ELECTRICAL_HASH)):
        sources = {source["id"]: source for source in profiles[profile]["sources"]}
        assert sources["known"]["sha256"] == digest
        assert sources["missing"]["sha256"] == source_corpora
