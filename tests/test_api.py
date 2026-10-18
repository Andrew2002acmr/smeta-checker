"""REST API over audit services: documents, audits, results, reports (issue 18).

No Streamlit import anywhere in this module: the app factory is exercised
through FastAPI TestClient, the LLM is a fake client factory, and
Postgres + file blobs come from the issue-17 repository helpers.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz

from api import ApiContext, create_app
from audit_models import AuditSettings
from storage_blobs import FileBlobStore


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _fake_pdf_bytes():
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 500, 600), "Copper cable 45 m. " * 40)
        return bytes(pdf.tobytes())


def _fake_vor_bytes():
    header = (
        "№ п.п.;Наименование работ, ресурсов, затрат по проекту;"
        "Объём работ / Количество;Ед. изм.;Ссылка на ПД;"
        "Наименование файла;Номер страниц (через пробел)"
    )
    return (header + "\n1;Copper cable;45;m;;project.pdf;1\n").encode("utf-8-sig")


def _ok_client_factory():
    class FakeClient:
        def __init__(self, **_kwargs):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **_kwargs):
            body = {
                "work_found": True,
                "volume_confirmed": True,
                "unit_confirmed": True,
                "comment_confirmed": True,
                "found_volume": "45 m",
                "found_unit": "m",
                "found_page": "1",
                "evidence": [
                    {"source_id": "p1q1", "kind": "work"},
                    {"source_id": "p1q1", "kind": "volume"},
                    {"source_id": "p1q1", "kind": "unit"},
                ],
                "discrepancy_details": "Confirmed",
            }
            return SimpleNamespace(
                output_text=json.dumps(body),
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
        return client, app

    def _upload(self, client, project, kind, filename, payload, content_type):
        response = client.post(
            "/api/v1/documents",
            data={"project": project, "kind": kind},
            files={"file": (filename, payload, content_type)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _upload_pair(self, client, project="ИГДИ"):
        vor = self._upload(
            client, project, "vor", "vor.csv", _fake_vor_bytes(), "text/csv"
        )
        pdf = self._upload(
            client,
            project,
            "pdf",
            "project.pdf",
            _fake_pdf_bytes(),
            "application/pdf",
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
                data={"project": "ИГДИ", "kind": "docx"},
                files={"file": ("vor.csv", _fake_vor_bytes(), "text/csv")},
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_upload_rejects_invalid_vor_with_json_error(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            response = client.post(
                "/api/v1/documents",
                data={"project": "ИГДИ", "kind": "vor"},
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
                data={"project": "ИГДИ", "kind": "pdf"},
                files={"file": ("broken.pdf", b"%PDF-1.7 broken", "application/pdf")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_pdf")

    def test_same_vor_bytes_reuse_version(self):
        with tempfile.TemporaryDirectory(prefix="smeta-api-") as tmp:
            client, _ = self._open_app(tmp)
            first = self._upload(
                client, "ИГДИ", "vor", "vor.csv", _fake_vor_bytes(), "text/csv"
            )
            second = self._upload(
                client, "ИГДИ", "vor", "vor.csv", _fake_vor_bytes(), "text/csv"
            )
            self.assertEqual(first["version_id"], second["version_id"])
            self.assertEqual(second["version_no"], 1)

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
            self.assertEqual(results["results"][0]["status"], "ОК")
            self.assertTrue(results["results"][0]["work_found"])
            self.assertEqual(
                results["results"][0]["pdf_sha256"],
                app.state.context.repository.load_version(
                    __import__("uuid").UUID(pdf["version_id"])
                ).sha256,
            )
            for kind, prefix in (("excel", b"PK"), ("word", b"PK")):
                report = client.get(f"/api/v1/audits/{run_id}/reports/{kind}")
                self.assertEqual(report.status_code, 200, report.text[:200])
                self.assertTrue(report.content.startswith(prefix))
            runner.wait(run_id, timeout=5.0)

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
                    },
                )
            run_id = created.json()["run_id"]
            finished = self._wait_for_run(client, run_id)
            self.assertEqual(finished["status"], "done")
            response = client.get(f"/api/v1/audits/{run_id}/reports/csv")
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

if __name__ == "__main__":
    unittest.main()
