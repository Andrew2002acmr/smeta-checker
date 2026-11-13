"""Durable LangGraph execution: ownership fencing, budgets, and receipts."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy import select

from storage_models import AuditModelCall, PositionResult, Run, TaskOutbox


GRAPH_SCHEMA = "audit_graph"


def thread_id(run_id: Any, position_index: int, execution_revision: int) -> str:
    return f"{run_id}:{int(position_index)}:{int(execution_revision)}"


def advisory_key(run_id: Any, position_index: int) -> int:
    digest = hashlib.sha256(f"{run_id}:{int(position_index)}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def request_sha(prompt: str, schema: str, model: str, caps: str, aliases: dict[str, str],
                instructions: str = "") -> str:
    payload = json.dumps(
        {"prompt": prompt, "schema": schema, "model": model, "caps": caps,
         "aliases": aliases, "instructions": instructions},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class FencedPostgresSaver(PostgresSaver):
    """PostgresSaver that refuses writes without a live delivery lease."""

    def __init__(self, conn: Any, *, repository: Any, run_id: UUID, task_id: UUID, token: UUID) -> None:
        super().__init__(conn)
        self._fence_repository = repository
        self._fence_run_id = run_id
        self._fence_task_id = task_id
        self._fence_token = token
        self._fence_lock = threading.RLock()

    def _check_ownership(self) -> None:
        from sqlalchemy.orm import Session

        with Session(self._fence_repository._engine) as session:
            run = session.scalar(select(Run).where(Run.id == self._fence_run_id).with_for_update())
            row = session.scalar(select(TaskOutbox).where(TaskOutbox.id == self._fence_task_id).with_for_update())
            now = session.scalar(select(__import__("sqlalchemy").func.clock_timestamp()))
            if run is None or run.status != "running":
                raise PermissionError("Run is no longer running")
            if row is None or row.delivery_token != self._fence_token or row.status != "running":
                raise PermissionError("Delivery lease is no longer owned")
            lease_until = getattr(row, "lease_until", None)
            if lease_until is not None and now is not None and lease_until < now:
                raise PermissionError("Delivery lease expired")
            session.rollback()

    def put(self, *args: Any, **kwargs: Any) -> Any:
        with self._fence_lock:
            self._check_ownership()
            return super().put(*args, **kwargs)

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        with self._fence_lock:
            self._check_ownership()
            return super().put_writes(*args, **kwargs)


def open_fenced_saver(*, database_url: str, repository: Any, run_id: UUID, position_index: int,
                      task_id: UUID, token: UUID) -> tuple[FencedPostgresSaver, Any]:
    from sqlalchemy.engine import make_url

    dsn = make_url(database_url)._replace(drivername='postgresql').render_as_string(hide_password=False)
    connection = psycopg.connect(
        dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row,  # type: ignore[arg-type]
        options=f"-c search_path={GRAPH_SCHEMA},public",
    )
    key = advisory_key(run_id, position_index)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cursor.fetchone()
        locked = bool(row.get("pg_try_advisory_lock", False)) if isinstance(row, dict) else False
        if not locked:
            connection.close()
            raise PermissionError("Position is already owned by another worker")
    return FencedPostgresSaver(connection, repository=repository, run_id=run_id, task_id=task_id, token=token), connection


def graph_ask(*, repository: Any, blob_store: Any, run_id: UUID, position_index: int,
              execution_revision: int, task_id: UUID, token: UUID, client: Any,
              credentials: Any, instructions: str, payload: dict[str, Any], schema: Any,
              trace: Any, stats: dict[str, Any], position_cap: int, run_cap: int,
              output_cap: int = 8192, prompt_schema: str = "") -> tuple[Any, bool]:
    """Reserve, call and receipt one model attempt; unknown transport stays unknown.

    Returns ``(answer, did_http)``. Receipt reuse returns the stored answer
    with ``did_http=False`` so state counters never count a replayed receipt
    as a fresh HTTP attempt.
    """
    from sqlalchemy.orm import Session

    with Session(repository._engine) as session:
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        row = session.scalar(select(TaskOutbox).where(TaskOutbox.id == task_id).with_for_update())
        if run is None or run.status != "running" or row is None or row.delivery_token != token or row.status != "running":
            raise PermissionError("Delivery lease is no longer owned")
        session.rollback()
    from normative_context import ModelProviderQuotaError, _ask
    from normative_context_packing import serialize_context

    serialized = serialize_context(payload)
    reserved = len(serialized.encode("utf-8")) // 2 + output_cap
    sha = request_sha(serialized, schema.__name__, credentials.model_name,
                       f"{position_cap}/{run_cap}/{output_cap}", {},
                       (prompt_schema + "\n" + instructions) if prompt_schema else instructions)
    existing = None
    from sqlalchemy.orm import Session
    with Session(repository._engine) as session:
        existing = session.scalar(select(AuditModelCall).where(
            AuditModelCall.run_id == run_id, AuditModelCall.position_index == position_index,
            AuditModelCall.request_sha == sha, AuditModelCall.execution_revision == execution_revision))
    if existing is not None and existing.state == "responded" and existing.response_ref:
        stored_request = (existing.request_ref or {}).get("prompt_sha")
        if stored_request != sha or existing.request_sha != sha:
            raise ValueError("Stored model receipt does not match the current request")
        response = blob_store.get(existing.response_ref["key"])
        envelope = json.loads(response.decode("utf-8"))
        return schema.model_validate_json(envelope["text"]), False
    if existing is not None and existing.state == "reserved":
        call = {"id": str(existing.id), "attempt_no": existing.attempt_no}
    else:
        call = reserve_model_call(
            repository=repository, run_id=run_id, position_index=position_index, request_sha=sha,
            execution_revision=execution_revision, reserved_tokens=reserved,
            request_ref={"prompt_sha": sha}, position_cap=position_cap, run_cap=run_cap)
    receipts: list[dict[str, Any]] = []
    from normative_context import ModelProviderResponseError, ModelRateLimitError, _monthly_key_limit
    import time as _time

    def _throttled(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        if status in (429, "429"):
            return not _monthly_key_limit(status, body if isinstance(body, dict) else {})
        # Transient gateway failures (HTML error pages, overloaded
        # upstream): same backoff as throttling, same as the legacy
        # agent's transient class.
        if status in (500, 502, 503, 504, "500", "502", "503", "504"):
            return True
        if isinstance(exc, ModelProviderResponseError) and exc.code in (429, "429"):
            return True
        return False
    answer = None
    for attempt in range(4):
        try:
            answer = _ask(client, credentials, instructions, payload, schema, trace,
                           "graph-request-evidence", stats, response_sink=receipts.append)
            break
        except ModelProviderQuotaError:
            record_model_receipt(repository=repository, call_id=call["id"], response_ref={},
                                 state="failed", error_code="429")
            raise
        except Exception as exc:
            if _throttled(exc) and attempt < 3:
                _time.sleep((5, 15, 45)[attempt])
                continue
            if _throttled(exc):
                record_model_receipt(repository=repository, call_id=call["id"], response_ref={},
                                     state="failed", error_code="rate_limited")
                raise ModelRateLimitError() from exc
            record_model_receipt(repository=repository, call_id=call["id"], response_ref={},
                                 state="outcome_unknown", error_code=type(exc).__name__[:16])
            raise
    assert answer is not None
    envelope = receipts[0] if receipts else {"text": "", "usage": {}}
    stored = blob_store.put(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
    record_model_receipt(
        repository=repository, call_id=call["id"],
        response_ref={"key": stored.key, "sha256": stored.sha256},
        actual_tokens=int((envelope.get("usage") or {}).get("total", 0) or 0),
    )
    return answer, True


def reserve_model_call(*, repository: Any, run_id: UUID, position_index: int, request_sha: str,
                       execution_revision: int, reserved_tokens: int,
                       request_ref: dict[str, Any], position_cap: int, run_cap: int) -> dict[str, Any]:
    from sqlalchemy import func
    from sqlalchemy.orm import Session

    with Session(repository._engine) as session:
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.status != "running":
            raise PermissionError("Run is no longer running")
        position_calls = session.scalar(select(func.coalesce(func.sum(AuditModelCall.attempt_no), 0)).where(
            AuditModelCall.run_id == run_id, AuditModelCall.position_index == position_index)) or 0
        run_calls = session.scalar(select(func.coalesce(func.sum(AuditModelCall.attempt_no), 0)).where(
            AuditModelCall.run_id == run_id)) or 0
        if position_calls >= position_cap or run_calls >= run_cap:
            raise ValueError("Model call budget exhausted")
        row = session.scalar(select(AuditModelCall).where(
            AuditModelCall.run_id == run_id, AuditModelCall.position_index == position_index,
            AuditModelCall.request_sha == request_sha,
            AuditModelCall.execution_revision == execution_revision).with_for_update())
        if row is None:
            row = AuditModelCall(
                run_id=run_id, position_index=position_index, request_sha=request_sha,
                attempt_no=1, execution_revision=execution_revision, state="reserved",
                reserved_tokens=reserved_tokens, request_ref=dict(request_ref))
            session.add(row)
        elif row.state in {"failed", "outcome_unknown"}:
            row.attempt_no = int(row.attempt_no or 0) + 1
            row.execution_revision = execution_revision
            row.state = "reserved"
            row.error_code = None
            row.reserved_tokens = reserved_tokens
            row.request_ref = dict(request_ref)
        else:
            raise ValueError(f"Model call is already {row.state}")
        session.commit()
        session.refresh(row)
        return {"id": str(row.id), "state": row.state, "attempt_no": row.attempt_no}


def record_model_receipt(*, repository: Any, call_id: Any, response_ref: dict[str, Any],
                         actual_tokens: int | None = None, state: str = "responded",
                         error_code: str | None = None) -> None:
    from sqlalchemy.orm import Session

    with Session(repository._engine) as session:
        row = session.get(AuditModelCall, UUID(str(call_id)))
        if row is None:
            raise KeyError("Unknown model call")
        row.state = state
        row.error_code = error_code
        row.response_ref = dict(response_ref)
        if actual_tokens is not None:
            row.actual_tokens = actual_tokens
        session.commit()


def publish_assessment(*, repository: Any, run_id: UUID, position_index: int,
                       assessment: dict[str, Any], task_id: UUID, token: UUID) -> bool:
    """Store a canonical assessment once per revision; same revision is idempotent."""
    from sqlalchemy.orm import Session

    revision = assessment.get("revision")
    with Session(repository._engine) as session:
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        row = session.scalar(select(TaskOutbox).where(TaskOutbox.id == task_id).with_for_update())
        if run is None or run.status != "running":
            return False
        if row is None or row.delivery_token != token or row.status != "running":
            return False
        existing = session.scalar(select(PositionResult).where(
            PositionResult.run_id == run_id, PositionResult.position_index == position_index).with_for_update())
        if existing is not None and (existing.result or {}).get("revision") == revision:
            session.rollback()
            return True
        payload = dict(assessment)
        if existing is None:
            session.add(PositionResult(run_id=run_id, position_index=position_index, result=payload))
        else:
            existing.result = payload
        session.commit()
        return True


__all__ = [
    "GRAPH_SCHEMA",
    "FencedPostgresSaver",
    "advisory_key",
    "graph_ask",
    "open_fenced_saver",
    "publish_assessment",
    "record_model_receipt",
    "request_sha",
    "reserve_model_call",
    "thread_id",
]
