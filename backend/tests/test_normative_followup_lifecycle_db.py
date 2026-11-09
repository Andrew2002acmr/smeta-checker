"""Real durable PostgreSQL delivery with synthetic evidence and zero LLM calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID

from normative_background import review_position
from test_durable_tasks import pipeline as pipeline
from test_normative_followup_lifecycle import geometry_payload


def test_mismatched_parent_fact_change_enqueues_formal_child_once_and_runs_locally(pipeline):
    p = pipeline
    p.repo.update_run_config(p.run.id, dict(normative_after_match=True))
    child = dict(position_key="child", vor_version_id="vor", kb={},
        work_relations=dict(parent=dict(index=0)),
        position_check=dict(pipeline_version=27, execution_state="complete", state="needs_facts",
            items=[], sources=[], parameter_cards=[], parameter_proposals=[]))
    saved_child = p.repo.save_normative_review(p.run.id, 1, "check", child, "synthetic-child")
    neighbour = deepcopy(child)
    neighbour["work_relations"] = dict(material_owner=dict(index=0))
    p.repo.save_normative_review(p.run.id, 2, "check", neighbour, "synthetic-neighbour")
    empty_parent = dict(position_check=dict(pipeline_version=27, execution_state="complete",
        state="needs_facts"), work_relations=dict(children=[dict(material_index=1)]))
    p.repo.save_normative_review(p.run.id, 0, "check", empty_parent, "synthetic-no-new-geometry")
    assert not p.repo.reserve_deliveries()
    parent = geometry_payload()
    parent["work_relations"] = dict(children=[dict(material_index=1), dict(material_index=2)])
    p.repo.save_normative_review(p.run.id, 0, "check", parent, "synthetic-parent-mismatch")
    p.repo.save_normative_review(p.run.id, 0, "check", parent, "synthetic-parent-redelivery")
    tasks = p.repo.reserve_deliveries()
    assert len(tasks) == 1
    assert tasks[0]["payload"]["position_index"] == 1
    assert tasks[0]["payload"]["local_only"] is True
    revision = tasks[0]["payload"]["dependency_revision"]
    task, token = UUID(tasks[0]["id"]), UUID(tasks[0]["token"])
    assert p.repo.claim_delivery(task, token, timeout=30)
    with patch("normative_position_check.check") as paid, patch(
        "normative_position_check.validate_saved", return_value=saved_child
    ) as local:
        outcome = review_position(p.context, p.run.id, 1, local_only=True, dependency_revision=revision)
        assert outcome["local_only"] is True
        local.assert_called_once()
        paid.assert_not_called()
    p.repo.settle_delivery(task, token)
    assert not p.repo.reserve_deliveries()
    changed = deepcopy(parent)
    result = changed["position_check"]
    result["sources"][0]["text"] = result["sources"][0]["text"].replace("400", "450")
    result["parameter_proposals"][0].update(value="450", quote="количество 450 шт",
        scope_quote=result["sources"][0]["text"])
    result["parameter_cards"][0].update(found_value="450",
        source_anchors=[dict(source_id="p1", quote="количество 450 шт", rects=[])])
    p.repo.save_normative_review(p.run.id, 0, "check", changed, "synthetic-new-project-fact")
    p.repo.save_normative_review(p.run.id, 0, "check", changed, "synthetic-new-project-fact-redelivery")
    refreshed = p.repo.reserve_deliveries()
    assert len(refreshed) == 1
    assert refreshed[0]["payload"]["position_index"] == 1
    assert refreshed[0]["payload"]["dependency_revision"] != revision
