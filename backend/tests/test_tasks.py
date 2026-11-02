"""Background jobs: statuses in Postgres, idempotent claims, restart recovery (issue 19).

Seams: Repository job methods, task functions with injected fakes, API dispatch.
Celery/Redis are reached only through the dispatcher seam (send_task/fallback);
no broker in unit tests.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage_blobs import FileBlobStore


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _open_repo(tmp):
    from storage_db import open_repository

    repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
    repo.reset_for_tests()
    return repo


class PositionJobTests(unittest.TestCase):
    def _make_run(self, repo):
        project = repo.create_project("ИГДИ")
        vor = repo.add_document_version(
            project.id, b"vor-bytes", filename="vor.csv", kind="vor",
            new_document=True,
        )
        pdf = repo.add_document_version(
            project.id, b"pdf-bytes", filename="project.pdf", kind="pdf",
            new_document=True,
        )
        run = repo.create_run(project.id, vor.id, {"model": "qwen-test"},
                              pdf_pins={"project.pdf": pdf.id})
        return run, pdf

    def test_claim_is_idempotent_and_serialized(self):
        repo = _open_repo(tempfile.mkdtemp(prefix="smeta-jobs-"))
        run, _ = self._make_run(repo)
        first = repo.create_position_job(run.id, 0, queue="pdf", payload={"k": "v"})
        self.assertEqual(first.status, "queued")
        claimed = repo.claim_position_job(run.id, 0, worker="w1")
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.attempts, 1)
        again = repo.claim_position_job(run.id, 0, worker="w1")
        self.assertEqual(again.worker, "w1")
        self.assertEqual(again.attempts, 1)
        with self.assertRaises(LookupError):
            repo.claim_position_job(run.id, 0, worker="w2")
        repo.finish_position_job(run.id, 0, "done", result={"status": "ОК"})
        record = repo.load_position_job(run.id, 0)
        self.assertEqual(record.status, "done")
        self.assertEqual(record.result["status"], "ОК")

    def test_failed_job_can_retry_without_duplicating_results(self):
        repo = _open_repo(tempfile.mkdtemp(prefix="smeta-jobs-"))
        run, _ = self._make_run(repo)
        repo.create_position_job(run.id, 0, queue="llm", payload={})
        repo.claim_position_job(run.id, 0, worker="w1")
        repo.finish_position_job(run.id, 0, "failed", error="boom")
        retried = repo.claim_position_job(run.id, 0, worker="w2")
        self.assertEqual(retried.status, "running")
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(retried.queue, "llm")

    def test_stale_running_jobs_recover_after_restart(self):
        repo = _open_repo(tempfile.mkdtemp(prefix="smeta-jobs-"))
        run, _ = self._make_run(repo)
        repo.create_position_job(run.id, 0, queue="pdf", payload={})
        repo.create_position_job(run.id, 1, queue="llm", payload={})
        repo.claim_position_job(run.id, 0, worker="dead-worker")
        repo.claim_position_job(run.id, 1, worker="dead-worker")
        repo.finish_position_job(run.id, 1, "done", result={"status": "ОК"})
        recovered = repo.recover_stale_jobs(run.id, alive_workers=set())
        self.assertEqual(sorted(recovered), [0])
        record = repo.load_position_job(run.id, 0)
        self.assertEqual(record.status, "queued")
        self.assertEqual(record.attempts, 1)

    def test_position_count_survives_run_finish(self):
        repo = _open_repo(tempfile.mkdtemp(prefix="smeta-jobs-"))
        run, _ = self._make_run(repo)
        repo.create_position_job(run.id, 0, queue="pdf", payload={})
        repo.claim_position_job(run.id, 0, worker="w1")
        repo.finish_position_job(run.id, 0, "done", result={"status": "ОК"})
        repo.finish_run(run.id, "done")
        loaded = repo.load_run(run.id)
        self.assertEqual(loaded["status"], "done")

    def test_claim_finalize_wins_exactly_once(self):
        repo = _open_repo(tempfile.mkdtemp(prefix="smeta-jobs-"))
        run, _ = self._make_run(repo)
        repo.update_run_config(run.id, {"prepare_complete": True, "expected_positions": 0})
        self.assertTrue(repo.claim_finalize(run.id))
        self.assertFalse(repo.claim_finalize(run.id))
        self.assertTrue(repo.load_run(run.id)["config"]["finalize_chained"])


def _fake_pdf_bytes():
    import pymupdf

    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 500, 600), "Copper cable 45 m. " * 40)
        return bytes(pdf.tobytes())


def _fake_vor_bytes(rows=("1;Copper cable;45;m;;project.pdf;1",)):
    header = (
        "№ п.п.;Наименование работ, ресурсов, затрат по проекту;"
        "Объём работ / Количество;Ед. изм.;Ссылка на ПД;"
        "Наименование файла;Номер страниц (через пробел)"
    )
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8-sig")


def _ok_client_factory():
    import json
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


class TaskPipelineTests(unittest.TestCase):
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
        vor = repo.add_document_version(
            project.id, vor_bytes, filename="vor.csv", kind="vor",
            new_document=True,
        )
        pdf = repo.add_document_version(
            project.id, pdf_bytes, filename="project.pdf", kind="pdf",
            new_document=True,
        )
        config = {
            "model": "qwen-test",
            "pdf_versions": {"project.pdf": str(pdf.id)},
            "vor_filename": "vor.csv",
        }
        run = repo.create_run(project.id, vor.id, config,
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

    def test_prepare_audit_finalize_matches_inprocess_payload(self):
        from unittest.mock import patch

        from audit_tasks import audit_position, finalize_run, prepare_run

        with tempfile.TemporaryDirectory(prefix="smeta-pipe-") as tmp:
            context, repo = self._make_context(tmp)
            vor_bytes = _fake_vor_bytes()
            pdf_bytes = _fake_pdf_bytes()
            run, versions = self._make_run(repo, vor_bytes, pdf_bytes)
            dispatched = []
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
                    dispatch=lambda name, payload: dispatched.append((name, payload)),
                )
            self.assertEqual(out["queued"], [0])
            self.assertEqual(out["failed"], [])
            self.assertEqual(
                dispatched,
                [
                    (
                        "audit_tasks.audit_position",
                        {"run_id": str(run.id), "position_index": 0},
                    )
                ],
            )
            job = repo.load_position_job(run.id, 0)
            self.assertEqual(job.queue, "llm")
            self.assertEqual(job.status, "queued")
            with patch("audit_tracing._configured_client", return_value=None):
                result = audit_position(context, run.id, 0, worker="llm-test")
            self.assertFalse(result["duplicate"])
            loaded = repo.load_run(run.id)
            self.assertEqual(len(loaded["results"]), 1)
            self.assertEqual(loaded["results"][0]["status"], "ОК")
            self.assertTrue(loaded["results"][0]["work_found"])
            again = audit_position(context, run.id, 0, worker="llm-test")
            self.assertTrue(again["duplicate"])
            with patch("audit_tracing._configured_client", return_value=None):
                final = finalize_run(context, run.id, worker="pdf-test")
            self.assertEqual(final["positions"], 1)
            self.assertIn("metrics", final)
            self.assertEqual(final["metrics"]["statuses"], {"ОК": 1})
            self.assertIn("prepare", final["metrics"]["stage_seconds"])
            finished = repo.load_run(run.id)
            self.assertEqual(finished["status"], "done")
            self.assertEqual(len(finished["results"]), 1)
            self.assertEqual(finished["results"][0]["status"], "ОК")
            self.assertEqual(finished["results"][0]["usage"], {"total": 100})
            source = finished["results"][0]["position_source"]
            self.assertEqual(source["row"], 1)
            self.assertEqual(source["header_row"], 1)
            self.assertEqual(source["format"], "csv")
            self.assertEqual(source["sheet"], "")
            self.assertEqual(
                finished["config"]["metrics"]["statuses"], {"ОК": 1}
            )
            self.assertEqual(repo.load_run_report(run.id, "word")[:2], b"PK")

    def test_one_broken_position_does_not_abort_others(self):
        from unittest.mock import patch

        from audit_tasks import audit_position, finalize_run, prepare_run

        rows = (
            "1;Copper cable;45;m;;project.pdf;1",
            "2;Unknown work;10;kg;;missing.pdf;99",
        )
        with tempfile.TemporaryDirectory(prefix="smeta-pipe-") as tmp:
            context, repo = self._make_context(tmp)
            vor_bytes = _fake_vor_bytes(rows)
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
            self.assertEqual(sorted(out["queued"]), [0, 1])
            with patch("audit_tracing._configured_client", return_value=None):
                audit_position(context, run.id, 0, worker="llm-test")
                audit_position(context, run.id, 1, worker="llm-test")
                finalize_run(context, run.id, worker="pdf-test")
            loaded = repo.load_run(run.id)
            self.assertEqual(loaded["status"], "done")
            self.assertEqual(len(loaded["results"]), 2)
            statuses = [item["status"] for item in loaded["results"]]
            self.assertIn("ОК", statuses)
            failed = next(item for item in loaded["results"]
                          if item["status"] == "Ошибка")
            self.assertIn("missing.pdf", failed["discrepancy_details"])
            self.assertIsNotNone(failed["position_source"])

    def test_recover_redispatches_orphaned_positions(self):
        from audit_tasks import recover_run

        with tempfile.TemporaryDirectory(prefix="smeta-pipe-") as tmp:
            context, repo = self._make_context(tmp)
            run, _ = self._make_run(repo, _fake_vor_bytes(), _fake_pdf_bytes())
            repo.create_position_job(run.id, 0, queue="llm", payload={})
            repo.claim_position_job(run.id, 0, worker="dead")
            dispatched = []
            out = recover_run(
                context,
                run.id,
                alive_workers=set(),
                dispatch=lambda name, payload: dispatched.append((name, payload)),
            )
            self.assertEqual(out["recovered"], [0])
            self.assertEqual(
                dispatched,
                [
                    (
                        "audit_tasks.audit_position",
                        {"run_id": str(run.id), "position_index": 0},
                    )
                ],
            )

    def test_last_position_chains_finalize_exactly_once(self):
        from unittest.mock import patch

        from audit_tasks import audit_position, prepare_run

        rows = (
            "1;Copper cable;45;m;;project.pdf;1",
            "2;Copper cable;45;m;;project.pdf;1",
        )
        with tempfile.TemporaryDirectory(prefix="smeta-pipe-") as tmp:
            context, repo = self._make_context(tmp)
            vor_bytes = _fake_vor_bytes(rows)
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
            self.assertEqual(sorted(out["queued"]), [0, 1])
            chained: list[tuple[str, dict]] = []
            dispatch = lambda name, payload: chained.append((name, payload))  # noqa: E731
            with patch("audit_tracing._configured_client", return_value=None):
                audit_position(
                    context, run.id, 0, worker="llm-1", dispatch=dispatch
                )
                self.assertEqual(chained, [])
                audit_position(
                    context, run.id, 1, worker="llm-2", dispatch=dispatch
                )
            self.assertEqual(len(chained), 1)
            name, payload = chained[0]
            self.assertEqual(name, "audit_tasks.finalize_run")
            self.assertEqual(payload["run_id"], str(run.id))
            self.assertTrue(repo.load_run(run.id)["config"]["finalize_chained"])

    def test_failed_last_position_still_chains_finalize(self):
        from unittest.mock import patch

        from audit_tasks import audit_position, prepare_run

        with tempfile.TemporaryDirectory(prefix="smeta-pipe-") as tmp:
            context, repo = self._make_context(tmp)
            run, versions = self._make_run(
                repo, _fake_vor_bytes(), _fake_pdf_bytes()
            )
            with patch("audit_tracing._configured_client", return_value=None):
                prepare_run(
                    context,
                    run.id,
                    project="ИГДИ",
                    vor_filename="vor.csv",
                    vor_bytes=_fake_vor_bytes(),
                    pdf_files={"project.pdf": _fake_pdf_bytes()},
                    pdf_versions=versions,
                    worker="pdf-test",
                )
            chained: list[tuple[str, dict]] = []
            dispatch = lambda name, payload: chained.append((name, payload))  # noqa: E731
            with patch(
                "audit_tasks.traced_worker_check_row",
                side_effect=RuntimeError("llm down"),
            ), patch("audit_tracing._configured_client", return_value=None):
                with self.assertRaises(RuntimeError):
                    audit_position(
                        context, run.id, 0, worker="llm-1", dispatch=dispatch
                    )
            self.assertEqual(repo.load_position_job(run.id, 0).status, "failed")
            self.assertEqual(len(chained), 1)
            self.assertEqual(chained[0][0], "audit_tasks.finalize_run")
            self.assertEqual(chained[0][1]["run_id"], str(run.id))


if __name__ == "__main__":
    unittest.main()
