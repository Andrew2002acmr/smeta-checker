"""Commit evidence follow-up/dependency intents with the immutable review."""

from sqlalchemy import select

from analysis_replay import digest
from storage_models import NormativeReview, Run
from normative_evidence_followup import plan_for


def geometry_revision(payload):
    result = payload.get("position_check") or {}
    # A comparison mismatch does not revoke independently proved project
    # geometry. This revision is only an invalidation signal, never approval of
    # a binding or permission to transfer the parent's verdict to its children.
    from normative_parent_basis import _anchored_card, _proposal_dependencies
    from normative_parameter_feedback import literal_parameter_issues
    from workspace_models import NormativeParameterProposal
    from normative_parameters import measure
    from normative_source_identity import canonical_source_id

    sources = {s["id"]: s for s in result.get("sources", [])}
    proposals = []
    for raw in result.get("parameter_proposals", []):
        if raw.get("parameter") not in {"quantity", "thickness"}:
            continue
        try:
            proposal = NormativeParameterProposal.model_validate(raw)
            source = sources.get(proposal.source_id)
            if (source is None or proposal.scope_state != "verified"
                    or literal_parameter_issues(proposal, source)
                    or source.get("origin") == "uncertain_table"
                    or source.get("origin", "").startswith("ocr") and not source.get("numeric_verified")):
                continue
            dependencies = _proposal_dependencies(raw, sources)
            if not dependencies or any(canonical_source_id(sources[sid]) is None for sid in dependencies):
                continue
            proposals.append(raw)
        except (KeyError, TypeError, ValueError):
            continue
    cards = [
        {key: c.get(key) for key in ("id", "found_value", "found_unit", "source_ids", "source_anchors")}
        for c in result.get("parameter_cards", [])
        if c.get("id") in {"quantity", "thickness", "input:area"}
        and c.get("state") in {"confirmed", "possible_mismatch"}
        and _anchored_card(c, sources)
        and any(p["source_id"] in c.get("source_ids", [])
            and measure(p["value"], p["unit"]) == measure(c.get("found_value"), c.get("found_unit", ""))
            and (p["parameter"] == c["id"] or c["id"] == "input:area" and p["parameter"] == "quantity")
            for p in proposals)
    ]
    ids = {sid for c in cards for sid in c.get("source_ids", [])}
    proposals = [
        p for p in proposals if p.get("source_id") in ids
    ]
    ids.update(sid for p in proposals for sid in p.get("operand_source_ids", []))
    return digest(
        dict(
            cards=cards,
            proposals=proposals,
            sources=[s for s in result.get("sources", []) if s["id"] in ids],
            execution=result.get("execution_state"),
            kb=payload.get("kb"),
            vor_version=payload.get("vor_version_id"),
            position_key=payload.get("position_key"),
        )
    )


def after_review(repo, session, run_id, index, payload, previous):
    result = payload.get("position_check") or {}
    if result.get("pipeline_version", 0) < 6:
        return
    run = session.get(Run, run_id)
    if (
        run is None
        or run.status not in {"running", "done"}
        or not (run.config or {}).get("normative_after_match")
    ):
        return
    plan = plan_for(payload)
    if plan and (run.config or {}).get("normative_evidence_followups", 1):
        # One additional pass per position/run, even after local refreshes or
        # worker redelivery. Technical failure never creates another paid retry.
        repo._enqueue(
            session,
            run_id,
            f"normative-followup:{index}",
            "audit_tasks.normative_position",
            dict(run_id=str(run_id), position_index=index, evidence_followup=True),
        )
    revision = geometry_revision(payload)
    if previous is not None and revision == geometry_revision(previous):
        return
    # Only existing formal work->material dependencies can inherit geometry.
    # Structural neighbours and repeated titles on other sites cannot.
    children = {
        v["material_index"]
        for v in (payload.get("work_relations") or {}).get("children", [])
    }
    for child in children:
        row = session.scalar(
            select(NormativeReview)
            .where(
                NormativeReview.run_id == run_id,
                NormativeReview.position_index == child,
                NormativeReview.kind == "check",
            )
            .order_by(NormativeReview.created_at.desc(), NormativeReview.id.desc())
            .limit(1)
        )
        if (
            row is None
            or row.payload.get("position_check", {}).get("pipeline_version", 0) < 6
        ):
            continue
        if (row.payload.get("work_relations", {}).get("parent") or {}).get(
            "index"
        ) != index:
            continue
        repo._enqueue(
            session,
            run_id,
            f"normative-dependency:{child}:{revision[:48]}",
            "audit_tasks.normative_position",
            dict(
                run_id=str(run_id),
                position_index=child,
                local_only=True,
                dependency_revision=revision,
            ),
        )
