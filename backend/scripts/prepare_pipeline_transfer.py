"""Freeze input-only transfer candidates. Never run a review or read its verdicts."""

import argparse
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def signature(position):
    fields = {
        "work_name": "w_name",
        "volume": "w_vol",
        "unit": "w_unit",
        "comment": "comment",
        "formula": "formula",
    }
    return digest(
        {
            key: " ".join(
                unicodedata.normalize(
                    "NFKC", str(position.get(key, position.get(alias, "")))
                ).split()
            )
            for key, alias in fields.items()
        }
    )


def fixture_signatures(folder):
    seen = set()

    def visit(value):
        if isinstance(value, dict):
            if ("work_name" in value or "w_name" in value) and (
                "unit" in value or "w_unit" in value
            ):
                seen.add(signature(value))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in sorted(folder.glob("*.json")):
        visit(json.loads(path.read_text("utf-8")))
    return seen


def select_candidates(
    run, known_signatures=(), *, count=8, development_indices=tuple(range(10))
) -> dict[str, Any]:
    if not 1 <= count <= 10:
        raise ValueError("Only a small candidate set of 1..10 inputs is allowed")
    positions = run["config"]["position_inputs"]
    if len({p["index"] for p in positions}) != len(positions):
        raise ValueError("Duplicate position indices")
    known = set(known_signatures)
    excluded = []
    eligible = []
    for position in positions:
        reason = (
            "development_pilot"
            if position["index"] in development_indices
            else "existing_fixture"
            if signature(position) in known
            else None
        )
        if reason:
            excluded.append(dict(index=position["index"], reason=reason))
        else:
            eligible.append(position)
    # Outcome-independent ordering and type/unit/formula diversity. No model
    # result, status, expected amount match or normative answer is consulted.
    eligible.sort(
        key=lambda p: digest([run.get("vor_sha256"), signature(p), p["index"]])
    )
    selected = []
    buckets = set()
    for position in eligible:
        bucket = (
            position.get("position_type", ""),
            position.get("unit", ""),
            bool(position.get("formula")),
        )
        if bucket not in buckets and len(selected) < count:
            selected.append(position)
            buckets.add(bucket)
    selected_indices = {p["index"] for p in selected}
    selected += [p for p in eligible if p["index"] not in selected_indices][
        : count - len(selected)
    ]
    if len(selected) != count:
        raise ValueError("Not enough eligible inputs; do not reuse development cases")
    return dict(
        schema=1,
        mode="selection_only",
        model_requests=0,
        routerai_requests=0,
        source_run_id=run["id"],
        vor_sha256=run.get("vor_sha256"),
        input_fingerprint=digest(positions),
        excluded=excluded,
        selected_indices=[p["index"] for p in selected],
        positions=selected,
        expectation_labels=None,
        execution_status="not_run",
        annotation_status="independent_annotation_required",
        contamination_status="same_project_historical_exposure_not_fully_known",
        independent_project_required_for_external_generalization=True,
        dependency_context_review_required=True,
        policy="Freeze before independent annotation; never tune rules using control outcomes.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()
    result = select_candidates(
        json.loads(args.source_run.read_text("utf-8")),
        fixture_signatures(args.fixtures),
        count=args.count,
    )
    result["source_file_sha256"] = hashlib.sha256(
        args.source_run.read_bytes()
    ).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # A frozen candidate set cannot be silently replaced after seeing outcomes.
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"positions", "excluded"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
