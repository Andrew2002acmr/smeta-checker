"""Independent bounded normative follow-up; no mutation of matching verdicts."""
from workspace_models import NormativePositionCheckRequest
from analysis_replay import DEFAULT_QUESTION


def review_position(context, run_id, index, *, local_only=False):
    from normative_position_check import check, validate_saved
    from normative_local_checks import LOCAL_RULES_VERSION
    repo = context.repository
    record = repo.load_run(run_id)
    if record["status"] not in {"running", "done"}:
        return {"skipped": "run_stopped"}
    if not record["config"].get("normative_after_match"):
        return {"skipped": "disabled"}
    # Upgrade saved results locally; never repeat a paid request during recovery.
    latest = next((r for r in repo.normative_reviews(run_id, index) if r['kind'] == 'check'), None)
    if latest:
        if latest['payload']['position_check'].get('pipeline_version', 1) >= 2:
            return {'skipped': 'already_reviewed'}
        if latest['payload']['position_check'].get('local_rules_version', 0) >= LOCAL_RULES_VERSION:
            return {"skipped": "already_reviewed"}
        updated = validate_saved(context, record, index, latest['id'], 'automatic-local-refresh')
        return dict(review_id=updated['id'], local_only=True)
    if local_only:
        return {'skipped': 'no_saved_review'}
    if not any(r["position_index"] == index for r in record["indexed_results"]):
        return {"skipped": "no_matching_result"}
    result = check(context, record, index, NormativePositionCheckRequest(
        question=DEFAULT_QUESTION
    ), "automatic-after-matching", automatic=True)
    return {"review_id": result["id"]} if result else {"skipped": "insufficient_sources"}
