from copy import deepcopy

from normative_agent import compact, MAX_CONTEXT
from normative_context_window import window
from normative_context_packing import serialize_context
from test_normative_context_packing import CAPTURES, size
from normative_embedded_context import literal_paths


def test_small_window_is_lossless():
    value = dict(
        work_scope_graph={"positions": []},
        required_validation_claims=["quantity"],
        position={"work": "Work"},
    )
    assert window(value, [], limit=58000) == value


def test_dense_real_capture_reserves_room_for_project_operands_without_losing_norms():
    case = deepcopy(CAPTURES[0])
    original = deepcopy(case)
    metadata = case["metadata"]
    metadata["work_scope_graph"] = dict(positions=[], same_work_other_scopes=[])
    metadata["extra_navigation"] = "n" * 6000
    packed = compact(
        metadata,
        case["sources"],
        case["preferred"],
        measure=lambda v: size(v, case["sources"]),
    )
    assert size(packed, case["sources"]) <= MAX_CONTEXT
    preview = {s["id"]: s for s in packed["sources"]}
    for sid in [
        "agent:p89:cell:feb8114d3c30a3c1318def97",
        "agent:p89:cell:b10b80c2f6781706ffd1f953",
        "agent:sum:ea3f8bc61fa62ef5f0b6f647",
    ]:
        assert preview[sid]["text"] == next(
            s["text"] for s in case["sources"] if s["id"] == sid
        )
    deferred = {s["source_id"] for s in packed["context_window"]["deferred_sources"]}
    full = set(literal_paths(packed, case["sources"]))
    assert {
        sid
        for sid in literal_paths(original["metadata"], case["sources"])
        if sid.startswith("methodology:")
    } <= full
    assert set(literal_paths(original["metadata"], case["sources"])) <= deferred | full
    assert case["sources"] == original["sources"]
    assert (
        packed["required_validation_claims"] == metadata["required_validation_claims"]
    )


def test_unknown_large_required_metadata_is_not_silently_dropped():
    value = dict(
        work_scope_graph={"positions": []},
        required_validation_claims=["quantity"],
        unknown_required="x" * 10000,
    )
    output = window(value, [], limit=1000)
    assert output["unknown_required"] == value["unknown_required"]
    assert len(serialize_context(output)) > 1000
