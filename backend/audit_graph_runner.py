"""Execute one position graph with fencing, budgets and receipts."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from audit_assessment import reduce_assessment
from audit_graph import RECURSION_LIMIT, GraphContext, build_graph
from audit_graph_store import graph_ask, open_fenced_saver, publish_assessment, thread_id
from audit_models import VorPosition
from pdf_binding import index_designations, verify_designations
def run_graph_position(*, context: Any, run_id: UUID, position_index: int,
                       task_id: UUID, token: UUID, request_id: str = "",
                       execution_revision: int | None = None, validation_only: bool = False) -> dict[str, Any]:
    record = context.repository.load_run(run_id, include_results=False)
    job = context.repository.load_position_job(run_id, position_index)
    payload = dict(job.payload or {})
    if execution_revision is None:
        execution_revision = int(payload.get("execution_revision", 0))
    position = VorPosition(
        index=position_index,
        number=payload.get("number", ""),
        file_ref=payload.get("file_ref", ""),
        position_source=payload.get("position_source"),
        work_name=payload.get("work_name", "") or "position",
        volume=payload.get("volume", "") or "-",
        unit=payload.get("unit", "") or "-",
        reference=payload.get("reference", ""),
        page_ref=payload.get("page_ref", ""),
        comment=payload.get("comment", ""),
        formula=payload.get("formula", ""),
        position_type=payload.get("position_type", ""),
        calculation=payload.get("calculation", {}),
        formula_refs=dict(payload.get("formula_refs") or {}),
        explicit_refs=list(payload.get("explicit_refs") or []),
        designation_refs=list(payload.get("designation_refs") or []),
    )
    snapshot = payload.get("snapshot") or {}
    bind_error = payload.get("bind_error") or ""
    if bind_error or not snapshot:

        assessment = reduce_assessment({
            "run_id": str(run_id),
            "position_index": position_index,
            "execution_revision": execution_revision,
            "fingerprint": {"sha256": "bind-error"},
            "project": {"obligations": [{
                "id": "project:binding", "domain": "presence", "subject": "Привязка PDF",
                "state": "needs_facts", "gap": "pdf-binding",
                "reason": bind_error or "Подготовленный корпус отсутствует",
            }]},
            "normative": {"obligations": []},
            "relationships": {"obligations": []},
            "own_quantity": {"kind": "direct", "state": "unresolved", "unit": position.unit,
                             "declared_value": position.volume},
            "execution_state": "complete",
        }).model_dump(mode="json")
        published = publish_assessment(
            repository=context.repository, run_id=run_id, position_index=position_index,
            assessment=assessment, task_id=task_id, token=token)
        if published:
            context.repository.finish_position_job(run_id, position_index, "done", result=assessment)
        return {"position_index": position_index, "duplicate": False, "http_attempts": 0}

    from audit_snapshot import load_snapshot

    prepared = load_snapshot(context.blob_store, snapshot)
    if prepared.pending_ocr or not prepared.metadata.get("preparation_complete"):
        assessment = reduce_assessment({
            "run_id": str(run_id),
            "position_index": position_index,
            "execution_revision": execution_revision,
            "fingerprint": {"sha256": "preparation-incomplete"},
            "project": {"obligations": [{
                "id": "project:binding", "domain": "presence", "subject": "Подготовка ПД",
                "state": "needs_facts", "gap": "preparation-incomplete",
                "reason": "Подготовка ПД не завершена; проверка невозможна без полного корпуса.",
            }]},
            "normative": {"obligations": []},
            "relationships": {"obligations": []},
            "own_quantity": {"kind": "direct", "state": "unresolved", "unit": position.unit,
                             "declared_value": position.volume},
            "execution_state": "technical_failure",
            "error": "IncompletePreparation: подготовка ПД не завершена",
        }).model_dump(mode="json")
        published = publish_assessment(
            repository=context.repository, run_id=run_id, position_index=position_index,
            assessment=assessment, task_id=task_id, token=token)
        if published:
            context.repository.finish_position_job(run_id, position_index, "done", result=assessment)
        return {"position_index": position_index, "duplicate": False, "http_attempts": 0}
    _found, missing = verify_designations(
        list(position.designation_refs or []),
        index_designations(enumerate(prepared.texts, start=1)),
    )
    if missing:
        files = payload.get("pdf_name") or "загруженной ПД"
        absent = ", ".join(f"'{item}'" for item in missing)
        assessment = reduce_assessment({
            "run_id": str(run_id),
            "position_index": position_index,
            "execution_revision": 0,
            "fingerprint": {"sha256": "designation-gate"},
            "project": {"obligations": [{
                "id": "project:binding", "domain": "presence", "subject": "Обозначения ПД",
                "state": "possible_mismatch", "comparable": True, "mismatch_level": "proved",
                "reason": f"ВОР ссылается на {absent}, в {files} обозначения отсутствуют",
            }]},
            "normative": {"obligations": []},
            "relationships": {"obligations": []},
            "own_quantity": {"kind": "direct", "state": "unresolved", "unit": position.unit,
                             "declared_value": position.volume},
            "execution_state": "complete",
        }).model_dump(mode="json")
        published = publish_assessment(
            repository=context.repository, run_id=run_id, position_index=position_index,
            assessment=assessment, task_id=task_id, token=token)
        if published:
            context.repository.finish_position_job(run_id, position_index, "done", result=assessment)
        return {"position_index": position_index, "duplicate": False, "http_attempts": 0}
    supporting = {
        str(entry.get("version_id")): {"snapshot": entry.get("snapshot"), "filename": name}
        for name, entry in (payload.get("supporting_documents") or {}).items()
        if entry.get("snapshot") and entry.get("version_id")
    }
    database_url = os.environ.get("DATABASE_URL") or context.settings.database_url
    saver, connection = open_fenced_saver(
        database_url=database_url, repository=context.repository, run_id=run_id,
        position_index=position_index, task_id=task_id, token=token,
    )
    try:
        graph = build_graph(checkpointer=saver)
        credentials = context.settings.llm_credentials()
        from audit_llm import _check_llm_host
        _check_llm_host(credentials.base_url, tuple(context.settings.allowed_llm_hosts or ()))
        if not credentials.api_key:
            raise ValueError("Missing model configuration")
        from audit_tracing import TracingRun

        tracing = TracingRun(
            endpoint=context.settings.phoenix_collector_endpoint,
            project=context.settings.phoenix_project_name,
            environment=context.settings.phoenix_environment,
            audit_run_id=str(run_id), request_id=request_id,
            redact_pdf=context.settings.telemetry_redact_pdf,
            mask_prompts=context.settings.telemetry_mask_prompts,
            public_url=context.settings.phoenix_public_url,
        )
        trace = tracing.position(input={"position_index": position_index}, metadata={"position_index": position_index})
        stats: dict[str, Any] = {"requests": 0, "usage": {}, "responses": [], "warnings": []}
        config = record.get("config", {}) or {}
        selected = config.get("selected_indices") or [position_index]
        run_cap = int(config.get("max_http_attempts", 6 * len(selected)))
        position_cap = int(config.get("max_position_attempts", 6))
        client = (context.client_factory or __import__("openai").OpenAI)(
            api_key=credentials.api_key, base_url=credentials.base_url, timeout=60, max_retries=0)

        def ask(payload: dict[str, Any], schema: Any, response_sink: Any = None) -> Any:
            from audit_graph import GRAPH_INSTRUCTIONS, GRAPH_PROMPT_SCHEMA
            from normative_agent import TOOLS

            _ = response_sink
            return graph_ask(
                repository=context.repository, blob_store=context.blob_store, run_id=run_id,
                position_index=position_index, execution_revision=execution_revision, task_id=task_id, token=token,
                client=client, credentials=credentials, instructions=GRAPH_INSTRUCTIONS + TOOLS,
                payload=payload, schema=schema, trace=trace, stats=stats,
                position_cap=position_cap, run_cap=run_cap, prompt_schema=GRAPH_PROMPT_SCHEMA,
            )
        graph_context = GraphContext(
            repository=context.repository, blob_store=context.blob_store, position=position,
            record={"id": str(run_id), "config": config}, snapshot_refs=[snapshot],
            supporting=supporting, client_factory=context.client_factory, credentials=credentials,
            trace=trace, stats=stats, ask=ask, validation_only=validation_only,
        )
        try:
            result = graph.invoke(
                {"execution_revision": execution_revision}, context=graph_context,
                config={"configurable": {"thread_id": thread_id(run_id, position_index, execution_revision)},
                        "recursion_limit": RECURSION_LIMIT},
                durability="sync",
            )
        finally:
            tracing.close()
            try:
                client.close()
            except Exception:
                pass
        assessment = result.get("assessment") or {}
        published = publish_assessment(
            repository=context.repository, run_id=run_id, position_index=position_index,
            assessment=assessment, task_id=task_id, token=token,
        )
        if not published:
            return {"position_index": position_index, "fenced": True}
        context.repository.finish_position_job(run_id, position_index, "done", result=assessment)
        return {"position_index": position_index, "duplicate": False}
    finally:
        connection.close()


__all__ = ["run_graph_position"]
