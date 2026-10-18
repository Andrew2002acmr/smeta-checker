"""Persistent storage: blobs, Postgres history, restart recovery (issue 17)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage_blobs import FileBlobStore, open_blob_store


class BlobStoreTests(unittest.TestCase):
    def test_put_get_roundtrip_is_content_addressed(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            store = FileBlobStore(tmp)
            ref = store.put(b"%PDF-1.7 fake")
            self.assertEqual(store.get(ref.key), b"%PDF-1.7 fake")
            self.assertEqual(len(ref.sha256), 64)
            self.assertTrue(ref.key.endswith(ref.sha256))
            self.assertEqual(ref.size, 13)

    def test_same_bytes_stored_once(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            store = FileBlobStore(tmp)
            first = store.put(b"vor-bytes")
            second = store.put(b"vor-bytes")
            self.assertEqual(first.key, second.key)
            files = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(len(files), 1)

    def test_blobs_survive_new_store_instance(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            ref = FileBlobStore(tmp).put(b"excel-bytes")
            self.assertEqual(FileBlobStore(tmp).get(ref.key), b"excel-bytes")

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            with self.assertRaises(FileNotFoundError):
                FileBlobStore(tmp).get("aa/bb/" + "0" * 64)

    def test_factory_opens_file_url(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            store = open_blob_store(f"file://{tmp}")
            ref = store.put(b"docx-bytes")
            self.assertEqual(store.get(ref.key), b"docx-bytes")


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _ok_result():
    return {
        "status": "ОК",
        "work_found": True,
        "volume_confirmed": True,
        "unit_confirmed": True,
        "comment_confirmed": True,
        "found_volume": "45 m",
        "found_unit": "m",
        "found_pages": [1, 2],
        "evidence": [
            {
                "document_id": "project.pdf",
                "page": 1,
                "quote": "Copper cable",
                "kind": "work",
            }
        ],
    }


class RepositoryTests(unittest.TestCase):
    def _open(self, blobs):
        from storage_db import open_repository

        repo = open_repository(_postgres_url(), blob_store=blobs)
        repo.reset_for_tests()
        return repo

    def test_save_and_reload_run_history(self):
        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            repo = self._open(FileBlobStore(tmp))
            project = repo.create_project("ИГДИ")
            vor_version = repo.add_document_version(
                project.id, b"vor-bytes", filename="vor.xlsx", kind="vor"
            )
            pdf_version = repo.add_document_version(
                project.id, b"pdf-bytes", filename="project.pdf", kind="pdf"
            )
            run = repo.create_run(
                project.id,
                vor_version.id,
                {"pdf_version_id": str(pdf_version.id), "model": "qwen-test"},
            )
            repo.save_position_result(run.id, 0, _ok_result())
            repo.save_run_report(run.id, "excel", b"excel-bytes")
            repo.finish_run(run.id, "done")

            fresh = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            history = fresh.list_runs(project.id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].id, run.id)
            self.assertEqual(history[0].status, "done")
            loaded = fresh.load_run(run.id)
            self.assertEqual(loaded["vor_filename"], "vor.xlsx")
            self.assertEqual(loaded["vor_sha256"], vor_version.sha256)
            self.assertEqual(len(loaded["results"]), 1)
            self.assertEqual(loaded["results"][0]["status"], "ОК")
            self.assertEqual(
                loaded["results"][0]["evidence"][0]["quote"], "Copper cable"
            )
            self.assertEqual(fresh.load_run_report(run.id, "excel"), b"excel-bytes")

    def test_rerun_appends_new_run_without_touching_history(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            repo = self._open(FileBlobStore(tmp))
            project = repo.create_project("ИГДИ")
            vor_version = repo.add_document_version(
                project.id, b"vor-bytes", filename="vor.xlsx", kind="vor"
            )
            first = repo.create_run(project.id, vor_version.id, {})
            repo.save_position_result(first.id, 0, _ok_result())
            repo.finish_run(first.id, "done")
            second = repo.create_run(project.id, vor_version.id, {})
            repo.finish_run(second.id, "done")

            history = repo.list_runs(project.id)
            self.assertEqual([item.id for item in history], [second.id, first.id])
            loaded = repo.load_run(first.id)
            self.assertEqual(len(loaded["results"]), 1)
            self.assertEqual(loaded["results"][0]["status"], "ОК")

    def test_reupload_creates_new_version_with_hash_identity(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            repo = self._open(FileBlobStore(tmp))
            project = repo.create_project("ИГДИ")
            first = repo.add_document_version(
                project.id, b"bytes-v1", filename="vor.xlsx", kind="vor"
            )
            same = repo.add_document_version(
                project.id, b"bytes-v1", filename="vor.xlsx", kind="vor"
            )
            self.assertEqual(same.id, first.id)
            second = repo.add_document_version(
                project.id, b"bytes-v2", filename="vor.xlsx", kind="vor"
            )
            self.assertEqual(second.document_id, first.document_id)
            self.assertNotEqual(second.id, first.id)
            self.assertNotEqual(second.sha256, first.sha256)
            self.assertEqual(second.version_no, first.version_no + 1)
            self.assertEqual(len(second.sha256), 64)

    def test_same_bytes_share_blob_across_documents(self):
        with tempfile.TemporaryDirectory(prefix="smeta-blobs-") as tmp:
            repo = self._open(FileBlobStore(tmp))
            project = repo.create_project("ИГДИ")
            first = repo.add_document_version(
                project.id, b"same-bytes", filename="a/vor.xlsx", kind="vor"
            )
            second = repo.add_document_version(
                project.id, b"same-bytes", filename="b/vor.xlsx", kind="vor"
            )
            self.assertNotEqual(first.document_id, second.document_id)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.blob_key, second.blob_key)


class MigrationTests(unittest.TestCase):
    EXPECTED = {
        "projects",
        "documents",
        "document_versions",
        "runs",
        "position_results",
        "run_artifacts",
    }

    def test_upgrade_head_creates_all_tables(self):
        from sqlalchemy import create_engine, inspect

        from storage_db import migrate

        migrate(_postgres_url())
        tables = set(inspect(create_engine(_postgres_url())).get_table_names())
        self.assertTrue(self.EXPECTED <= tables, tables)
        self.assertIn("alembic_version", tables)

    def test_downgrade_base_removes_tables(self):
        from sqlalchemy import create_engine, inspect
        from sqlalchemy.engine import make_url

        from storage_db import downgrade, migrate

        url = make_url(_postgres_url())
        admin = create_engine(
            url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        # NOTE: str(url) masks the password as "***"; render it explicitly.
        probe = url.set(database="smeta_migrate_probe").render_as_string(
            hide_password=False
        )
        probe_engine = create_engine(probe)
        with admin.connect() as conn:
            conn.exec_driver_sql('DROP DATABASE IF EXISTS "smeta_migrate_probe"')
            conn.exec_driver_sql('CREATE DATABASE "smeta_migrate_probe"')
        try:
            migrate(probe)
            tables = set(inspect(probe_engine).get_table_names())
            self.assertTrue(self.EXPECTED <= tables, tables)
            downgrade(probe)
            probe_engine.dispose()
            probe_engine = create_engine(probe)
            tables = set(inspect(probe_engine).get_table_names())
            self.assertEqual(self.EXPECTED & tables, set())
        finally:
            probe_engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql('DROP DATABASE IF EXISTS "smeta_migrate_probe"')
        admin.dispose()


class AuditPersistenceTests(unittest.TestCase):
    def _fake_vor(self, folder):
        import fitz

        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_textbox(fitz.Rect(50, 50, 500, 600), "Copper cable 45 m. " * 40)
            pdf.save(folder / "project.pdf")
        header = [
            "№ п.п.",
            "Наименование работ, ресурсов, затрат по проекту",
            "Объём работ / Количество",
            "Ед. изм.",
            "Ссылка на ПД",
            "Наименование файла",
            "Номер страниц (через пробел)",
        ]
        (folder / "vor.csv").write_text(
            ";".join(header) + "\n1;Copper cable;45;m;;project.pdf;1\n",
            encoding="utf-8-sig",
        )

    def _run(self, folder, repo, client_factory):
        import fitz

        from audit_service import run_audit

        pool = {"project.pdf": fitz.open(folder / "project.pdf")}
        try:
            return run_audit(
                {"vor.csv": str(folder / "vor.csv")},
                ["vor.csv"],
                pool,
                api_key="test-only",
                folder="test-folder",
                model_name="qwen-test",
                client_factory=client_factory,
                sleeper=lambda _: None,
                store=repo,
                project_name="ИГДИ",
            )
        finally:
            for doc in pool.values():
                doc.close()

    def _ok_factory(self):
        from types import SimpleNamespace

        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = SimpleNamespace(create=self.create)

            def create(self, **kwargs):
                import json

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

    def test_run_audit_persists_run_and_results(self):
        from unittest.mock import patch

        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-audit-persist-") as tmp:
            folder = Path(tmp)
            self._fake_vor(folder)
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            project = repo.create_project("ИГДИ")
            with patch("audit_tracing._configured_client", return_value=None):
                outcome = self._run(folder, repo, self._ok_factory())
            self.assertEqual(outcome["issues"], [])
            self.assertIn("vor.csv", outcome["results"])
            runs = repo.list_runs(project.id)
            self.assertEqual(len(runs), 1)
            loaded = repo.load_run(runs[0].id)
            self.assertEqual(len(loaded["results"]), 1)
            self.assertEqual(
                loaded["results"][0]["status"],
                outcome["results"]["vor.csv"]["df"]["Статус аудита"].iloc[0],
            )
            self.assertEqual(
                repo.load_run_report(runs[0].id, "excel"),
                outcome["results"]["vor.csv"]["excel_bytes"],
            )
            self.assertEqual(
                repo.load_run_report(runs[0].id, "word"),
                outcome["results"]["vor.csv"]["word_bytes"],
            )

    def test_rerun_keeps_previous_run_intact(self):
        from unittest.mock import patch

        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-audit-rerun-") as tmp:
            folder = Path(tmp)
            self._fake_vor(folder)
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            project = repo.create_project("ИГДИ")
            with patch("audit_tracing._configured_client", return_value=None):
                first = self._run(folder, repo, self._ok_factory())
                second = self._run(folder, repo, self._ok_factory())
            self.assertIn("run_id", first["results"]["vor.csv"])
            self.assertIn("run_id", second["results"]["vor.csv"])
            self.assertNotEqual(
                first["results"]["vor.csv"]["run_id"],
                second["results"]["vor.csv"]["run_id"],
            )
            history = repo.list_runs(project.id)
            self.assertEqual(len(history), 2)
            for run in history:
                loaded = repo.load_run(run.id)
                self.assertEqual(len(loaded["results"]), 1)
                self.assertEqual(loaded["results"][0]["status"], "ОК")

    def test_pdf_hash_is_stable_from_original_bytes(self):
        import hashlib
        from unittest.mock import patch

        from audit_service import run_audit
        from storage_db import open_repository

        with tempfile.TemporaryDirectory(prefix="smeta-audit-pdfhash-") as tmp:
            folder = Path(tmp)
            self._fake_vor(folder)
            repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
            repo.reset_for_tests()
            repo.create_project("ИГДИ")
            expected = hashlib.sha256((folder / "project.pdf").read_bytes()).hexdigest()

            import fitz

            with patch("audit_tracing._configured_client", return_value=None):
                with patch(
                    "audit_service.audit_position_hybrid",
                    return_value={
                        "status": "ОК",
                        "work_found": True,
                        "volume_confirmed": True,
                        "unit_confirmed": True,
                        "comment_confirmed": True,
                        "found_volume": "45",
                        "found_unit": "m",
                        "found_pages": [1],
                        "evidence": [],
                        "discrepancy_details": "stub",
                    },
                ):
                    pool = {"project.pdf": fitz.open(folder / "project.pdf")}
                    try:
                        run_audit(
                            {"vor.csv": str(folder / "vor.csv")},
                            ["vor.csv"],
                            pool,
                            api_key="t",
                            folder="t",
                            model_name="t",
                            sleeper=lambda _: None,
                            store=repo,
                            project_name="ИГДИ",
                            pdf_sources={"project.pdf": str(folder / "project.pdf")},
                        )
                    finally:
                        for doc in pool.values():
                            doc.close()
            project = repo.get_or_create_project("ИГДИ")
            loaded = repo.load_run(repo.list_runs(project.id)[0].id)
            self.assertEqual(loaded["results"][0]["pdf_sha256"], expected)


if __name__ == "__main__":
    unittest.main()
