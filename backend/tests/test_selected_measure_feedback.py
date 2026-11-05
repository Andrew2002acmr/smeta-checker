"""Explain a selected unit's unknown scope without certifying any premise."""

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from audit_models import VorPosition
from normative_thickness_adjustment import feedback, findings, apply_guard
from test_normative_thickness_adjustment import asphalt_case, selected_adjustment


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "missing_adjustment",
        "wrong_steps",
        "wrong_quote",
        "wrong_unit",
        "wrong_source",
    ],
)
def test_scope_feedback_names_actual_missing_fields_and_does_not_approve(change):
    position, result = cast(Any, asphalt_case())
    selected_adjustment(result)
    result["parameter_proposals"][2].update(scope_state="unresolved", scope_quote="")
    if change == "missing_adjustment":
        result["evidence_trace"] = []
    elif change == "wrong_steps":
        result["evidence_trace"][0]["signed_steps"] = -2
    elif change == "wrong_quote":
        result["evidence_trace"][0]["adjustment_quote"] = "Invented quote"
    elif change == "wrong_unit":
        result["sources"][3]["text"] = result["sources"][3]["text"].replace(
            "1000 м2", "1000 м3"
        )
    elif change == "wrong_source":
        result["evidence_trace"][0]["adjustment_source_id"] = "invented"
    before = deepcopy(result)
    obligations = findings(position, result, {position.index: position}, {})
    message = " ".join(feedback(position, result, {position.index: position}, {}))
    assert "unit_scale.source_id=base" in message and "27-06-031-01" in message
    assert (
        "scope_state остаётся unresolved" in message
        and "scope_quote отсутствует" in message
    )
    if change == "missing_adjustment":
        assert "Явная поправка ещё не передана" in message
    else:
        assert "Исходные adjustments для этой базы уже переданы (1)" in message
        assert "перепроверь" in message
    assert result == before
    assert findings(position, result, {position.index: position}, {}) == obligations
    assert apply_guard(position, result, {position.index: position}, {})
    assert any(
        i["state"] == "needs_facts" and "normative:applicability" in i["claim_ids"]
        for i in result["items"]
    )


def test_verified_measure_retains_existing_adjustment_feedback():
    position, result = cast(Any, asphalt_case())
    message = " ".join(feedback(position, result, {position.index: position}, {}))
    assert "Нужна явно выбранная поправка" in message
    assert "unverified_selected_measure" not in message


@pytest.mark.parametrize("index", [4, 6])
def test_actual_saved_work_reproducer_keeps_unknown_and_explains_selected_measure(
    index,
):
    backend = Path(__file__).resolve().parents[1]
    pilot = backend / ".audit_cache/pilot-20261103"
    path = pilot / f"codex-pipeline-20261104/new-{index}-final.json"
    if not path.exists():
        pytest.skip("Optional immutable local pilot artifact")
    result = json.loads(path.read_text("utf-8"))["payload"]["position_check"]
    baseline = json.loads((pilot / "baseline-run.json").read_text("utf-8"))
    positions = {
        p["index"]: VorPosition.model_validate(p)
        for p in baseline["config"]["position_inputs"]
    }
    original = deepcopy(result)
    pending = findings(positions[index], result, positions, {})
    assert len(pending) == 1 and pending[0]["reason"] == "unverified_selected_measure"
    proposal = next(
        p for p in result["parameter_proposals"] if p["parameter"] == "unit_scale"
    )
    assert proposal["scope_state"] == "unresolved" and proposal["scope_quote"] == ""
    message = " ".join(feedback(positions[index], result, positions, {}))
    assert proposal["source_id"] in message and pending[0]["base_code"] in message
    assert "scope_quote отсутствует" in message and "уже переданы (1)" in message
    assert result == original
    assert findings(positions[index], result, positions, {}) == pending
