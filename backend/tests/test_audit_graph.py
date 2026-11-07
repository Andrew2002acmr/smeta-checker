"""The explicit graph routes gaps, budgets and validated facts only."""
from types import SimpleNamespace

from audit_graph import GraphContext, build_graph
from audit_models import VorPosition


def context(**overrides):
    position = VorPosition(index=0, work_name="Устройство основания", volume="10", unit="м2")
    payload = {
        "repository": None,
        "blob_store": None,
        "position": position,
        "record": {"id": "run-1", "config": {"pdf_versions": {"doc": "pdf-1"}}},
        "snapshot_refs": [{"key": "primary"}],
        "supporting": {},
        "validation_only": True,
    }
    payload.update(overrides)
    return GraphContext(**payload)

def test_validation_only_path_needs_no_model(monkeypatch):
    index = SimpleNamespace(texts=["площадь"], tables=[[]], errors={}, pending_ocr=[], quality={},
                            metadata={"preparation_complete": True, "document_version_id": "pdf-1"})
    monkeypatch.setattr("audit_snapshot.load_snapshot", lambda _store, _ref: index)
    monkeypatch.setattr("audit_evidence_tools.EvidenceTools.from_prepared",
                        lambda _index, _version, filename="x": SimpleNamespace(
                            sources=[], events=[], preferred=[], result_archive={}, quantity_sums=[],
                            coverage={}, table_search_done=False, project_search_results={},
                            filename=filename, version="pdf-1,",
                        ))
    graph = build_graph()
    result = graph.invoke({}, context=context(), config={"recursion_limit": 96})
    assert result["assessment"]["schema_version"] == "evidence-assessment-v1"
    assert result["assessment"]["execution"]["state"] == "complete"
    assert result["execution_state"] == "complete"


def test_incomplete_preparation_fails_closed(monkeypatch):
    index = SimpleNamespace(texts=["площадь"], tables=[[]], errors={}, pending_ocr=[0], quality={}, metadata={"preparation_complete": False})
    monkeypatch.setattr("audit_snapshot.load_snapshot", lambda _store, _ref: index)
    graph = build_graph()
    try:
        graph.invoke({}, context=context(), config={"recursion_limit": 96})
    except ValueError as exc:
        assert "incomplete" in str(exc).lower()
    else:
        raise AssertionError("incomplete preparation must fail")


def test_route_gap_honours_budgets_and_gaps():
    from audit_graph import route_gap

    runtime = SimpleNamespace(context=context(validation_only=False))
    assert route_gap({"gap_ids": [], "http_attempts": 0, "tool_executions": 0}, runtime) == "publish"
    assert route_gap({"gap_ids": ["a"], "http_attempts": 6, "tool_executions": 0}, runtime) == "publish"
    assert route_gap({"gap_ids": ["a"], "http_attempts": 0, "request_rounds": 6,
                      "tool_executions": 0}, runtime) == "publish"
    assert route_gap({"gap_ids": ["a"], "http_attempts": 0, "tool_executions": 32}, runtime) == "publish"
    assert route_gap({"gap_ids": ["a"], "http_attempts": 0, "tool_executions": 0}, runtime) == "request_evidence"


def test_request_evidence_without_model_stops_cleanly():
    from audit_graph import request_evidence

    runtime = SimpleNamespace(context=context(validation_only=False))
    result = request_evidence({"http_attempts": 0}, runtime)
    assert result["stop_reason"] == "model_unavailable"


def test_model_failure_becomes_technical_failure_not_lost_result(monkeypatch):
    from audit_graph import publish, request_evidence
    from normative_context import ModelEmptyResponseError

    def failing_ask(payload, schema, response_sink=None):
        raise ModelEmptyResponseError()

    runtime = SimpleNamespace(context=context(
        validation_only=False, ask=failing_ask,
        client_factory=object(), credentials=object()))
    monkeypatch.setattr("audit_graph._model_payload", lambda context, state: {"payload": {}, "aliases": {}})
    failed = request_evidence({"http_attempts": 0, "iterations_without_progress": 0}, runtime)
    assert failed["http_attempts"] == 1
    assert failed["stop_reason"] == "model_failure"
    assert "ModelEmptyResponseError" in failed["execution_error"]
    published = publish({
        "run_id": "run-1", "position_index": 0, "execution_revision": 0,
        "fingerprint": {"sha256": "x"},
        "obligations": {"project": [{"id": "project:binding", "domain": "presence",
            "subject": "Привязка", "required": True, "policy": "blocking", "state": "pending"}],
            "normative": [], "relationships": []},
        "own_quantity": {"kind": "direct", "state": "unresolved", "unit": "м2", "declared_value": "10"},
        "facts": [], "sources": [], "http_attempts": 1, "tool_executions": 0,
        "stop_reason": "model_failure", "execution_error": failed["execution_error"],
    }, SimpleNamespace(context=None))
    assert published["assessment"]["execution"]["state"] == "technical_failure"
    assert published["assessment"]["completion"] == "incomplete"


def test_receipt_reuse_does_not_consume_http_budget(monkeypatch):
    from audit_evidence_validation import AgentAnswer
    from audit_graph import request_evidence

    answer = AgentAnswer(summary="Повтор", items=[], parameters=[])

    def reuse_ask(payload, schema, response_sink=None):
        _ = payload, schema, response_sink
        return answer, False

    runtime = SimpleNamespace(context=context(
        validation_only=False, ask=reuse_ask,
        client_factory=object(), credentials=object()))
    monkeypatch.setattr("audit_graph._model_payload",
                        lambda context, state: {"payload": {}, "aliases": {}})
    monkeypatch.setattr("audit_graph.restore_source_ids", lambda answer, aliases, sources: answer)
    monkeypatch.setattr("audit_graph._restore_tools", lambda context, state: SimpleNamespace(sources=[]))
    result = request_evidence({"http_attempts": 5, "iterations_without_progress": 0}, runtime)
    assert result["http_attempts"] == 5

def test_duplicate_items_do_not_reset_stall_counter(monkeypatch):
    from audit_evidence_validation import AgentAnswer
    from audit_graph import request_evidence

    item = {"topic": "Наличие", "state": "confirmed", "explanation": "Есть.",
            "claim_ids": ["presence"], "project_source_ids": ["s1"],
            "normative_source_ids": []}
    answer = AgentAnswer(summary="Повтор", items=[item], parameters=[])

    def reuse_ask(payload, schema, response_sink=None):
        _ = payload, schema, response_sink
        return answer, False

    runtime = SimpleNamespace(context=context(
        validation_only=False, ask=reuse_ask,
        client_factory=object(), credentials=object()))
    monkeypatch.setattr("audit_graph._model_payload",
                        lambda context, state: {"payload": {}, "aliases": {}})
    monkeypatch.setattr("audit_graph.restore_source_ids", lambda answer, aliases, sources: answer)
    monkeypatch.setattr("audit_graph._restore_tools", lambda context, state: SimpleNamespace(sources=[]))
    first = request_evidence({"http_attempts": 0, "request_rounds": 0,
                              "items": [], "iterations_without_progress": 0}, runtime)
    assert first["iterations_without_progress"] == 0
    assert first["request_rounds"] == 1
    second = request_evidence({"http_attempts": 0, "request_rounds": 1,
                               "items": first["items"], "iterations_without_progress": 0}, runtime)
    assert second["iterations_without_progress"] == 1
    assert second["request_rounds"] == 2
    assert len(second["items"]) == 1

def test_read_evidence_keeps_unprocessed_requests_and_full_fingerprint(monkeypatch):
    import hashlib
    import json as _json
    from audit_evidence_tools import EvidenceRequest
    from audit_graph import read_evidence

    calls = []

    class Tools:
        sources = []

        def execute(self, request):
            calls.append(request.model_dump(mode="json"))
            return {"ok": True}

    tools = Tools()
    monkeypatch.setattr("audit_graph._restore_tools", lambda context, state: tools)
    monkeypatch.setattr("audit_graph.tool_bundle", lambda tools: {"version": 1})
    first = EvidenceRequest(tool="read_page", page=1).model_dump(mode="json")
    second = EvidenceRequest(tool="read_page", page=2).model_dump(mode="json")
    runtime = SimpleNamespace(context=context(validation_only=False))
    result = read_evidence({
        "pending_requests": [first, second], "processed_requests": [], "tool_executions": 31,
        "iterations_without_progress": 0,
    }, runtime)
    assert result["tool_executions"] == 32
    assert len(result["pending_requests"]) == 1
    assert result["pending_requests"][0]["page"] == 2
    expected = hashlib.sha256(_json.dumps(
        EvidenceRequest.model_validate(first).model_dump(mode="json"),
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert result["processed_requests"] == [expected]

def test_derive_quantities_links_technological_layers_from_run_inputs():
    from audit_graph import _related_positions, _run_positions, derive_quantities
    from audit_models import VorPosition

    def source(row):
        return {"document_sha256": "abc", "format": "xlsx", "sheet": "ВР", "row": row,
                "header_row": 15, "position_id": f"Ф{row}",
                "sections": [{"address": "A17", "text": "Раздел 1"}]}
    first = VorPosition(index=0, work_name="Устройство нижнего слоя основания из ЩПС, h = 12 см (слой 1)",
                        volume="431.61", unit="м2", position_type="Работа", position_source=source(18))
    second = VorPosition(index=1, work_name="Устройство нижнего слоя основания из ЩПС, h = 12 см (слой 2)",
                         volume="431.61", unit="м2", position_type="Работа", position_source=source(20))
    record = {"id": "run-1", "config": {"position_inputs": [
        first.model_dump(mode="json"), second.model_dump(mode="json")]},
    }
    runtime = SimpleNamespace(context=context(position=first, record=record, validation_only=False))
    related = _related_positions(runtime.context)
    assert [entry["index"] for entry in related] == [1]
    assert set(_run_positions(runtime.context)) == {0, 1}
    from normative_local_checks import layer_group

    group = layer_group(first, related)
    assert group is not None and group["height"] == group["first"] + group["second"]
    result = derive_quantities({"proposals": [], "sources": [], "quantity_kind": "direct"}, runtime)
    assert result["own_quantity"]["state"] == "unresolved"

def test_unit_item_does_not_close_binding_or_quantity(monkeypatch):
    from audit_graph import validate_evidence

    sources = [{"id": "s1", "kind": "project", "page": 89, "text": "Площадь 431,61 м2", "origin": "native"}]
    tools = SimpleNamespace(sources=list(sources), preferred=[], events=[])
    monkeypatch.setattr("audit_graph._restore_tools", lambda context, state: tools)
    monkeypatch.setattr("audit_graph.tool_bundle", lambda tools: {"version": 1})
    monkeypatch.setattr("audit_graph.answer_feedback", lambda *args, **kwargs: [])
    runtime = SimpleNamespace(context=context(validation_only=False))
    obligations = {"project": [
        {"id": "project:binding", "domain": "presence", "subject": "Привязка",
         "required": True, "policy": "blocking", "state": "pending"},
        {"id": "project:unit", "domain": "unit", "subject": "Единица",
         "required": True, "policy": "blocking", "state": "pending"},
        {"id": "project:own_quantity_inputs", "domain": "quantity", "subject": "Входы",
         "required": True, "policy": "blocking", "state": "pending"},
    ]}
    items = [{
        "topic": "Единица", "state": "confirmed", "explanation": "Единица м2.",
        "claim_ids": ["unit"], "project_source_ids": ["s1"], "normative_source_ids": [],
    }]
    proposals = [{
        "parameter": "quantity", "operation": "direct", "value": "431.61", "unit": "м2",
        "source_id": "s1", "operand_source_ids": [], "quote": "Площадь 431,61 м2",
        "scope_state": "unresolved", "scope_quote": "",
    }]
    out = validate_evidence(
        {"proposals": proposals, "items": items, "facts": [], "obligations": obligations,
         "gap_ids": ["project:binding", "project:unit", "project:own_quantity_inputs"]},
        runtime)
    states = {o["id"]: o["state"] for o in out["obligations"]["project"]}
    # Unit closes via the literal fact; binding still needs subject scope.
    assert states["project:unit"] == "confirmed"
    assert states["project:binding"] == "pending"
    assert states["project:own_quantity_inputs"] == "confirmed"

def test_thickness_issue_does_not_invalidate_quantity_fact(monkeypatch):
    from audit_graph import validate_evidence

    sources = [{"id": "s1", "kind": "project", "page": 89, "text": "Площадь 431,61 м2", "origin": "native"}]
    tools = SimpleNamespace(sources=list(sources), preferred=[], events=[])
    monkeypatch.setattr("audit_graph._restore_tools", lambda context, state: tools)
    monkeypatch.setattr("audit_graph.tool_bundle", lambda tools: {"version": 1})
    monkeypatch.setattr("audit_graph.answer_feedback",
                        lambda answer, sources, tools, **kwargs: ["Параметр thickness: толщина не подтверждена."])
    runtime = SimpleNamespace(context=context(validation_only=False))
    proposals = [
        {"parameter": "quantity", "operation": "direct", "value": "431.61", "unit": "м2",
         "source_id": "s1", "operand_source_ids": [], "quote": "Площадь 431,61 м2",
         "scope_state": "verified", "scope_quote": "Площадь 431,61 м2"},
        {"parameter": "thickness", "operation": "direct", "value": "24", "unit": "см",
         "source_id": "s1", "operand_source_ids": [], "quote": "h=24",
         "scope_state": "unresolved", "scope_quote": ""},
    ]
    out = validate_evidence(
        {"proposals": proposals, "items": [], "facts": [],
         "obligations": {"project": []}, "gap_ids": []},
        runtime)
    states = {(fact["parameter"], fact["literal_state"]) for fact in out["facts"]}
    assert ("quantity", "verified") in states
    assert ("thickness", "unresolved") in states
