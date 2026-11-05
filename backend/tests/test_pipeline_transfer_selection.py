from copy import deepcopy
from typing import Any

import pytest

from scripts.prepare_pipeline_transfer import select_candidates, signature


def sample() -> dict[str, Any]:
    return dict(
        id="synthetic-run",
        vor_sha256="fixture-workbook",
        config=dict(
            position_inputs=[
                dict(
                    index=i,
                    work_name=f"Work {i}",
                    unit=["м2", "м3", "шт"][i % 3],
                    volume=str(i + 20),
                    position_type="Материал" if i % 2 else "Работа",
                    formula="A1*2" if i % 2 else "",
                    comment="",
                )
                for i in range(30)
            ]
        ),
        indexed_results=[dict(state="needs_facts")],
    )


def test_transfer_is_disjoint_deterministic_and_independent_of_saved_verdicts():
    original = sample()
    first = select_candidates(original)
    changed = deepcopy(original)
    changed["indexed_results"] = [dict(state="confirmed")] * 200
    assert select_candidates(changed) == first
    assert len(first["selected_indices"]) == 8
    assert not set(first["selected_indices"]) & set(range(10))
    assert (
        first["expectation_labels"] is None and first["execution_status"] == "not_run"
    )
    assert first["contamination_status"] != "blind_holdout"
    assert sample() == original


def test_existing_fixture_is_excluded_even_when_its_index_changes():
    data = sample()
    known = deepcopy(data["config"]["position_inputs"][12])
    known["index"] = 999
    result = select_candidates(data, [signature(known)])
    assert 12 not in result["selected_indices"]
    assert dict(index=12, reason="existing_fixture") in result["excluded"]


@pytest.mark.parametrize("count", [0, 11, 103])
def test_selection_cannot_expand_to_a_full_audit(count):
    with pytest.raises(ValueError):
        select_candidates(sample(), count=count)


def test_selection_refuses_to_fill_shortage_with_development_data():
    with pytest.raises(ValueError, match="Not enough eligible"):
        select_candidates(sample(), count=8, development_indices=tuple(range(29)))
