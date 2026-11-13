"""Real PostgreSQL must retain the conditional appendix candidate and its limits."""

from copy import deepcopy
import json

import pytest

from analysis_replay import digest
from normative_parameters import enrich
from normative_verdict import publish, readable_review
from normative_workbook_rate import PROOF, geometric_rate_replay_supported
from test_asphalt_appendix_candidate import fixture
from test_durable_tasks import pipeline as pipeline


def ledger(review):
    return next(
        event for event in review["evidence_trace"] if event.get("kind") == PROOF
    )


def prepared():
    """Synthetic PD; complete primary appendix text comes from the shared fixture."""
    work, raw, positions, relation = fixture()
    for source in raw["sources"]:
        if source["id"] in {"app", "new35", "base"}:
            # These negative qualifiers must survive as complete source objects.
            source.update(
                edition_status="edition_not_confirmed",
                automatic_application_allowed=False,
                amendment_navigation={
                    "kind": "simultaneous_addressed_amendment_navigation",
                    "target_edition_state": "unresolved",
                },
                source_provenance={"amendment_completeness_verified": False},
            )
    review = readable_review(enrich(work, raw, positions, relation))
    review["position_key"] = "conditional-appendix-projection"
    assert geometric_rate_replay_supported(work, review)
    candidate = ledger(review)["appendix_rate_candidate"]
    assert candidate["rate"] == "123.30"
    return work, review


def save_and_read(p, work, review):
    saved = p.repo.save_normative_review(
        p.run.id,
        work.index,
        "check",
        dict(position_key=review["position_key"], position_check=review),
        "appendix-projection-test",
    )
    full = next(
        row
        for row in p.repo.normative_reviews(p.run.id, work.index)
        if row["id"] == saved["id"]
    )["payload"]["position_check"]
    batch = p.repo.normative_validation_context(p.run.id)[work.index]["review"]
    return full, batch


def public_status(work, review):
    return publish(
        {"Статус аудита": "ОК"},
        {"checks": []},
        review["position_key"],
        dict(stage="done", review=readable_review(review)),
        enabled=True,
        derived=True,
        position=work,
    )["Статус аудита"]


def assert_conditional_state(work, review):
    visible = readable_review(review)
    assert geometric_rate_replay_supported(work, visible)
    proof = ledger(visible)["appendix_rate_candidate"]
    assert proof["conditional_arithmetic_verified"]
    assert not proof["normative_basis_verified"]
    assert not proof["dependent_output_verified"]
    assert not proof["actual_material_density_verified"]
    assert {
        "edition_chain",
        "project_consumption_priority",
        "edge_prescription",
    } <= set(proof["pending_condition_ids"])
    cards = {card["id"]: card for card in visible["parameter_cards"]}
    assert cards["quantity"]["state"] == "confirmed"
    assert cards["quantity"]["found_unit"] == "м2"
    assert cards["thickness"]["state"] == "confirmed"
    assert cards[f"calculation:{work.index}"]["state"] == "confirmed"
    assert cards[f"factor:{work.index}:123.30"]["state"] == "needs_facts"
    finding = ledger(visible)["appendix_scope_finding"]
    assert finding in visible["items"]
    assert finding["required"] and finding["state"] == "needs_facts"
    assert "normative:applicability" in finding["claim_ids"]
    assert "basis:comment:consumption:1" in finding["claim_ids"]
    assert public_status(work, visible) != "ОК"


def test_postgres_full_and_batch_keep_appendix_inputs_and_exclude_unselected_ocr(
    pipeline,
):
    work, review = prepared()
    original = deepcopy(review)
    review["sources"].extend(
        [
            dict(
                id="unselected-ocr",
                kind="project",
                origin="ocr",
                text="private-ocr-noise" * 500,
            ),
            dict(
                id="unselected-norm", kind="normative", text="private-norm-noise" * 500
            ),
        ]
    )
    review["evidence_trace"].append(
        dict(
            kind="tool",
            output="private-ocr-noise",
            nested=dict(
                kind=PROOF, root_source_ids=["unselected-ocr", "unselected-norm"]
            ),
        )
    )
    before = deepcopy(review)
    full, batch = save_and_read(pipeline, work, review)
    assert review == before
    assert full == review
    assert {s["id"] for s in full["sources"]} == {s["id"] for s in review["sources"]}
    assert ledger(full) == ledger(original) == ledger(batch)
    assert (
        full["parameter_proposals"]
        == original["parameter_proposals"]
        == batch["parameter_proposals"]
    )

    expected = {s["id"]: s for s in original["sources"]}
    actual = {s["id"]: s for s in batch["sources"]}
    candidate = ledger(original)["appendix_rate_candidate"]
    assert set(candidate["root_source_ids"]) <= set(actual)
    for source_id in candidate["root_source_ids"]:
        assert actual[source_id] == expected[source_id]
        assert digest(actual[source_id]) == candidate["source_sha256"][source_id]
    for source_id in ("app", "new35", "base"):
        assert actual[source_id]["automatic_application_allowed"] is False
        assert (
            actual[source_id]["amendment_navigation"]["target_edition_state"]
            == "unresolved"
        )
        assert (
            actual[source_id]["source_provenance"]["amendment_completeness_verified"]
            is False
        )
    assert "private-ocr-noise" not in json.dumps(batch)
    assert "private-norm-noise" not in json.dumps(batch)
    assert "unselected-ocr" not in actual and "unselected-norm" not in actual
    assert not any(e.get("kind") == "tool" for e in batch["evidence_trace"])
    assert_conditional_state(work, full)
    assert_conditional_state(work, batch)


@pytest.mark.parametrize(
    "mutation",
    [
        "anchor",
        "proposal",
        "selected_norm_item",
        "appendix_literal",
        "selected_qualifier",
    ],
)
def test_postgres_does_not_hide_tampered_appendix_candidate_inputs(pipeline, mutation):
    work, review = prepared()
    if mutation == "anchor":
        next(s for s in review["sources"] if s["kind"] == "project")["rects"] = [
            [0.2, 0.3, 0.6, 0.7]
        ]
    elif mutation == "proposal":
        next(p for p in review["parameter_proposals"] if p["parameter"] == "thickness")[
            "scope_state"
        ] = "unresolved"
    elif mutation == "selected_norm_item":
        next(
            i
            for i in review["items"]
            if i["state"] == "confirmed" and "normative:applicability" in i["claim_ids"]
        )["state"] = "needs_facts"
    elif mutation == "appendix_literal":
        source = next(s for s in review["sources"] if s["id"] == "app")
        source["text"] = source["text"].replace("98,64", "98,65")
    else:
        next(s for s in review["sources"] if s["id"] == "base")["edition_status"] = (
            "changed"
        )
    assert not geometric_rate_replay_supported(work, review)
    full, batch = save_and_read(pipeline, work, review)
    assert ledger(batch) == ledger(full) == ledger(review)
    assert batch["parameter_proposals"] == full["parameter_proposals"]
    selected_ids = set(ledger(review)["appendix_rate_candidate"]["root_source_ids"])
    expected = {s["id"]: s for s in review["sources"] if s["id"] in selected_ids}
    assert {s["id"]: s for s in batch["sources"] if s["id"] in selected_ids} == expected
    for stored in (full, batch):
        assert not geometric_rate_replay_supported(work, readable_review(stored))
        assert public_status(work, stored) != "ОК"
        assert ledger(stored)["appendix_scope_finding"]["state"] == "needs_facts"
