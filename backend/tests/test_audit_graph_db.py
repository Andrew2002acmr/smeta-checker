"""Graph budgets, receipts and idempotent publication survive the database."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from audit_assessment import reduce_assessment
from audit_graph_store import (
    advisory_key,
    graph_ask,
    publish_assessment,
    record_model_receipt,
    reserve_model_call,
    thread_id,
)
from storage_blobs import FileBlobStore


def _postgres_url():
    return os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://nikgretenok@localhost:5432/smeta_test",
    )


def _assessment(run_id):
    return reduce_assessment({
        "run_id": str(run_id),
        "position_index": 0,
        "execution_revision": 0,
        "fingerprint": {"sha256": "abc"},
        "project": {"obligations": [{
            "id": "project:binding", "domain": "presence", "subject": "Привязка",
            "state": "confirmed", "reason": "Источник проверен.",
        }]},
        "normative": {"obligations": []},
        "relationships": {"obligations": []},
        "execution_state": "complete",
    }).model_dump(mode="json")


class GraphStoreTests(unittest.TestCase):
    def _open(self, tmp):
        from storage_db import open_repository

        repo = open_repository(_postgres_url(), blob_store=FileBlobStore(tmp))
        repo.reset_for_tests()
        return repo

    def _run(self, repo):
        project = repo.create_project("Граф")
        vor = repo.add_document_version(
            project.id, b"vor", filename="vor.csv", kind="vor", new_document=True
        )
        return repo.create_run(project.id, vor.id, {"pdf_versions": {}})

    def test_thread_and_lock_keys_are_stable(self):
        run_id = uuid4()
        self.assertEqual(thread_id(run_id, 3, 1), f"{run_id}:3:1")
        self.assertEqual(advisory_key(run_id, 3), advisory_key(run_id, 3))
        self.assertNotEqual(advisory_key(run_id, 3), advisory_key(run_id, 4))

    def test_model_budget_counts_position_and_run_attempts(self):
        with tempfile.TemporaryDirectory(prefix="smeta-graph-db-") as tmp:
            repo = self._open(tmp)
            run = self._run(repo)
            first = reserve_model_call(
                repository=repo, run_id=run.id, position_index=0, request_sha="a" * 64,
                execution_revision=0, reserved_tokens=100,
                request_ref={"key": "request-1"}, position_cap=1, run_cap=2,
            )
            self.assertEqual(first["state"], "reserved")
            with self.assertRaises(ValueError):
                reserve_model_call(
                    repository=repo, run_id=run.id, position_index=0, request_sha="b" * 64,
                    execution_revision=0, reserved_tokens=100,
                    request_ref={"key": "request-2"}, position_cap=1, run_cap=2,
                )
            second = reserve_model_call(
                repository=repo, run_id=run.id, position_index=1, request_sha="c" * 64,
                execution_revision=0, reserved_tokens=100,
                request_ref={"key": "request-3"}, position_cap=1, run_cap=2,
            )
            record_model_receipt(
                repository=repo, call_id=second["id"], response_ref={"key": "response-3"},
                actual_tokens=42,
            )
            with self.assertRaises(ValueError):
                reserve_model_call(
                    repository=repo, run_id=run.id, position_index=2, request_sha="d" * 64,
                    execution_revision=0, reserved_tokens=100,
                    request_ref={"key": "request-4"}, position_cap=1, run_cap=2,
                )

    def test_failed_call_retries_on_same_row_and_consumes_budget(self):
        with tempfile.TemporaryDirectory(prefix="smeta-graph-db-") as tmp:
            repo = self._open(tmp)
            run = self._run(repo)
            first = reserve_model_call(
                repository=repo, run_id=run.id, position_index=0, request_sha="e" * 64,
                execution_revision=0, reserved_tokens=100,
                request_ref={"key": "request-5"}, position_cap=2, run_cap=2,
            )
            record_model_receipt(
                repository=repo, call_id=first["id"], response_ref={},
                state="outcome_unknown", error_code="RuntimeError")
            retry = reserve_model_call(
                repository=repo, run_id=run.id, position_index=0, request_sha="e" * 64,
                execution_revision=0, reserved_tokens=100,
                request_ref={"key": "request-5"}, position_cap=2, run_cap=2,
            )
            self.assertEqual(retry["id"], first["id"])
            self.assertEqual(retry["attempt_no"], 2)
            with self.assertRaises(ValueError):
                reserve_model_call(
                    repository=repo, run_id=run.id, position_index=0, request_sha="f" * 64,
                    execution_revision=0, reserved_tokens=100,
                    request_ref={"key": "request-6"}, position_cap=2, run_cap=2,
                )

    def test_publish_is_idempotent_per_revision(self):
        with tempfile.TemporaryDirectory(prefix="smeta-graph-db-") as tmp:
            repo = self._open(tmp)
            run = self._run(repo)
            task_id = uuid4()
            token = uuid4()
            repo._engine.begin()
            from sqlalchemy import insert
            from storage_models import TaskOutbox
            from datetime import datetime, timedelta, timezone

            with repo._engine.begin() as connection:
                connection.execute(insert(TaskOutbox).values(
                    id=task_id, run_id=run.id, task_key="graph:0", task_name="audit_tasks.audit_position",
                    payload={}, status="running", attempts=1, delivery_token=token,
                    lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                ))
            assessment = _assessment(run.id)
            self.assertTrue(publish_assessment(
                repository=repo, run_id=run.id, position_index=0,
                assessment=assessment, task_id=task_id, token=token,
            ))
            self.assertTrue(publish_assessment(
                repository=repo, run_id=run.id, position_index=0,
                assessment=assessment, task_id=task_id, token=token,
            ))
            loaded = repo.load_run(run.id)
            self.assertEqual(loaded["indexed_results"][0]["result"]["revision"], assessment["revision"])
            self.assertFalse(publish_assessment(
                repository=repo, run_id=run.id, position_index=0,
                assessment=assessment, task_id=task_id, token=uuid4(),
            ))
    def _delivery(self, repo, run):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import insert
        from storage_models import TaskOutbox

        task_id, token = uuid4(), uuid4()
        with repo._engine.begin() as connection:
            connection.execute(insert(TaskOutbox).values(
                id=task_id, run_id=run.id, task_key="graph:ask", task_name="audit_tasks.audit_position",
                payload={}, status="running", attempts=1, delivery_token=token,
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            ))
        return task_id, token

    def test_graph_ask_reuses_receipt_and_marks_unknown_transport(self):
        from audit_evidence_validation import AgentAnswer

        with tempfile.TemporaryDirectory(prefix="smeta-graph-db-") as tmp:
            repo = self._open(tmp)
            blobs = FileBlobStore(tmp)
            run = self._run(repo)
            task_id, token = self._delivery(repo, run)
            credentials = SimpleNamespace(model_name="test-model", base_url="https://example.test/v1", folder="folder")

            class _Observation:
                def update(self, *args, **kwargs):
                    return None

            class _Stage:
                def __enter__(self):
                    return _Observation()

                def __exit__(self, *args):
                    return False

            trace = SimpleNamespace(stage=lambda *args, **kwargs: _Stage())
            answer = AgentAnswer(summary="Готово", items=[], parameters=[])
            client = Mock()
            client.chat.completions.create.return_value = SimpleNamespace(
                id="resp-1", usage={"total": 10},
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=answer.model_dump_json()))],
            )
            stats: dict = {"requests": 0, "usage": {}, "responses": []}
            first, first_http = graph_ask(
                repository=repo, blob_store=blobs, run_id=run.id, position_index=0,
                execution_revision=0, task_id=task_id, token=token, client=client,
                credentials=credentials, instructions="Проверь.", payload={"sources": []},
                schema=AgentAnswer, trace=trace, stats=stats, position_cap=6, run_cap=6,
            )
            self.assertEqual(first.summary, "Готово")
            self.assertTrue(first_http)
            self.assertEqual(client.chat.completions.create.call_count, 1)
            second, second_http = graph_ask(
                repository=repo, blob_store=blobs, run_id=run.id, position_index=0,
                execution_revision=0, task_id=task_id, token=token, client=client,
                credentials=credentials, instructions="Проверь.", payload={"sources": []},
                schema=AgentAnswer, trace=trace, stats=stats, position_cap=6, run_cap=6,
            )
            self.assertEqual(second.summary, "Готово")
            self.assertFalse(second_http)
            self.assertEqual(client.chat.completions.create.call_count, 1)
            self.assertEqual(stats["requests"], 1)
            client.chat.completions.create.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                graph_ask(
                    repository=repo, blob_store=blobs, run_id=run.id, position_index=0,
                    execution_revision=0, task_id=task_id, token=token, client=client,
                    credentials=credentials, instructions="Проверь.", payload={"sources": [{"id": "other"}]},
                    schema=AgentAnswer, trace=trace, stats=stats, position_cap=6, run_cap=6,
                )
            from sqlalchemy import select
            from sqlalchemy.orm import Session
            from storage_models import AuditModelCall
            with Session(repo._engine) as session:
                states = sorted(row.state for row in session.scalars(select(AuditModelCall)).all())
            self.assertEqual(states, ["outcome_unknown", "responded"])


if __name__ == "__main__":
    unittest.main()
