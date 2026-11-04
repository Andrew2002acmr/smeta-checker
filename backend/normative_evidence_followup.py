"""A bounded second research pass over missing obligations, never an OCR retry."""

from copy import deepcopy

from analysis_replay import DEFAULT_QUESTION, digest


def plan_for(payload):
    result = payload.get("position_check") or {}
    if (
        result.get("pipeline_version", 0) < 6
        or result.get("execution_state") != "complete"
        or result.get("state") != "needs_facts"
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
    # A dependent parent's result and image verification are separate jobs.
    # Do not spend a model request reasserting that those are unavailable.
    actions = [
        a
        for a in plans[-1]["actions"]
        if a["action"] not in {"resolve_parent_basis", "verify_source_quality"}
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
    return dict(
        schema=1,
        actions=deepcopy(actions),
        input_fingerprint=digest(actions),
        additional_calls_limit=3,
        instructions="Проверь открытые аспекты целевыми чтениями. Сохрани независимые "
        "подтверждённые факты. Не повторяй прежний поиск без нового запроса или чтения. "
        "Для read_registered_source используй сохранённые read_requests: источник уже найден, "
        "но его полный текст не был передан в последнем успешно завершённом контексте. "
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
    )
    tools.events.append(
        dict(
            kind="evidence_followup_v1",
            plan=deepcopy(continuation["plan"]),
            origin_review_id=str(continuation["review"]["id"]),
        )
    )
