"""Independent fact validation and reversible derived numerical projections."""

from copy import deepcopy

PROJECT_CLAIMS = {"quantity", "unit", "presence", "thickness"}
PROJECTION = "numeric_item_projection_v2"


def project_claim(claim):
    return claim in PROJECT_CLAIMS or claim.startswith("geometry:")


def atomic_items(items, trace=None):
    """Split an asserted conjunction before applying different source policies.

    Preserve the model state. Splitting cannot turn a model's uncertainty into
    a positive assertion. Original statements remain in the evidence trace.
    """
    output = []
    for item in items:
        claims = item.claim_ids
        numeric = [c for c in claims if c in {"quantity", "unit"}]
        groups = ([numeric] if numeric else []) + [
            [c] for c in claims if c not in {"quantity", "unit"}
        ]
        if len(groups) < 2:
            output.append(item)
            continue
        if trace is not None:
            trace.append(
                dict(kind="atomic_claim_partition_v1", original=item.model_dump())
            )
        for group in groups:
            values = {"claim_ids": group}
            if all(project_claim(c) for c in group):
                # A norm is not required to establish a literal project fact.
                # Keep the discarded references in the original trace above.
                values["normative_source_ids"] = []
            output.append(item.model_copy(update=values))
    return output


def dimension_candidates(proposals, expected):
    """Partition physical subjects before checking ambiguity, never by value.

    Retain a sole incompatible dimensional group so the ordinary unit checker
    can report it. Malformed units cannot silently disappear beside valid ones.
    """
    from normative_parameters import measure

    if not expected:
        return proposals
    dimensions = [measure(p.get("value"), p.get("unit", "")) for p in proposals]
    if any(d is None for d in dimensions):
        return proposals
    compatible = [
        p for p, d in zip(proposals, dimensions, strict=True) if d[0] == expected[0]
    ]
    return compatible or proposals


def restore_numeric_items(result):
    """A card's derived downgrade is not independent new evidence on replay."""
    events = result.get("evidence_trace", [])
    projections = [e for e in events if e.get("kind") == PROJECTION]
    for item in result.get("items", []):
        match = next((e for e in reversed(projections) if item == e["projected"]), None)
        if match:
            item.clear()
            item.update(deepcopy(match["original"]))
    result["evidence_trace"] = [e for e in events if e.get("kind") != PROJECTION]


def record_projection(result, original, projected):
    result.setdefault("evidence_trace", []).append(
        dict(
            kind=PROJECTION, original=deepcopy(original), projected=deepcopy(projected)
        )
    )
