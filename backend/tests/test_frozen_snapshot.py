"""Frozen snapshot propagation: prepare persists, worker restores, no PDF reopen.

Covers the stage5 contract on the behavior surface (no copy-echo):
roundtrip fidelity, Celery prepare->audit without PDF access in the worker,
explicit prep failure on missing/tampered blobs, direct-path in-memory parity.
"""

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_snapshot import (
    SNAPSHOT_SCHEMA,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    store_snapshot,
)
from storage_blobs import FileBlobStore


def _fake_pdf_bytes(pages=("Copper cable 45 m", "second page")):
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    try:
        return bytes(doc.tobytes())
    finally:
        doc.close()


def _fake_vor_bytes(rows=("1;Copper cable;45;m;;project.pdf;1",)):
    header = ("№ п.п.;Наименование работ;Объем работ / Количество;Ед. изм.;"
              "Ссылка на ПД;Наименование файла;Номер страниц (через пробел)")
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8-sig")


def _ok_client_factory():
    from types import SimpleNamespace

    class FakeClient:
        def __init__(self, **_kwargs):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            import re
            source_id = re.findall(r"\[([a-z0-9]+)\] ", kwargs["input"])[0]
            body = {
                "claims": [
                    {"claim_id": "presence", "source_ids": [source_id]},
                    {"claim_id": "quantity", "source_ids": [source_id],
                     "value_source_id": source_id, "unit_source_id": source_id},
                    {"claim_id": "unit", "source_ids": [source_id]},
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


class SnapshotCodecTests(unittest.TestCase):
    def _index(self):
        from audit_core import PageTextIndex

        return PageTextIndex(
            ("Copper cable 45 m", "scan needs ocr"),
            {1: "boom"},
            (1,),
            (("Copper cable | 45 | m",), ()),
            ([{"source_id": "a", "page": 1}], [{"source_id": "b", "page": 2}]),
            quality={0: {"state": "usable"}},
            metadata={"pdf_sha256": "abc"},
        )

    def test_roundtrip_preserves_all_fields_and_corpus_id(self):
        index = self._index()
        restored = decode_snapshot(encode_snapshot(index))
        self.assertEqual(list(restored.texts), list(index.texts))
        self.assertEqual(restored.errors, index.errors)
        self.assertEqual(list(restored.pending_ocr), [1])
        self.assertEqual(
            [list(rows) for rows in restored.table_rows],
            [["Copper cable | 45 | m"], []],
        )
        self.assertEqual(
            [[dict(item) for item in page] for page in restored.locators],
            [[{"source_id": "a", "page": 1}], [{"source_id": "b", "page": 2}]],
        )
        self.assertEqual(restored.quality, {0: {"state": "usable"}})
        self.assertEqual(restored.metadata, {"pdf_sha256": "abc"})
        self.assertEqual(
            restored.to_snapshot()["corpus_id"], index.to_snapshot()["corpus_id"]
        )
    def test_roundtrip_with_double_digit_pages(self):
        # Regression: int page keys sort numerically on write
        # but lexicographically after a JSON round trip,
        # which used to break corpus_id on multi-page PDFs.
        from audit_core import PageTextIndex

        texts = tuple("page %d body" % i for i in range(15))
        index = PageTextIndex(
            texts,
            {2: "scan issue", 10: "complex drawing", 12: "read failure"},
            (10,),
            quality={0: {"state": "usable"}, 3: {"state": "pending"},
                     11: {"state": "uncertain"}},
            metadata={"pdf_sha256": "abc"},
        )
        snapshot = index.to_snapshot()
        wire = json.loads(json.dumps(snapshot))
        restored = decode_snapshot(encode_snapshot(index))
        self.assertEqual(restored.to_snapshot(), snapshot)
        from audit_core import PageTextIndex as Restored

        self.assertEqual(Restored.from_snapshot(wire).to_snapshot(), snapshot)

    def test_decode_rejects_tampered_payload(self):
        data = encode_snapshot(self._index())
        raw = bytearray(gzip.decompress(data))
        raw[raw.index(b"Copper")] = ord("X")
        with self.assertRaises(ValueError):
            decode_snapshot(bytes(gzip.compress(bytes(raw))))

    def test_store_verifies_blob_sha_and_load_checks_ref(self):
        index = self._index()
        with tempfile.TemporaryDirectory(prefix="smeta-snap-") as tmp:
            store = FileBlobStore(tmp)
            ref = store_snapshot(store, index)
            self.assertEqual(ref["snapshot_schema"], SNAPSHOT_SCHEMA)
            self.assertEqual(ref["corpus_id"], index.to_snapshot()["corpus_id"])
            restored = load_snapshot(store, dict(ref))
            self.assertEqual(list(restored.texts), list(index.texts))
            tampered = dict(ref, snapshot_sha256="0" * 64)
            with self.assertRaises(ValueError):
                load_snapshot(store, tampered)
            with self.assertRaises(ValueError):
                load_snapshot(store, {})


def _postgres_url():
    import os

    return os.environ.get(
        "TEST_POSTGRES_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/smeta_test"
    )


def _open_repo(tmp):
    from storage_db import open_repository

    return open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))


class FrozenPropagationTests(unittest.TestCase):
    def _make_context(self, tmp):
        from audit_models import AuditSettings
        from audit_tasks import TaskContext

        repo = _open_repo(tmp)
        settings = AuditSettings(
            yandex_cloud_api_key="test-only",
            yandex_cloud_folder="test-folder",
            yandex_cloud_model="qwen-test",
            database_url=_postgres_url(),
            blob_store_url=f"file://{tmp}",
        )
        return (
            TaskContext(
                settings=settings,
                repository=repo,
                blob_store=FileBlobStore(tmp),
                client_factory=_ok_client_factory(),
            ),
            repo,
        )

    def _make_run(self, repo, vor_bytes, pdf_bytes):
        from storage_db import DocumentVersionRow

        project = repo.get_or_create_project("ИГДИ")
        department_id = repo.list_departments(project.id)[0]["id"]
        vor = repo.add_document_version(project.id, vor_bytes, filename="vor.csv", kind="vor",
                                            department_id=department_id,
                                        new_document=True)
        pdf = repo.add_document_version(project.id, pdf_bytes, filename="project.pdf", kind="pdf",
                                            department_id=department_id,
                                        new_document=True)
        run = repo.create_run(project.id, vor.id, {"pdf_versions": {"project.pdf": str(pdf.id)}},
                              pdf_pins={"project.pdf": pdf.id})
        versions = {
            "project.pdf": DocumentVersionRow(
                id=pdf.id,
                document_id=pdf.document_id,
                version_no=pdf.version_no,
                sha256=pdf.sha256,
                blob_key=pdf.blob_key,
                size=pdf.size,
                filename=pdf.filename,
            )
        }
        return run, versions

    def test_prepare_audit_never_reopens_pdf(self):
        from audit_tasks import audit_position, prepare_run

        with tempfile.TemporaryDirectory(prefix="smeta-frozen-") as tmp:
            context, repo = self._make_context(tmp)
            vor_bytes = _fake_vor_bytes()
            pdf_bytes = _fake_pdf_bytes()
            run, versions = self._make_run(repo, vor_bytes, pdf_bytes)
            with patch("audit_tracing._configured_client", return_value=None):
                out = prepare_run(
                    context,
                    run.id,
                    project="ИГДИ",
                    vor_filename="vor.csv",
                    vor_bytes=vor_bytes,
                    pdf_files={"project.pdf": pdf_bytes},
                    pdf_versions=versions,
                    worker="pdf-test",
                )
            self.assertEqual(out["queued"], [0])
            job = repo.load_position_job(run.id, 0)
            snapshot = job.payload.get("snapshot") or {}
            self.assertEqual(snapshot.get("snapshot_schema"), SNAPSHOT_SCHEMA)
            self.assertTrue(snapshot.get("snapshot_key"))
            self.assertTrue(snapshot.get("snapshot_sha256"))
            self.assertTrue(job.payload.get("corpus_id"))
            stored_bytes = context.blob_store.get(snapshot["snapshot_key"])
            self.assertEqual(
                hashlib.sha256(stored_bytes).hexdigest(), snapshot["snapshot_sha256"]
            )
            with (
                patch("audit_tracing._configured_client", return_value=None),
                patch("pymupdf.open", side_effect=AssertionError("worker opened PDF")),
            ):
                result = audit_position(context, run.id, 0, worker="llm-test")
            self.assertFalse(result["duplicate"])
            loaded = repo.load_run(run.id)
            self.assertEqual(
                loaded["results"][0].get("corpus_id"), job.payload.get("corpus_id")
            )
            self.assertNotIn("pdf_files", job.payload)

    def test_missing_snapshot_is_explicit_prep_error_with_workbook_kept(self):
        from audit_tasks import audit_position, prepare_run

        with tempfile.TemporaryDirectory(prefix="smeta-frozen-miss-") as tmp:
            context, repo = self._make_context(tmp)
            vor_bytes = _fake_vor_bytes()
            pdf_bytes = _fake_pdf_bytes()
            run, versions = self._make_run(repo, vor_bytes, pdf_bytes)
            with patch("audit_tracing._configured_client", return_value=None):
                prepare_run(
                    context,
                    run.id,
                    project="ИГДИ",
                    vor_filename="vor.csv",
                    vor_bytes=vor_bytes,
                    pdf_files={"project.pdf": pdf_bytes},
                    pdf_versions=versions,
                    worker="pdf-test",
                )
            job = repo.load_position_job(run.id, 0)
            payload = dict(job.payload or {})
            payload["snapshot"] = {}
            repo.requeue_position_job(run.id, 0, queue="llm", payload=payload)
            with patch("audit_tracing._configured_client", return_value=None):
                audit_position(context, run.id, 0, worker="llm-test")
            stored = repo.load_run(run.id)["results"][0]
            self.assertEqual(stored["status"], "Технический сбой")
            self.assertIn("снимок корпуса", stored["discrepancy_details"].lower())
            # Workbook identity/checks survive the PDF-side failure.
            self.assertIsNotNone(stored.get("position_source"))
            self.assertEqual(stored.get("workbook_notes"), [])
            again = repo.load_position_job(run.id, 0)
            self.assertEqual(again.status, "done")


if __name__ == "__main__":
    unittest.main()
