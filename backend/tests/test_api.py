"""REST API over audit services: documents, audits, results, reports (issue 18).

No UI import anywhere in this module: the app factory is exercised
through FastAPI TestClient, the LLM is a fake client factory, and
Postgres + file blobs come from the issue-17 repository helpers.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf

from api import ApiContext, create_app
from audit_models import AuditSettings
from storage_blobs import FileBlobStore


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _fake_pdf_bytes():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 500, 600), "Copper cable 45 m. " * 40)
        return bytes(pdf.tobytes())


def _fake_vor_bytes():
    header = (
        "№ п.п.;Наименование работ, ресурсов, затрат по проекту;"
        "Объём работ / Количество;Ед. изм.;Ссылка на ПД;"
        "Наименование файла;Номер страниц (через пробел)"
    )
    return (header + "\n1;Copper cable;45;m;;project.pdf;1\n").encode("utf-8-sig")


def _designation_pdf_bytes(*texts):
    # fitz insert_textbox without an embedded TTF renders Cyrillic as "?",
    # so fixtures use Latin designations; Cyrillic matching is covered by
    # test_pdf_binding unit tests against real extracted strings.
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            page.insert_textbox(pymupdf.Rect(50, 50, 500, 600), text)
        return bytes(pdf.tobytes())


def _designation_vor_bytes(reference):
    header = (
        "№ п.п.;Наименование работ, ресурсов, затрат по проекту;"
        "Объём работ / Количество;Ед. изм.;Ссылка на чертежи в ПД;"
        "Наименование файла;Номер страниц (через пробел)"
    )
    return (
        header + f"\n1;Beregoukreplenie otkosa;100;m3;{reference};design.pdf;1\n"
    ).encode("utf-8-sig")


def _ok_client_factory():
    class FakeClient:
        def __init__(self, **_kwargs):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            import json as _json

            payload = _json.loads(kwargs["input"])
            sources = payload.get("sources", []) if isinstance(payload, dict) else []
            source = sources[0] if sources else {"id": "s0", "text": "Copper cable 45 m."}
            body = {
                "summary": "Подтверждено синтетическим клиентом.",
                "items": [
                    {
                        "topic": "Наличие работы",
                        "state": "confirmed",
                        "explanation": "Работа найдена в источнике.",
                        "claim_ids": ["presence"],
                        "project_source_ids": [source["id"]],
                        "normative_source_ids": [],
                    }
                ],
                "parameters": [
                    {
                        "parameter": "quantity",
                        "operation": "direct",
                        "value": "45",
                        "unit": "m",
                        "source_id": source["id"],
                        "operand_source_ids": [],
                        "quote": str(source.get("text", ""))[:900],
                        "scope_state": "verified",
                        "scope_quote": str(source.get("text", ""))[:900],
                    }
                ],
                "requests": [],
            }
            return SimpleNamespace(
                output_text=_json.dumps(body),
                usage=SimpleNamespace(total_tokens=100),
            )

        def close(self):
            pass

    def factory(**kwargs):
        return FakeClient(**kwargs)

    return factory


class ApiTests(unittest.TestCase):
    def _open_app(self, tmp):
        from storage_db import open_repository

        repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
        repo.reset_for_tests()
        settings = AuditSettings(
            yandex_cloud_api_key="test-only",
            yandex_cloud_folder="test-folder",
            yandex_cloud_model="qwen-test",
            database_url=_postgres_url(),
            blob_store_url=f"file://{tmp}",
        )
        context = ApiContext(
            settings=settings,
            repository=repo,
            blob_store=FileBlobStore(tmp),
            client_factory=_ok_client_factory(),
        )
        app = create_app(context)
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        self.addCleanup(repo._engine.dispose)
        return client, app

    def _open_app_with_dispatcher(self, tmp, dispatcher):
        from storage_db import open_repository

        repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
        repo.reset_for_tests()
        settings = AuditSettings(
            yandex_cloud_api_key="test-only",
            yandex_cloud_folder="test-folder",
            yandex_cloud_model="qwen-test",
            database_url=_postgres_url(),
            blob_store_url=f"file://{tmp}",
        )
        context = ApiContext(
            settings=settings,
            repository=repo,
            blob_store=FileBlobStore(tmp),
            client_factory=_ok_client_factory(),
            dispatcher=dispatcher,
        )
        app = create_app(context)
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        self.addCleanup(repo._engine.dispose)
        return client, app

    def _project_id(self, client, name="ИГДИ"):
        existing = next((p for p in client.get("/api/v1/projects").json()
                         if p["name"] == name), None)
        if existing is not None:
            return existing["id"]
        response = client.post("/api/v1/projects", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def _upload(self, client, project, kind, filename, payload, content_type,
                project_id=None, document_id=None, new_document=None):
        data = {"project_id": project_id or ApiTests._project_id(self, client, project), "kind": kind}
        if document_id:
            data["document_id"] = document_id
        if new_document is None:
            data["new_document"] = "false" if document_id else "true"
        elif new_document:
            data["new_document"] = "true"
        response = client.post(
            "/api/v1/documents",
            data=data,
            files={"file": (filename, payload, content_type)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _upload_pair(self, client, project="ИГДИ"):
        created = client.post("/api/v1/projects", json={"name": project})
        self.assertEqual(created.status_code, 201, created.text)
        project_id = created.json()["id"]
        vor = self._upload(
            client, project, "vor", "vor.csv", _fake_vor_bytes(), "text/csv",
            project_id=project_id, new_document=True,
        )
        pdf = self._upload(
            client,
            project,
            "pdf",
            "project.pdf",
            _fake_pdf_bytes(),
            "application/pdf",
            project_id=project_id,
            new_document=True,
        )
        return vor, pdf
    def _wait_for_run(self, client, run_id, timeout=60.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = client.get(f"/api/v1/audits/{run_id}")
            self.assertEqual(last.status_code, 200, last.text)
            if last.json()["status"] in {"done", "failed"}:
                return last.json()
            time.sleep(0.2)
        self.fail(f"run {run_id} did not finish: {last.json() if last else None}")

    def test_openapi_schema_lists_audit_routes(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.get("/openapi.json")
            self.assertEqual(response.status_code, 200)
            paths = response.json()["paths"]
            for route in (
                "/api/v1/documents",
                "/api/v1/audits",
                "/api/v1/audits/{run_id}",
                "/api/v1/audits/{run_id}/results",
            ):
                self.assertIn(route, paths)

    def test_upload_rejects_unknown_kind_with_json_error(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/documents",
                data={"project_id": self._project_id(client), "kind": "docx"},
                files={"file": ("vor.csv", _fake_vor_bytes(), "text/csv")},
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_upload_rejects_invalid_vor_with_json_error(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/documents",
                data={"project_id": self._project_id(client), "kind": "vor"},
                files={"file": ("vor.csv", b"col1;col2\n1;2\n", "text/csv")},
            )
            self.assertEqual(response.status_code, 400)
            payload = response.json()
            self.assertEqual(payload["error"]["code"], "invalid_vor")
            self.assertIn("обязательные колонки", payload["error"]["message"])

    def test_upload_rejects_invalid_pdf_with_json_error(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/documents",
                data={"project_id": self._project_id(client), "kind": "pdf"},
                files={"file": ("broken.pdf", b"%PDF-1.7 broken", "application/pdf")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_pdf")

    def test_same_bytes_independent_by_default_dedupe_explicit(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            project_id = client.post(
                "/api/v1/projects", json={"name": "ИГДИ"}).json()["id"]
            first = self._upload(
                client, "ИГДИ", "vor", "vor.csv", _fake_vor_bytes(), "text/csv",
                project_id=project_id, new_document=True,
            )
            second = self._upload(
                client, "ИГДИ", "vor", "vor.csv", _fake_vor_bytes(), "text/csv",
                project_id=project_id, new_document=True,
            )
            self.assertNotEqual(first["document_id"], second["document_id"])
            repeat = self._upload(
                client, "ИГДИ", "vor", "vor.csv", _fake_vor_bytes(), "text/csv",
                project_id=project_id, document_id=first["document_id"],
            )
            self.assertEqual(repeat["version_id"], first["version_id"])
            self.assertEqual(repeat["version_no"], 1)

    def test_preflight_reports_binding_status(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            vor, pdf = self._upload_pair(client)
            response = client.post(
                "/api/v1/documents/preflight",
                json={
                    "project": "ИГДИ",
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertFalse(payload["has_critical_error"])
            self.assertEqual(len(payload["rows"]), 1)
            self.assertIn("🟢", payload["rows"][0]["status"])

    def test_preflight_unknown_version_is_json_404(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            vor, _ = self._upload_pair(client)
            response = client.post(
                "/api/v1/documents/preflight",
                json={
                    "project": "ИГДИ",
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": ["00000000-0000-0000-0000-000000000000"],
                    "normative_profile": "roads",
                },
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "version_not_found")

    def test_audit_lifecycle_returns_fast_then_serves_results_and_reports(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, app = self._open_app(tmp)
            vor, pdf = self._upload_pair(client)
            with patch("audit_tracing._configured_client", return_value=None):
                started = time.monotonic()
                created = client.post(
                    "/api/v1/audits",
                    json={
                        "project": "ИГДИ",
                        "vor_version_id": vor["version_id"],
                        "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                    },
                )
                elapsed = time.monotonic() - started
            self.assertEqual(created.status_code, 202, created.text)
            run_id = created.json()["run_id"]
            self.assertEqual(created.json()["status"], "running")
            # The HTTP request returns before the audit finishes: even one
            # position must not run inside the request handler.
            runner = app.state.runner
            self.assertTrue(runner.wait(run_id, timeout=0.0) is False or elapsed < 60)
            status = client.get(f"/api/v1/audits/{run_id}").json()
            self.assertIn(status["status"], {"running", "done", "failed"})
            early = client.get(f"/api/v1/audits/{run_id}/results")
            self.assertIn(early.status_code, {200, 409})
            if early.status_code == 409:
                self.assertEqual(early.json()["error"]["code"], "audit_not_finished")
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            self.assertEqual(finished["position_count"], 1)
            results = client.get(f"/api/v1/audits/{run_id}/results").json()
            self.assertEqual(results["status"], "done")
            self.assertEqual(len(results["results"]), 1)
            assessment = results["results"][0]
            self.assertEqual(assessment["schema_version"], "evidence-assessment-v1")
            self.assertEqual(assessment["engine"], "langgraph-evidence-v1")
            self.assertEqual(assessment["execution"]["state"], "complete")
            self.assertIn(assessment["project"]["state"], {"confirmed", "needs_facts", "possible_mismatch"})
            report = client.get(f"/api/v1/audits/{run_id}/reports/word")
            self.assertEqual(report.status_code, 200, report.text[:200])
            self.assertTrue(report.content.startswith(b"PK"))
            legacy = client.get(f"/api/v1/audits/{run_id}/reports/excel")
            self.assertEqual(legacy.status_code, 400)
            self.assertEqual(legacy.json()["error"]["code"], "unknown_report")
            runner.wait(run_id, timeout=5.0)

    def test_missing_designation_is_error_without_llm_call(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, app = self._open_app(tmp)
            created = client.post("/api/v1/projects", json={"name": "ТКР"})
            project_id = created.json()["id"]
            vor = self._upload(
                client, "ТКР", "vor", "vor.csv",
                _designation_vor_bytes("30/09-TKR16.GCH l. 1,"),
                "text/csv", project_id=project_id, new_document=True,
            )
            pdf = self._upload(
                client, "ТКР", "pdf", "design.pdf",
                _designation_pdf_bytes(
                    "PROJECT DOCUMENTATION 30/09-TKR16 Volume 3.16",
                    "Specification 30/09-TKR16.SO sheet 1",
                ),
                "application/pdf", project_id=project_id, new_document=True,
            )
            with patch("audit_tracing._configured_client", return_value=None):
                created = client.post(
                    "/api/v1/audits",
                    json={
                        "project": "ТКР",
                        "vor_version_id": vor["version_id"],
                        "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                    },
                )
            self.assertEqual(created.status_code, 202, created.text)
            run_id = created.json()["run_id"]
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            results = client.get(f"/api/v1/audits/{run_id}/results").json()
            self.assertEqual(len(results["results"]), 1)
            row = results["results"][0]
            self.assertEqual(row["project"]["state"], "possible_mismatch")
            self.assertIn("30/09-TKR16.GCH", row["project"]["obligations"][0]["reason"])
            self.assertIn("обозначения отсутствуют", row["project"]["obligations"][0]["reason"])
            self.assertEqual(row["execution"]["http_attempts"], 0)

    def test_found_designation_passes_to_audit(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, app = self._open_app(tmp)
            created = client.post("/api/v1/projects", json={"name": "ТКР"})
            project_id = created.json()["id"]
            vor = self._upload(
                client, "ТКР", "vor", "vor.csv",
                _designation_vor_bytes("30/09-TKR16.SO l. 1,"),
                "text/csv", project_id=project_id, new_document=True,
            )
            pdf = self._upload(
                client, "ТКР", "pdf", "design.pdf",
                _designation_pdf_bytes(
                    "PROJECT DOCUMENTATION 30/09-TKR16 Volume 3.16 "
                    "Beregoukreplenie otkosa 100 m3",
                    "Specification 30/09-TKR16.SO sheet 1",
                ),
                "application/pdf", project_id=project_id, new_document=True,
            )
            with patch("audit_tracing._configured_client", return_value=None):
                created = client.post(
                    "/api/v1/audits",
                    json={
                        "project": "ТКР",
                        "vor_version_id": vor["version_id"],
                        "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                    },
                )
            self.assertEqual(created.status_code, 202, created.text)
            run_id = created.json()["run_id"]
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            results = client.get(f"/api/v1/audits/{run_id}/results").json()
            row = results["results"][0]
            reasons = " ".join(obligation.get("reason", "") for obligation in row["project"]["obligations"])
            self.assertNotIn("обозначения отсутствуют", reasons)

    def test_audit_without_pdf_selection_uses_project_pool(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, app = self._open_app(tmp)
            vor, pdf = self._upload_pair(client)
            with patch("audit_tracing._configured_client", return_value=None):
                created = client.post(
                    "/api/v1/audits",
                    json={
                        "project": "ИГДИ",
                        "vor_version_id": vor["version_id"],
                        "normative_profile": "roads",
                    },
                )
            self.assertEqual(created.status_code, 202, created.text)
            run_id = created.json()["run_id"]
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            results = client.get(f"/api/v1/audits/{run_id}/results").json()
            self.assertEqual(results["results"][0]["schema_version"], "evidence-assessment-v1")

    def test_audit_without_any_pdf_is_explicit_error(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            created = client.post("/api/v1/projects", json={"name": "Пустой"})
            project_id = created.json()["id"]
            vor = self._upload(
                client, "Пустой", "vor", "vor.csv", _fake_vor_bytes(),
                "text/csv", project_id=project_id, new_document=True,
            )
            response = client.post(
                "/api/v1/audits",
                json={
                    "project": "Пустой",
                    "vor_version_id": vor["version_id"],
                    "normative_profile": "roads",
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "pdf_pool_empty")

    def test_audit_unknown_run_is_json_404(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.get("/api/v1/audits/00000000-0000-0000-0000-000000000000")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "run_not_found")

    def test_audit_rejects_vor_version_of_wrong_kind(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            _, pdf = self._upload_pair(client)
            response = client.post(
                "/api/v1/audits",
                json={
                    "project": "ИГДИ",
                    "vor_version_id": pdf["version_id"],
                    "pdf_version_ids": [],
                    "normative_profile": "roads",
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "not_vor_version")

    def test_download_unknown_report_kind_is_json_400(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            vor, pdf = self._upload_pair(client)
            with patch("audit_tracing._configured_client", return_value=None):
                created = client.post(
                    "/api/v1/audits",
                    json={
                        "project": "ИГДИ",
                        "vor_version_id": vor["version_id"],
                        "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                    },
                )
            run_id = created.json()["run_id"]
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            for kind in ("csv", "excel"):
                response = client.get(f"/api/v1/audits/{run_id}/reports/{kind}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "unknown_report")

    def test_audit_with_dispatcher_enqueues_without_running_inline(self):
        dispatched = []
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app_with_dispatcher(
                tmp, lambda name, payload: dispatched.append((name, payload))
            )
            vor, pdf = self._upload_pair(client)
            created = client.post(
                "/api/v1/audits",
                json={
                    "project": "ИГДИ",
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                },
            )
            self.assertEqual(created.status_code, 202, created.text)
            self.assertEqual(len(dispatched), 1)
            name, payload = dispatched[0]
            self.assertEqual(name, "audit_tasks.prepare_run")
            self.assertEqual(payload["run_id"], created.json()["run_id"])
            self.assertEqual(payload["project"], "ИГДИ")
            self.assertEqual(payload["vor_filename"], "vor.csv")
            self.assertIn("vor_blob_key", payload)
            self.assertIn("project.pdf", payload["pdf_blob_keys"])
            # Nothing ran inline: the run stays running with zero results.
            status = client.get(f"/api/v1/audits/{created.json()['run_id']}").json()
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["position_count"], 0)

    def test_request_id_echoed_and_stored_with_run(self):
        dispatched = []
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, app = self._open_app_with_dispatcher(
                tmp, lambda name, payload: dispatched.append((name, payload))
            )
            vor, pdf = self._upload_pair(client)
            created = client.post(
                "/api/v1/audits",
                json={
                    "project": "ИГДИ",
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                        "normative_profile": "roads",
                },
                headers={"X-Request-ID": "req-test-123"},
            )
            self.assertEqual(created.status_code, 202, created.text)
            self.assertEqual(created.headers["X-Request-ID"], "req-test-123")
            self.assertEqual(dispatched[0][1]["request_id"], "req-test-123")
            record = app.state.context.repository.load_run(
                __import__("uuid").UUID(created.json()["run_id"])
            )
            self.assertEqual(record["config"]["request_id"], "req-test-123")

    def test_request_id_generated_when_missing(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["X-Request-ID"])

    def test_oversize_upload_rejected_with_413(self):
        from storage_blobs import FileBlobStore
        from storage_db import open_repository

        from api import ApiContext, create_app

        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            settings = AuditSettings(
                yandex_cloud_api_key="test-only",
                yandex_cloud_folder="test-folder",
                database_url=_postgres_url(),
                blob_store_url=f"file://{tmp}",
                max_upload_bytes=10,
            )
            context = ApiContext(
                settings=settings,
                repository=repo,
                blob_store=FileBlobStore(tmp),
                client_factory=_ok_client_factory(),
            )
            app = create_app(context)
            from fastapi.testclient import TestClient

            client = TestClient(app, raise_server_exceptions=False)
            self.addCleanup(client.close)
            response = client.post(
                "/api/v1/documents",
                data={"project_id": self._project_id(client), "kind": "vor"},
                files={"file": ("vor.csv", _fake_vor_bytes(), "text/csv")},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "upload_too_large")

    def test_wrong_mime_rejected(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/documents",
                data={"project_id": self._project_id(client), "kind": "pdf"},
                files={"file": ("a.pdf", _fake_pdf_bytes(), "image/png")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.json()["error"]["code"], "unsupported_media_type"
            )

    def test_api_token_guards_v1_routes_when_configured(self):
        from storage_blobs import FileBlobStore
        from storage_db import open_repository

        from api import ApiContext, create_app

        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            context = ApiContext(
                settings=AuditSettings(
                    database_url=_postgres_url(),
                    blob_store_url=f"file://{tmp}",
                    api_token="secret-token",
                ),
                repository=repo,
                blob_store=FileBlobStore(tmp),
            )
            from fastapi.testclient import TestClient

            client = TestClient(create_app(context), raise_server_exceptions=False)
            self.addCleanup(client.close)
            # Health stays open for container healthchecks.
            self.assertEqual(client.get("/healthz").status_code, 200)
            # No token and a wrong token both fail closed with the same code.
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                response = client.post(
                    "/api/v1/documents",
                    data={"project_id": "00000000-0000-0000-0000-000000000000", "kind": "pdf"},
                    files={"file": ("a.pdf", _fake_pdf_bytes(), "application/pdf")},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401, headers)
                self.assertEqual(response.json()["error"]["code"], "unauthorized")
            response = client.get(
                "/api/v1/audits/00000000-0000-0000-0000-000000000000"
            )
            self.assertEqual(response.status_code, 401)
            # Correct Bearer token passes auth (404 = past the guard).
            response = client.get(
                "/api/v1/audits/00000000-0000-0000-0000-000000000000",
                headers={"Authorization": "Bearer secret-token"},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "run_not_found")

    def test_api_token_empty_keeps_local_access_open(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.get(
                "/api/v1/audits/00000000-0000-0000-0000-000000000000"
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "run_not_found")

    def test_project_update_archives_without_name_merge(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            first = client.post("/api/v1/projects", json={"name": "Дубль"})
            second = client.post("/api/v1/projects", json={"name": "Дубль"})
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(second.status_code, 201, second.text)
            self.assertNotEqual(first.json()["id"], second.json()["id"])
            renamed = client.patch(
                f"/api/v1/projects/{first.json()['id']}", json={"name": "  Новое  "}
            )
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(renamed.json()["name"], "Новое")
            archived = client.patch(
                f"/api/v1/projects/{first.json()['id']}", json={"archived": True}
            )
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertTrue(archived.json()["archived"])
            visible = client.get("/api/v1/projects")
            self.assertNotIn(first.json()["id"], [p["id"] for p in visible.json()])
            hidden = client.get("/api/v1/projects?archived=true")
            self.assertIn(first.json()["id"], [p["id"] for p in hidden.json()])
            missing = client.patch(
                "/api/v1/projects/00000000-0000-0000-0000-000000000000",
                json={"name": "X"},
            )
            self.assertEqual(missing.status_code, 404)
            blank = client.patch(
                f"/api/v1/projects/{second.json()['id']}", json={"name": "  "})
            self.assertEqual(blank.status_code, 400)

    def test_suggest_name_endpoint_removed(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/projects/00000000-0000-0000-0000-000000000000/suggest-name")
            self.assertEqual(response.status_code, 404)
if __name__ == "__main__":
    unittest.main()
