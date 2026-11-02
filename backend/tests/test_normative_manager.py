"""The normative manager returns usable corpora and catalog on partial failures."""

from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace
import sqlite3

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest

import normative_service
import normative_workbench
from normative_api import register_normative_routes


@pytest.fixture
def manager_client(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    pdf_bytes = b"%PDF-1.4\nfixture catalog document\n%%EOF\n"
    (catalog / "fixture.pdf").write_bytes(pdf_bytes)
    monkeypatch.setenv("NORMATIVE_CATALOG_ROOT", str(catalog))

    # The database seam has only the source rows needed by the manager route;
    # no real corpus, installation paths, or network services are required.
    digest = sha256(pdf_bytes).hexdigest()
    root = tmp_path / "corpus"
    root.mkdir()

    class Corpus:
        def __init__(self, profile):
            self.root = root
            self.profile = profile

        @contextmanager
        def connect(self):
            rows = [{"blob_path": f"blobs/{digest}.pdf"}] if self.profile == "electrical" else []
            yield SimpleNamespace(execute=lambda _query: rows)

    failures = {}
    secret = "/private/knowledge/token-secret.sqlite"

    def corpus(*, profile):
        if failures.get(profile) == "corpus":
            raise OSError(secret)
        return Corpus(profile)

    def manager_profile(profile):
        if failures.get(profile) == "profile":
            raise sqlite3.OperationalError(secret)
        return {
            "profile": profile,
            "title": f"fixture {profile}",
            "identity": {},
            "counts": {},
            "releases": [],
            "sources": [],
            "gaps": [],
            "review_queue": [],
            "review_queue_total": 0,
        }

    monkeypatch.setattr(normative_service, "corpus", corpus)
    monkeypatch.setattr(normative_workbench, "manager_profile", manager_profile)
    app = FastAPI()
    router = APIRouter()
    register_normative_routes(router, lambda: object(), lambda *_: {})
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, failures=failures, secret=secret, pdf=pdf_bytes)


@pytest.mark.parametrize(
    ("failures", "available", "imported"),
    [
        ({}, {"electrical", "roads"}, True),
        ({"electrical": "corpus"}, {"roads"}, False),
        ({"roads": "corpus"}, {"electrical"}, True),
        ({"electrical": "corpus", "roads": "corpus"}, set(), False),
        ({"electrical": "profile"}, {"roads"}, False),
        ({"roads": "profile"}, {"electrical"}, True),
    ],
)
def test_manager_preserves_available_profiles_and_catalog(manager_client, failures, available, imported):
    manager_client.failures.update(failures)
    response = manager_client.client.get("/api/v1/normative/manager")

    assert response.status_code == 200, response.text
    body = response.json()
    assert {profile["profile"] for profile in body["profiles"]} == available
    assert len(body["profiles"]) == len(available)
    errors = body["profile_errors"]
    assert {error["profile"] for error in errors} == set(failures)
    assert len(errors) == len(failures)
    for error in errors:
        assert set(error) == {"profile", "message"}
        assert isinstance(error["message"], str) and error["message"].strip()
        assert manager_client.secret not in error["message"]
        assert "token-secret" not in error["message"]

    assert len(body["collections"]) == 1
    collection = body["collections"][0]
    assert collection["file_count"] == 1
    catalog_file = collection["sections"][0]["files"][0]
    assert catalog_file["id"] == "fixture.pdf"
    assert catalog_file["sha256"] == sha256(manager_client.pdf).hexdigest()
    assert catalog_file["imported"] is imported

    pdf = manager_client.client.get(
        "/api/v1/normative/catalog/pdf", params={"path": catalog_file["id"]}
    )
    assert pdf.status_code == 200
    assert pdf.content == manager_client.pdf
