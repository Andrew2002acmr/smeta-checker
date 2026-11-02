"""Failure injection against real PostgreSQL; no Redis or paid model requests."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from audit_delivery import execute_delivery, publish_pending
from audit_tasks import prepare_run
from storage_models import TaskOutbox
import test_tasks as helpers


@pytest.fixture
def pipeline():
    helper = helpers.TaskPipelineTests()
    with tempfile.TemporaryDirectory(prefix="smeta-durable-") as tmp:
        context, repo = helper._make_context(tmp)
        vor, pdf = helpers._fake_vor_bytes(), helpers._fake_pdf_bytes()
        run, versions = helper._make_run(repo, vor, pdf)
        yield SimpleNamespace(
            context=context, repo=repo, run=run, versions=versions, vor=vor, pdf=pdf
        )
        repo._engine.dispose()


def prepare(p, **kwargs):
    with patch("audit_tracing._configured_client", return_value=None):
        return prepare_run(
            p.context,
            p.run.id,
            project="test",
            vor_filename="vor.csv",
            vor_bytes=p.vor,
            pdf_files={"project.pdf": p.pdf},
            pdf_versions=p.versions,
            worker=kwargs.pop("worker", "pdf-test"),
            **kwargs,
        )


def expire(p, task_id):
    with Session(p.repo._engine) as session:
        row = session.get(TaskOutbox, UUID(task_id))
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        row.available_at = row.lease_until
        session.commit()


def enqueue(p):
    p.repo.enqueue_task("audit_tasks.prepare_run", {"run_id": str(p.run.id)})
    return p.repo.reserve_deliveries()[0]


def test_soft_timeout_checkpoints_and_resumes_on_another_worker(pipeline):
    p = pipeline
    with patch(
        "pdf_text.PDFTextSource.for_position", side_effect=SoftTimeLimitExceeded()
    ):
        with pytest.raises(SoftTimeLimitExceeded):
            prepare(p, slice_seconds=120, durable=True)
    assert p.repo.run_status(p.run.id) == "running"
    assert not p.repo.load_run(p.run.id)["config"]["prepare_complete"]
    with patch(
        "vor_parse.load_vor_frame_positions",
        side_effect=AssertionError("must reuse parsed VOR"),
    ):
        prepare(
            p,
            slice_seconds=120,
            durable=True,
            worker="replacement",
            dispatch=p.repo.enqueue_task,
        )
    job = p.repo.list_position_jobs(p.run.id)[0]
    assert job.queue == "llm" and job.status == "queued"
    assert "vor_positions" not in job.payload
    assert job.payload["vor_context"]
    assert p.repo.load_run(p.run.id)["config"]["prepare_complete"]
    assert p.repo.reserve_deliveries()[0]["name"] == "audit_tasks.audit_position"


def test_preparation_slice_yields_before_first_position(pipeline):
    assert prepare(pipeline, slice_seconds=0)["continue"]
    assert pipeline.repo.list_position_jobs(pipeline.run.id) == []
    assert prepare(pipeline)["queued"] == [0]


def test_finalization_waits_for_preparation_and_expected_count(pipeline):
    p = pipeline
    p.repo.update_run_config(p.run.id, {"expected_positions": 2})
    for i in range(2):
        p.repo.create_position_job(p.run.id, i, queue="llm", payload={})
        if i == 0:
            p.repo.finish_position_job(p.run.id, i, "done")
    assert not p.repo.claim_finalize(p.run.id)
    p.repo.update_run_config(p.run.id, {"prepare_complete": True})
    assert not p.repo.claim_finalize(p.run.id)
    p.repo.finish_position_job(p.run.id, 1, "done")
    assert p.repo.claim_finalize(p.run.id)
    assert not p.repo.claim_finalize(p.run.id)
    rows = p.repo.reserve_deliveries()
    assert len(rows) == 1 and rows[0]["name"] == "audit_tasks.finalize_run"


def test_concurrent_finalizers_create_one_durable_intent(pipeline):
    from concurrent.futures import ThreadPoolExecutor

    p = pipeline
    p.repo.update_run_config(
        p.run.id, {"prepare_complete": True, "expected_positions": 0}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(
            pool.map(lambda _: p.repo.claim_finalize(p.run.id), range(2))
        ) == [False, True]
    assert len(p.repo.reserve_deliveries()) == 1


def test_broker_failure_preserves_intent_and_redelivers_same_token(pipeline):
    p = pipeline
    p.repo.enqueue_task("audit_tasks.prepare_run", {"run_id": str(p.run.id)})
    assert publish_pending(p.repo, Mock(side_effect=ConnectionError("offline"))) == 0
    with Session(p.repo._engine) as session:
        row = session.scalar(select(TaskOutbox))
        task_id, token = str(row.id), str(row.delivery_token)
    expire(p, task_id)
    sent = Mock()
    assert publish_pending(p.repo, sent) == 1
    assert sent.call_args.args[1]["delivery_token"] == token


def test_duplicate_message_executes_once(pipeline):
    p = pipeline
    task = enqueue(p)
    operation = Mock(return_value={"ok": True})
    execute_delivery(p.context, task["id"], task["token"], operation)
    assert execute_delivery(p.context, task["id"], task["token"], operation)[
        "duplicate"
    ]
    operation.assert_called_once()


def test_dead_child_lease_is_recovered_without_hostname_check(pipeline):
    p = pipeline
    first = enqueue(p)
    assert p.repo.claim_delivery(UUID(first["id"]), UUID(first["token"]), timeout=600)
    expire(p, first["id"])
    second = p.repo.reserve_deliveries()[0]
    assert second["id"] == first["id"] and second["token"] != first["token"]
    assert not p.repo.claim_delivery(
        UUID(first["id"]), UUID(first["token"]), timeout=600
    )
    assert p.repo.claim_delivery(UUID(second["id"]), UUID(second["token"]), timeout=600)


def test_repeated_hard_deaths_mark_run_failed(pipeline):
    p = pipeline
    task = enqueue(p)
    for _ in range(3):
        assert p.repo.claim_delivery(UUID(task["id"]), UUID(task["token"]), timeout=600)
        expire(p, task["id"])
        rows = p.repo.reserve_deliveries()
        if rows:
            task = rows[0]
    assert rows == []
    record = p.repo.load_run(p.run.id)
    assert record["status"] == "failed" and record["config"]["execution_error"]


def test_checkpoint_does_not_consume_retry_budget(pipeline):
    p = pipeline
    task = enqueue(p)
    for _ in range(5):
        execute_delivery(
            p.context, task["id"], task["token"], lambda: {"continue": True}
        )
        task = p.repo.reserve_deliveries()[0]
    assert p.repo.run_status(p.run.id) == "running"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_resume_keeps_completed_positions_and_requeues_unfinished(pipeline, status):
    p = pipeline
    for i, status in enumerate(("done", "running")):
        p.repo.create_position_job(
            p.run.id, i, queue="llm", payload={"work_name": "work"}
        )
        if status == "done":
            p.repo.finish_position_job(p.run.id, i, "done", result={"status": "OK"})
        else:
            p.repo.claim_position_job(p.run.id, i, worker="old-worker")
    p.repo.finish_run(p.run.id, status)
    p.repo.resume_run(p.run.id)
    jobs = p.repo.list_position_jobs(p.run.id)
    assert jobs[0].status == "done" and jobs[0].result == {"status": "OK"}
    assert jobs[1].status == "queued" and jobs[1].worker is None
    assert sorted(t["name"] for t in p.repo.reserve_deliveries()) == [
        "audit_tasks.audit_position",
        "audit_tasks.prepare_run",
    ]


def test_resume_refuses_active_delivery(pipeline):
    enqueue(pipeline)
    with pytest.raises(ValueError, match="lease"):
        pipeline.repo.resume_run(pipeline.run.id)


def test_user_resume_is_atomic_and_does_not_restart_an_active_run(pipeline):
    p = pipeline
    p.repo.finish_run(p.run.id, "cancelled")
    p.repo.resume_run(p.run.id, allow_running=False)
    with pytest.raises(ValueError, match="interrupted"):
        p.repo.resume_run(p.run.id, allow_running=False)
    assert len(p.repo.reserve_deliveries()) == 1


def test_cancelled_run_keeps_its_state_while_old_execution_is_active(pipeline):
    p = pipeline
    enqueue(p)
    p.repo.finish_run(p.run.id, "cancelled")
    with pytest.raises(ValueError, match="lease"):
        p.repo.resume_run(p.run.id, allow_running=False)
    assert p.repo.run_status(p.run.id) == "cancelled"


def test_registered_pipeline_completes_through_durable_deliveries(pipeline):
    from audit_tasks import celery_app, register_tasks

    p = pipeline
    app = register_tasks(
        celery_app(p.context.settings.model_copy(update={"broker_url": "memory://"}))
    )
    vor = p.repo.load_version(p.run.vor_version_id)
    pdf = p.repo.load_version(next(iter(p.versions.values())).id)
    prepare_payload = {
        "run_id": str(p.run.id),
        "project": "test",
        "vor_filename": "vor.csv",
        "vor_blob_key": vor.blob_key,
        "pdf_blob_keys": {"project.pdf": pdf.blob_key},
    }
    messages = []
    with (
        patch("audit_tasks.open_task_context", return_value=p.context),
        patch("audit_tracing._configured_client", return_value=None),
        patch.object(
            app,
            "send_task",
            side_effect=lambda name, **kw: messages.append((name, kw["kwargs"])),
        ),
    ):
        assert app.tasks["audit_tasks.prepare_run"].run(**prepare_payload)["adopted"]
        executed = []
        while messages:
            name, payload = messages.pop(0)
            executed.append(name)
            app.tasks[name].run(**payload)
    assert executed == [
        "audit_tasks.prepare_run",
        "audit_tasks.audit_position",
        "audit_tasks.finalize_run",
    ]
    record = p.repo.load_run(p.run.id)
    assert record["status"] == "done"
    assert len(record["results"]) == 1
    assert p.repo.load_run_report(p.run.id, "word")


def test_shared_snapshot_writes_only_changed_pages(tmp_path):
    from audit_core import PageTextIndex
    from audit_snapshot import store_snapshot, load_snapshot
    from storage_blobs import FileBlobStore

    store = FileBlobStore(tmp_path)
    cache = {}
    texts = tuple(f"page {i} cable 45 m " * 100 for i in range(100))
    first = PageTextIndex(texts, table_rows=[[] for _ in texts])
    store_snapshot(store, first, page_cache=cache)
    before = len(list(Path(tmp_path).rglob("*")))
    rows = [[] for _ in texts]
    rows[50] = ["new evidence | 45 | m"]
    second = PageTextIndex(texts, table_rows=rows)
    put = store.put
    with patch.object(store, "put", wraps=put) as writes:
        ref = store_snapshot(store, second, page_cache=cache)
        assert writes.call_count == 2  # One changed page and one manifest.
    assert load_snapshot(store, ref).to_snapshot() == second.to_snapshot()
    with patch.object(store, "get", wraps=store.get) as reads:
        assert load_snapshot(store, ref).to_snapshot() == second.to_snapshot()
        assert reads.call_count == 1  # Manifest only; verified pages are reused.
    assert len(list(Path(tmp_path).rglob("*"))) > before


def test_native_checkpoint_preserves_page_for_next_slice(tmp_path):
    import pymupdf
    from audit_tasks import PreparationYield
    from pdf_text import PDFTextSource

    def stop_after_write():
        raise PreparationYield()

    with pymupdf.open(stream=helpers._fake_pdf_bytes(), filetype="pdf") as doc:
        with pytest.raises(PreparationYield):
            PDFTextSource(doc, cache_dir=tmp_path / "pdf", checkpoint=stop_after_write)
        source = PDFTextSource(doc, cache_dir=tmp_path / "pdf")
        try:
            assert source.extraction_hits == 1
            assert source.extraction_misses == 0
        finally:
            source.close()


def test_legacy_monolithic_snapshot_remains_readable(tmp_path):
    from audit_core import PageTextIndex
    from audit_snapshot import encode_snapshot, load_snapshot, snapshot_ref
    from storage_blobs import FileBlobStore

    store = FileBlobStore(tmp_path)
    index = PageTextIndex(["Cable 45 m"])
    ref = store.put(encode_snapshot(index))
    restored = load_snapshot(
        store, snapshot_ref(index.to_snapshot(), ref.key, ref.sha256)
    )
    assert restored.to_snapshot() == index.to_snapshot()


def test_corrupt_shared_page_is_rejected(tmp_path):
    from audit_core import PageTextIndex
    from audit_snapshot import load_json, load_snapshot, store_snapshot
    from storage_blobs import FileBlobStore

    store = FileBlobStore(tmp_path)
    ref = store_snapshot(store, PageTextIndex(["Cable 45 m"]))
    manifest = load_json(
        store, {"key": ref["snapshot_key"], "sha256": ref["snapshot_sha256"]}
    )
    page = manifest["pages"][0]
    (tmp_path / page["key"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        load_snapshot(store, ref)
