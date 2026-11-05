"""The planner must not hide an unverified material dependency."""

from copy import deepcopy

from normative_evidence_plan import build_work_plan
from normative_parameters import enrich
from normative_verdict import readable_review, verified_allocation_dependency
from test_planned_allocation_integration import prepared


def test_allocation_planner_uses_the_same_rebuilt_dependency_as_publication():
    positions, raw, relation = prepared()
    position = positions[1]
    review = enrich(position, raw, positions, relation)
    assert verified_allocation_dependency(review, position)
    plan = build_work_plan(
        position.model_dump(mode="json"),
        readable_review(review),
        dict(status="ОК", checks=[]),
        derived=True,
        relation=relation,
    )
    assert not any(a["key"] == "claim:derived:parent" for a in plan["actions"])
    changed = deepcopy(review)
    changed["parameter_cards"] = [
        c for c in changed["parameter_cards"] if c["id"] != "quantity"
    ]
    assert not verified_allocation_dependency(changed, position)
    plan = build_work_plan(
        position.model_dump(mode="json"),
        readable_review(changed),
        dict(status="ОК", checks=[]),
        derived=True,
        relation=relation,
    )
    assert any(a["key"] == "claim:derived:parent" for a in plan["actions"])
