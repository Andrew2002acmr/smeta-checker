"""Reserve room for actual project sources; defer whole optional text blocks."""

from copy import deepcopy

from normative_context_packing import serialize_context
from normative_embedded_context import CONTEXTS, literal_paths


def window(value, sources, *, limit, measure=None):
    output = deepcopy(value)
    size = measure or (lambda v: len(serialize_context(v)))
    reserve = min(18000, limit // 3)
    target = limit - reserve
    if size(output) <= target:
        return output
    deferred = []
    output["context_window"] = dict(
        reserved_source_characters=reserve, deferred_sources=deferred
    )
    # Work/formula/comment already supplied in the adjacent row is a reference,
    # not another copy of the same workbook text. Cross-site patterns carry no
    # quantities or duplicate names: their signature is the current work name.
    graph = output.get("work_scope_graph") or {}
    adjacent = {
        p["index"]: i for i, p in enumerate(output.get("related_vor_positions", []))
    }
    for node in graph.get("positions", []):
        if node["index"] in adjacent:
            reference = adjacent[node["index"]]
            for key in ("work", "quantity", "unit", "formula", "comment", "sections"):
                node.pop(key, None)
            node["context_ref"] = f"related_vor_positions[{reference}]"
    graph["same_work_other_scopes"] = [
        {k: v for k, v in p.items() if k in {"index", "scope_id", "row", "sections"}}
        for p in graph.get("same_work_other_scopes", [])
    ]
    # Defer full registered clauses, largest first. They remain immutable in
    # the source registry, with an explicit read request and honest preview.
    embedded = literal_paths(output, sources)
    chunks = []
    for name, field in CONTEXTS.items():
        if name=='methodology_context':
            # Clause 22 cannot be shown without clause 7's exceptions. The
            # reviewed methodology bundle is small and remains complete.
            continue
        context = output.get(name) or {}
        for chunk in context.get(field, []):
            if chunk.get("source_id") in embedded:
                chunks.append(chunk)
    for chunk in sorted(chunks, key=lambda c: len(c["quote"]), reverse=True):
        if size(output) <= target:
            break
        text = chunk.pop("quote")
        chunk.update(
            preview=text[:180],
            full_text_archived=True,
            read_request=dict(tool="read_source", source_id=chunk["source_id"]),
        )
        deferred.append(
            dict(
                source_id=chunk["source_id"],
                kind="full_normative_clause",
                full_length=len(text),
            )
        )
    # Literal templates are navigation, not the source itself. Keep whole
    # templates, prioritising explicitly discovered physical groups rather
    # than expected VOR numbers. Omitted handles stay available to read_source.
    preferred = {
        sid
        for c in output.get("quantity_candidates", [])
        for sid in [c.get("source_id"), *c.get("operand_source_ids", [])]
        if sid
    }
    for field in ("parameter_candidates", "quantity_parameter_candidates", "normative_thickness_adjustments"):
        entries = output.get(field)
        if not isinstance(entries, list):
            continue

        def sid(entry):
            item=entry.get("proposal") or entry
            return item.get("source_id") or item.get("base_source_id")

        entries.sort(key=lambda e: sid(e) not in preferred)
        while len(entries) > 4 and size(output) > target:
            removed = entries.pop()
            deferred.append(
                dict(source_id=sid(removed), kind="literal_template", field=field)
            )
    output["context_window"]["metadata_characters"] = size(output)
    return output
