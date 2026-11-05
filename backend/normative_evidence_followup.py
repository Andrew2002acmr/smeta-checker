"""A bounded second research pass over missing obligations, never an OCR retry."""

from copy import deepcopy

from analysis_replay import DEFAULT_QUESTION, digest


def plan_for(payload):
    result = payload.get("position_check") or {}
    if (
        result.get("pipeline_version", 0) < 6
        or result.get("execution_state") != "complete"
        or result.get("state") not in {"needs_facts", "possible_mismatch"}
        or (result.get("coverage") or {}).get("evidence_followup")
    ):
        return None
    plans = [
        e
        for e in result.get("evidence_trace", [])
        if e.get("kind") in {"evidence_work_plan_v1", "evidence_work_plan_v2"}
    ]
    if not plans:
        return None
    def still_open(action):
        key = action.get("key", "")
        if key.startswith("card:"):
            selected = [c for c in result.get("parameter_cards", []) if c.get("id") == key.removeprefix("card:")]
        elif key.startswith("claim:"):
            selected = [i for i in result.get("items", []) if key.removeprefix("claim:") in i.get("claim_ids", [])]
        else:
            selected = []
        # An established mismatch is an outcome, not a request to search until
        # the value agrees. Other open assertions of that position still need
        # their own proof even when its overall state is possible_mismatch.
        return not selected or any(item.get("state") not in {"confirmed", "possible_mismatch"} for item in selected)

    # A dependent parent's result and image verification are separate jobs.
    # Do not spend a model request reasserting that those are unavailable.
    actions = [
        a
        for a in plans[-1]["actions"]
        if a["action"] not in {"resolve_parent_basis", "verify_source_quality"} and still_open(a)
    ]
    # Establish project operands before spending the bounded pass on detailed
    # normative conditions. Never rank by a numerical match to VOR.
    priority = {
        "project_scope": 0,
        "formula_operand": 1,
        "numeric_basis": 2,
        "norm_selection": 3,
        "consumption_basis": 4,
        "material_or_condition": 5,
        "work_method": 6,
    }
    actions = sorted(actions, key=lambda a: priority.get(a.get("family"), 7))[:8]
    if not actions:
        return None
    actions = deepcopy(actions)
    expected = {"project_scope": "project_scope_or_quantity", "formula_operand": "calculation_operand",
        "numeric_basis": "literal_numeric_basis", "norm_selection": "norm_and_applicability_condition",
        "consumption_basis": "material_consumption_basis", "material_or_condition": "material_or_condition",
        "work_method": "project_work_method"}
    for action in actions:
        action["goal"] = action["key"]
        action["expected_evidence_type"] = expected.get(action.get("family"), "claim_evidence")
        action["budget"] = dict(shared_pass=True, additional_model_calls_limit=3)
    return dict(
        schema=2,
        actions=actions,
        input_fingerprint=digest(actions),
        additional_calls_limit=3,
        budget=dict(additional_model_calls_limit=3, max_actions=8, automatic_passes_limit=1),
        completion_reason="pending",
        instructions="Проверь открытые аспекты целевыми чтениями. Сохрани независимые "
        "подтверждённые факты. Не повторяй прежний поиск без нового запроса или чтения. "
        "Для read_registered_source используй сохранённые read_requests: источник уже найден, "
        "но его полный текст не был передан в последнем успешно завершённом контексте. "
        "Для verify_applicability_conditions проверь pending_condition_ids условного расчётного "
        "кандидата. Его итог не требуется искать буквальной цитатой; вычисление не доказывает "
        "применимость и не подтверждает зависимое количество. Отсутствие проектного расхода "
        "нельзя устанавливать одним неудачным поиском. "
        "Граф ВОР задаёт кандидатов, а не доказательство. Повторный OCR запрещён.",
    )


def question(plan):
    # Stable across runs, unlike review UUIDs. Exact-input replay can reuse this
    # completed pass separately from the initial bounded research attempt.
    return (
        DEFAULT_QUESTION
        + "\nЦелевой поиск недостающих оснований: "
        + plan["input_fingerprint"]
    )


def preserve_completed(continuation, failed):
    """A failed extra search cannot erase the completed initial evidence.

    The previous verdict is preserved, never promoted. The failed attempt has
    its own execution state, telemetry and link; it is not a cacheable answer.
    """
    previous=deepcopy(continuation['review']['payload']['position_check'])
    if previous.get('execution_state')!='complete':
        raise ValueError('Completed initial evidence required for follow-up recovery')
    failure=dict(execution_state='technical_failure',original_review_id=str(continuation['review']['id']),
        summary=failed['summary'],telemetry=deepcopy(failed['telemetry']),
        coverage=deepcopy(failed.get('coverage') or {}))
    previous.setdefault('coverage',{})['evidence_followup_failure']=failure
    previous['telemetry']=deepcopy(failed['telemetry'])
    previous.setdefault('evidence_trace',[]).append(dict(kind='evidence_followup_failure_v1',
        original_review_id=failure['original_review_id'],execution_state='technical_failure',
        previous_state=previous.get('state'),requests=failed['telemetry']['requests']))
    return previous


def complete_followup(result, continuation):
    """Record a bounded search outcome without treating a search as evidence.

    Completion describes the attempt; claim/card states remain the ordinary
    validators' decisions. A technical failure preserves the initial facts and
    consumes this one automatic pass rather than starting an unbounded retry.
    """
    previous = continuation["review"]["payload"]["position_check"]
    plan = continuation["plan"]
    before_ids = {s["id"] for s in previous.get("sources", [])}
    current_ids = {s["id"] for s in result.get("sources", [])}
    failure = (result.get("coverage") or {}).get("evidence_followup_failure")
    cards = {c["id"]: c for c in result.get("parameter_cards", [])}
    outcomes = []
    for action in plan["actions"]:
        key = action["key"]
        if key.startswith("card:"):
            selected = [cards[key.removeprefix("card:")]] if key.removeprefix("card:") in cards else []
            source_ids = {sid for c in selected for sid in c.get("source_ids", [])}
        elif key.startswith("claim:"):
            claim = key.removeprefix("claim:")
            selected = [item for item in result.get("items", []) if claim in item.get("claim_ids", [])]
            source_ids = {sid for item in selected
                for field in ("project_source_ids", "normative_source_ids") for sid in item.get(field, [])}
        else:
            selected, source_ids = [], set()
        states = {item.get("state") for item in selected}
        state = ("technical_failure" if failure else "possible_mismatch" if "possible_mismatch" in states
            else "confirmed" if states == {"confirmed"} else "needs_facts")
        outcomes.append(dict(goal=action.get("goal", key),
            expected_evidence_type=action.get("expected_evidence_type", "claim_evidence"),
            retry_fingerprint=action["retry_fingerprint"], state=state,
            source_ids=sorted(source_ids & current_ids),
            new_source_ids=sorted(source_ids & (current_ids - before_ids)),
            reason=("Дополнительный поиск завершился техническим сбоем; исходные факты сохранены."
                if failure else "Основания проверены обычными валидаторами."
                if state in {"confirmed", "possible_mismatch"} else
                "После ограниченного поиска доказательств для этого утверждения недостаточно.")))
    result.setdefault("coverage", {})["evidence_followup"] = dict(
        round=1, origin_review_id=str(continuation["review"]["id"]),
        attempted_fingerprints=[a["retry_fingerprint"] for a in plan["actions"]],
        budget=deepcopy(plan.get("budget") or dict(additional_model_calls_limit=3, max_actions=8,
            automatic_passes_limit=1)),
        completion_reason=("technical_failure_initial_evidence_preserved" if failure
            else "completed_with_open_obligations" if any(o["state"] == "needs_facts" for o in outcomes)
            else "completed_with_validated_outcomes"),
        new_source_ids=sorted(current_ids - before_ids), results=outcomes,
        automatic_retry_allowed=False,
    )
    return result


def _resume_tool_receipts(tools, result, origin_review_id):
    """Restore bounded navigation archives, never document evidence or claims."""
    archive = getattr(tools, "result_archive", {})
    tools.result_archive = archive
    imported, rejected = 0, 0
    for event in result.get("evidence_trace", []):
        candidate = event.get("result") if isinstance(event, dict) else None
        if not isinstance(candidate, dict) or "result_handle" not in candidate:
            continue
        handle = candidate["result_handle"]
        original = {key: value for key, value in candidate.items()
            if key not in {"result_handle", "archive_read_request"}}
        if not isinstance(handle, str) or handle != "tool-result:" + digest(original)[:32]:
            rejected += 1
            continue
        if handle in archive:
            if archive[handle] != original:
                raise ValueError("Follow-up tool result identity collision")
            continue
        if len(archive) >= 256:
            rejected += 1
            continue
        archive[handle] = deepcopy(original)
        tools.events.append(dict(kind="resumed_tool_result_v1", origin_review_id=str(origin_review_id),
            request=deepcopy(event.get("request") or {}),
            result=dict(deepcopy(original), result_handle=handle,
                archive_read_request=dict(tool="read_source", source_id=handle, offset=0))))
        imported += 1
    tools.coverage["resumed_tool_results"] = dict(imported=imported, rejected=rejected,
        navigation_only=True, document_evidence=False, limit=256)


def resume_evidence(tools, continuation, record, position, kb):
    from normative_service import position_key
    from normative_parent_basis import _proposal_dependencies
    from normative_fact_ledger import supply

    payload = continuation["review"]["payload"]
    if (
        payload.get("position_key") != position_key(position)
        or payload.get("vor_version_id") != str(record["vor_version_id"])
        or payload.get("kb") != kb
    ):
        raise ValueError("Follow-up evidence binding changed")
    result = payload["position_check"]
    pins = set(record["config"].get("pdf_versions", {}).values())
    sources = result.get("sources", [])
    if any(s.get("version_id") not in pins for s in sources if s["kind"] == "project"):
        raise ValueError("Follow-up PDF pins changed")
    available = {s["id"]: s for s in sources}
    # Source IDs can already occur in initial deterministic preflight. They
    # must still refer to the exact same immutable content.
    current = {s["id"]: s for s in tools.sources}
    from normative_source_registry import rect_order_equivalent
    if any(s["id"] in current and not rect_order_equivalent(current[s["id"]],s) for s in sources):
        raise ValueError("Follow-up source ID collision")
    for source in sources:
        existing=current.get(source['id'])
        if existing is not None and existing!=source:
            # Retain the frozen bytes and citation hashes. Rectangle ordering
            # only affects painting; it is not a different numerical source.
            tools.sources[tools.sources.index(existing)]=deepcopy(source)
            tools.events.append(dict(kind='source_geometry_order_normalized',source_id=source['id']))
    candidates = [
        dict(proposal=deepcopy(p))
        for p in result.get("parameter_proposals", [])
        if p.get("source_id") in available and _proposal_dependencies(p, available)
    ]
    supply(tools, dict(sources=sources, candidates=candidates))
    _resume_tool_receipts(tools, result, continuation["review"]["id"])
    tools.parameter_evidence = {
        digest(c["proposal"]): c["proposal"] for c in candidates
    }
    tools.allow_late_repair = False
    tools.coverage["evidence_followup"] = dict(
        round=1,
        origin_review_id=str(continuation["review"]["id"]),
        attempted_fingerprints=[
            a["retry_fingerprint"] for a in continuation["plan"]["actions"]
        ],
        completion_reason="in_progress",
    )
    tools.events.append(
        dict(
            kind="evidence_followup_v1",
            plan=deepcopy(continuation["plan"]),
            origin_review_id=str(continuation["review"]["id"]),
        )
    )
