"""Lifecycle boundary cases: documents, bindings, pins, archive (no AI)."""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf
import test_api as fixtures
from openpyxl import Workbook


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _vor_with_headers(meta_rows=(), number=1, quantity=45):
    book = Workbook()
    sheet = book.active
    sheet.title = "ВОР"
    for row in meta_rows:
        sheet.append(list(row))
    sheet.append(
        [
            "№ п.п.",
            "Наименование работ",
            "Объём работ / Количество",
            "Ед. изм.",
            "Ссылка на ПД",
            "Наименование файла",
            "Номер страниц (через пробел)",
        ]
    )
    sheet.append([number, "Copper cable", quantity, "m", "", "project.pdf", "1"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def _pdf_with_text(text="Шифр 123. Наименование объекта Тест."):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 500, 200), text)
        return bytes(pdf.tobytes())


class LifecycleTests(unittest.TestCase):
    _open_app = fixtures.ApiTests._open_app
    _upload = fixtures.ApiTests._upload
    _wait_for_run = fixtures.ApiTests._wait_for_run

    def _project(self, client, name="Проект"):
        created = client.post("/api/v1/projects", json={"name": name})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["id"]

    def test_same_filename_new_document_independent_and_dedup(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, _ = self._open_app(tmp)
            project_id = self._project(client)
            data = _vor_with_headers()
            first = self._upload(
                client,
                "Проект",
                "vor",
                "same.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                project_id=project_id,
                new_document=True,
            )
            second = self._upload(
                client,
                "Проект",
                "vor",
                "same.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                project_id=project_id,
                new_document=True,
            )
            self.assertNotEqual(first["document_id"], second["document_id"])
            catalog = client.get(f"/api/v1/projects/{project_id}/catalog")
            self.assertEqual(catalog.status_code, 200, catalog.text)
            self.assertEqual(len(catalog.json()), 2)
            # Explicit document append dedupes exact bytes.
            repeat = self._upload(
                client,
                "Проект",
                "vor",
                "same.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                project_id=project_id,
                document_id=first["document_id"],
            )
            self.assertEqual(repeat["version_id"], first["version_id"])

    def test_pdf_current_stays_until_explicit_patch(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, _ = self._open_app(tmp)
            project_id = self._project(client)
            first = self._upload(
                client,
                "Проект",
                "pdf",
                "a.pdf",
                _pdf_with_text("Шифр A"),
                "application/pdf",
                project_id=project_id,
                new_document=True,
            )
            doc_id = first["document_id"]
            second = self._upload(
                client,
                "Проект",
                "pdf",
                "a.pdf",
                _pdf_with_text("Шифр B"),
                "application/pdf",
                project_id=project_id,
                document_id=doc_id,
            )
            detail = client.get(f"/api/v1/documents/{doc_id}/detail").json()
            self.assertEqual(detail["current_version_id"], first["version_id"])
            patched = client.patch(
                f"/api/v1/documents/{doc_id}",
                json={"current_version_id": second["version_id"]},
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertEqual(patched.json()["current_version_id"], second["version_id"])

    def test_metadata_overrides_preserve_extraction(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, _ = self._open_app(tmp)
            project_id = self._project(client)
            meta = [("Наименование стройки", "Стройка А"), ("Шифр", "В-12")]
            uploaded = self._upload(
                client,
                "Проект",
                "vor",
                "meta.xlsx",
                _vor_with_headers(meta),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                project_id=project_id,
                new_document=True,
            )
            patched = client.patch(
                f"/api/v1/documents/{uploaded['version_id']}/metadata",
                json={"fields": {"document_code": "РУЧНОЙ"}},
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            body = patched.json()
            self.assertEqual(body["metadata_overrides"].get("document_code"), "РУЧНОЙ")
            self.assertEqual(body["metadata"].get("document_code"), "В-12")
            bad = client.patch(
                f"/api/v1/documents/{uploaded['version_id']}/metadata",
                json={"fields": {"unknown": "x"}},
            )
            self.assertEqual(bad.status_code, 400)

    def test_used_version_delete_rejected_and_run_pins_cross_project(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, app = self._open_app(tmp)
            project_id = self._project(client, "Первый")
            vor = self._upload(
                client,
                "Первый",
                "vor",
                "vor.csv",
                fixtures._fake_vor_bytes(),
                "text/csv",
                project_id=project_id,
                new_document=True,
            )
            pdf = self._upload(
                client,
                "Первый",
                "pdf",
                "project.pdf",
                fixtures._fake_pdf_bytes(),
                "application/pdf",
                project_id=project_id,
                new_document=True,
            )
            other_id = self._project(client, "Второй")
            other_pdf = self._upload(
                client,
                "Второй",
                "pdf",
                "other.pdf",
                fixtures._fake_pdf_bytes(),
                "application/pdf",
                project_id=other_id,
                new_document=True,
            )
            bad = client.post(
                "/api/v1/audits",
                json={
                    "project_id": project_id,
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [other_pdf["version_id"]],
                },
            )
            self.assertEqual(bad.status_code, 400)
            created = client.post(
                "/api/v1/audits",
                json={
                    "project_id": project_id,
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                },
            )
            self.assertEqual(created.status_code, 202, created.text)
            self._wait_for_run(client, created.json()["run_id"])
            for version_id in (vor["version_id"], pdf["version_id"]):
                denied = client.delete(f"/api/v1/documents/{version_id}/version")
                self.assertEqual(denied.status_code, 409, denied.text)
            # A new VOR revision verifies against current PDFs at audit
            # time; no binding is inherited or stored.
            child = self._upload(
                client,
                "Первый",
                "vor",
                "vor.csv",
                fixtures._fake_vor_bytes().replace(b";45;", b";46;"),
                "text/csv",
                project_id=project_id,
                document_id=vor["document_id"],
            )
            gone = client.get(f"/api/v1/documents/{child['version_id']}/bindings")
            self.assertEqual(gone.status_code, 404)

    def test_archive_restore_keeps_docs_archived_and_run_snapshot_stable(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, app = self._open_app(tmp)
            project_id = self._project(client)
            vor = self._upload(
                client,
                "Проект",
                "vor",
                "vor.csv",
                fixtures._fake_vor_bytes(),
                "text/csv",
                project_id=project_id,
                new_document=True,
            )
            pdf = self._upload(
                client,
                "Проект",
                "pdf",
                "project.pdf",
                fixtures._fake_pdf_bytes(),
                "application/pdf",
                project_id=project_id,
                new_document=True,
            )
            doc_id = vor["document_id"]
            self.assertEqual(
                client.get(f"/api/v1/projects/{project_id}/audits/count").json(),
                {"count": 0},
            )
            self.assertIsNone(
                client.get(f"/api/v1/documents/{doc_id}/detail").json()["run_freshness"]
            )
            created = client.post(
                "/api/v1/audits",
                json={
                    "project_id": project_id,
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                },
            )
            run_id = created.json()["run_id"]
            self._wait_for_run(client, run_id)
            self.assertEqual(
                client.get(f"/api/v1/projects/{project_id}/audits/count").json(),
                {"count": 1},
            )
            initial = client.get(f"/api/v1/documents/{doc_id}/detail").json()
            self.assertEqual(initial["run_freshness"], "current")
            self.assertFalse(initial["vor_updated"])
            self.assertFalse(initial["pd_updated"])
            from unittest.mock import patch

            with patch.object(app.state.context.repository, "run_progress_records", return_value=[]):
                unavailable = client.get(f"/api/v1/documents/{doc_id}/detail")
                self.assertEqual(unavailable.status_code, 200, unavailable.text)
                self.assertIsNone(unavailable.json()["last_run"])
                self.assertEqual(unavailable.json()["run_freshness"], "unavailable")
            before_rows = client.get(f"/api/v1/audits/{run_id}/rows").json()
            before_evidence = client.get(
                f"/api/v1/audits/{run_id}/positions/0/workspace"
            ).json()["evidence"]
            # Merely uploading a PDF does not replace the selected basis.
            pdf2 = self._upload(
                client,
                "Проект",
                "pdf",
                "project.pdf",
                _pdf_with_text("Шифр обновлён"),
                "application/pdf",
                project_id=project_id,
                document_id=pdf["document_id"],
            )
            unchanged = client.get(f"/api/v1/projects/{project_id}/catalog").json()
            self.assertFalse(
                next(d for d in unchanged if d["id"] == doc_id)["pd_updated"]
            )
            client.patch(
                f"/api/v1/documents/{pdf['document_id']}",
                json={"current_version_id": pdf2["version_id"]},
            ).raise_for_status()
            catalog = client.get(f"/api/v1/projects/{project_id}/catalog").json()
            vor_doc = next(d for d in catalog if d["id"] == doc_id)
            self.assertTrue(vor_doc["pd_updated"])
            self.assertEqual(vor_doc["run_freshness"], "stale")
            self.assertFalse(vor_doc["vor_updated"])
            vor2 = self._upload(
                client, "Проект", "vor", "vor.csv",
                fixtures._fake_vor_bytes().replace(b";45;", b";46;"), "text/csv",
                project_id=project_id, document_id=doc_id,
            )
            client.patch(
                f"/api/v1/documents/{doc_id}",
                json={"current_version_id": vor2["version_id"]},
            ).raise_for_status()
            changed_vor = client.get(f"/api/v1/documents/{doc_id}/detail").json()
            self.assertTrue(changed_vor["vor_updated"])
            self.assertEqual(changed_vor["run_freshness"], "stale")
            self.assertEqual(
                client.get(f"/api/v1/audits/{run_id}/rows").json(), before_rows
            )
            self.assertEqual(
                client.get(f"/api/v1/audits/{run_id}/positions/0/workspace").json()[
                    "evidence"
                ],
                before_evidence,
            )
            from uuid import UUID

            rerun = app.state.context.repository.create_run(
                UUID(project_id), UUID(vor2["version_id"]),
                {"pdf_versions": {"project.pdf": pdf2["version_id"]}},
                pdf_pins={"project.pdf": UUID(pdf2["version_id"])},
            )
            fresh = client.get(f"/api/v1/documents/{doc_id}/detail").json()
            self.assertEqual(fresh["last_run"]["run_id"], str(rerun.id))
            self.assertEqual(fresh["run_freshness"], "current")
            self.assertFalse(fresh["vor_updated"])
            self.assertFalse(fresh["pd_updated"])
            self.assertEqual(
                client.get(f"/api/v1/projects/{project_id}/audits/count").json(),
                {"count": 2},
            )
            # Archived PDF revisions pin the snapshot: delete stays 409.
            denied = client.delete(f"/api/v1/documents/{pdf['version_id']}/version")
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(
                client.get(
                    f"/api/v1/documents/{pdf2['version_id']}/content"
                ).status_code,
                200,
            )
            client.patch(
                f"/api/v1/documents/{pdf['document_id']}", json={"archived": True}
            )
            self.assertEqual(
                client.get(f"/api/v1/documents/{doc_id}/detail").json()["run_freshness"],
                "unavailable",
            )
            restored = client.patch(
                f"/api/v1/projects/{project_id}", json={"archived": False}
            )
            self.assertEqual(restored.status_code, 200)
            hidden = client.get(f"/api/v1/projects/{project_id}/catalog").json()
            self.assertNotIn(pdf["document_id"], [d["id"] for d in hidden])
            visible = client.get(
                f"/api/v1/projects/{project_id}/catalog?archived=true"
            ).json()
            self.assertIn(pdf["document_id"], [d["id"] for d in visible])

    def test_run_count_is_unpaginated_and_unknown_snapshot_is_unavailable(self):
        from uuid import UUID
        from sqlalchemy.orm import Session
        from storage_models import Run

        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, app = self._open_app(tmp)
            project_id = self._project(client)
            vor = self._upload(
                client, "Проект", "vor", "vor.csv", fixtures._fake_vor_bytes(),
                "text/csv", project_id=project_id, new_document=True,
            )
            repo = app.state.context.repository
            run = repo.create_run(UUID(project_id), UUID(vor["version_id"]), {})
            detail = client.get(f"/api/v1/documents/{vor['document_id']}/detail")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["run_freshness"], "unavailable")
            with Session(repo._engine) as session:
                stored = session.get(Run, run.id)
                stored.config = {"pdf_versions": {}}
                session.add_all([
                    Run(project_id=UUID(project_id), vor_version_id=UUID(vor["version_id"]),
                        config={"pdf_versions": {}}, status="done")
                    for _ in range(105)
                ])
                session.commit()
            self.assertEqual(
                client.get(f"/api/v1/documents/{vor['document_id']}/detail").json()["run_freshness"],
                "current",
            )
            self.assertEqual(
                client.get(f"/api/v1/projects/{project_id}/audits/count").json(),
                {"count": 106},
            )
            self.assertEqual(
                client.get(f"/api/v1/projects/{self._project(client, 'Другой')}/audits/count").json(),
                {"count": 0},
            )

    def test_database_rejects_cross_project_pins_and_current_version(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import IntegrityError
        from storage_blobs import FileBlobStore
        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, _ = self._open_app(tmp)
            owner = self._project(client, "Своя стройка")
            other = self._project(client, "Чужая стройка")
            vor = self._upload(
                client,
                "",
                "vor",
                "vor.csv",
                fixtures._fake_vor_bytes(),
                "text/csv",
                project_id=owner,
            )
            pdf = self._upload(
                client,
                "",
                "pdf",
                "project.pdf",
                fixtures._fake_pdf_bytes(),
                "application/pdf",
                project_id=other,
            )
            repo = open_repository(_postgres_url(), FileBlobStore(tmp))
            from uuid import UUID

            run = repo.create_run(UUID(owner), UUID(vor["version_id"]), {})
            engine = create_engine(_postgres_url())
            self.addCleanup(engine.dispose)
            params = {
                "pdf": pdf["version_id"],
                "doc": vor["document_id"],
                "run": str(run.id),
            }
            for statement, state in (
                (
                    "INSERT INTO run_pdf_versions VALUES (:run, 'source.pdf', :pdf)",
                    "23514",
                ),
                (
                    "UPDATE documents SET current_version_id = :pdf WHERE id = :doc",
                    "23503",
                ),
            ):
                with self.subTest(statement=statement):
                    with (
                        self.assertRaises(IntegrityError) as raised,
                        engine.begin() as connection,
                    ):
                        connection.execute(text(statement), params)
                    self.assertEqual(raised.exception.orig.sqlstate, state)

    def test_concurrent_revision_uploads_keep_unique_order_and_dedup(self):
        from concurrent.futures import ThreadPoolExecutor
        from storage_blobs import FileBlobStore
        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            repo = open_repository(_postgres_url(), FileBlobStore(tmp))
            repo.reset_for_tests()
            project = repo.create_project("Параллельные редакции")
            first = repo.add_document_version(
                project.id,
                _vor_with_headers(),
                filename="vor.xlsx",
                kind="vor",
                new_document=True,
            )

            def upload(quantity):
                return repo.add_document_version(
                    project.id,
                    _vor_with_headers(quantity=quantity),
                    filename=f"vor-{quantity}.xlsx",
                    kind="vor",
                    document_id=first.document_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                versions = list(executor.map(upload, [46, 47]))
            self.assertEqual(sorted(v.version_no for v in versions), [2, 3])
            repeated_bytes = _vor_with_headers(quantity=48)

            def duplicate(_):
                return repo.add_document_version(
                    project.id,
                    repeated_bytes,
                    filename="repeat.xlsx",
                    kind="vor",
                    document_id=first.document_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                repeated = list(executor.map(duplicate, range(2)))
            self.assertEqual(repeated[0].id, repeated[1].id)
            self.assertEqual(repeated[0].version_no, 4)
    def test_delete_project_cascades_runs_documents_and_pins(self):
        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            client, app = self._open_app(tmp)
            project_id = self._project(client, "Под снос")
            vor = self._upload(
                client, "Под снос", "vor", "vor.csv",
                fixtures._fake_vor_bytes(), "text/csv",
                project_id=project_id, new_document=True,
            )
            pdf = self._upload(
                client, "Под снос", "pdf", "project.pdf",
                fixtures._fake_pdf_bytes(), "application/pdf",
                project_id=project_id, new_document=True,
            )
            created = client.post(
                "/api/v1/audits",
                json={
                    "project_id": project_id,
                    "vor_version_id": vor["version_id"],
                    "pdf_version_ids": [pdf["version_id"]],
                },
            )
            self.assertEqual(created.status_code, 202, created.text)
            run_id = created.json()["run_id"]
            self._wait_for_run(client, run_id)
            archived = client.patch(
                f"/api/v1/projects/{project_id}", json={"archived": True}
            )
            self.assertEqual(archived.status_code, 200, archived.text)
            deleted = client.delete(f"/api/v1/projects/{project_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(deleted.json()["id"], project_id)
            self.assertEqual(
                client.get(f"/api/v1/projects/{project_id}").status_code, 404
            )
            self.assertEqual(
                client.get(
                    f"/api/v1/projects/{project_id}/catalog"
                ).status_code,
                404,
            )
            self.assertEqual(
                client.get(f"/api/v1/audits/{run_id}").status_code, 404
            )
            names = [p["name"] for p in client.get("/api/v1/projects").json()]
            self.assertNotIn("Под снос", names)

    def test_migration_backfills_pins_without_moving_uuids(self):
        from storage_blobs import FileBlobStore
        from storage_db import downgrade, migrate, open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-lifecycle-") as tmp:
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            project = repo.create_project("Легаси")
            vor = repo.add_document_version(
                project.id,
                fixtures._fake_vor_bytes(),
                filename="vor.csv",
                kind="vor",
                new_document=True,
            )
            pdf = repo.add_document_version(
                project.id,
                fixtures._fake_pdf_bytes(),
                filename="project.pdf",
                kind="pdf",
                new_document=True,
            )
            run = repo.create_run(
                project.id,
                vor.id,
                {"pdf_versions": {"project.pdf": str(pdf.id)}},
                pdf_pins={"project.pdf": pdf.id},
            )
            version_uuid = str(vor.id)
            project_uuid = str(project.id)
            downgrade(_postgres_url(), "0002_jobs")
            migrate(_postgres_url())
            fresh = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            reloaded = fresh.load_run(run.id)
            self.assertEqual(reloaded["vor_filename"], "vor.csv")
            self.assertEqual(fresh.run_pinned_versions(run.id), {"project.pdf": pdf.id})
            self.assertEqual(
                {d["id"] for d in fresh.catalog(project.id)},
                {str(vor.document_id), str(pdf.document_id)},
            )
            self.assertEqual(str(fresh.load_project(project.id).id), project_uuid)
            self.assertEqual(fresh.load_version(vor.id).filename, "vor.csv")
            self.assertEqual(fresh.load_version(pdf.id).filename, "project.pdf")
            self.assertEqual(
                str(fresh.load_run(run.id)["vor_version_id"]), version_uuid
            )


if __name__ == "__main__":
    unittest.main()
