"""Reserve room for actual project sources; defer whole optional text blocks."""

from copy import deepcopy

from normative_context_packing import serialize_context
from normative_embedded_context import CONTEXTS, literal_paths


def columnar_page_coverage(pages):
    """Losslessly encode repeated per-page OCR flags, never source evidence.

    Whole-document coverage is useful, but repeating field names on every
    page must not displace the actual project cells and normative clauses.
    Unknown flags are retained; heterogeneous records remain untouched.
    """
    if not isinstance(pages, dict) or not pages or not all(isinstance(v, dict) for v in pages.values()):
        return pages
    first = next(iter(pages.values()))
    if any(set(row) != set(first) for row in pages.values()):
        return pages
    shared = {k: v for k, v in first.items()
        if all(type(row[k]) is type(v) and row[k] == v for row in pages.values())}
    columns = sorted(set(first) - set(shared))
    return dict(encoding='page_columns_v1', shared=shared, columns=columns,
        pages={page: [row[k] for k in columns] for page, row in pages.items()})


def window(value, sources, *, limit, measure=None, read_source_ids=()):
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
    coverage = output.get('coverage') or {}
    original_inline = coverage.get('inline_raster')
    projected_inline = columnar_page_coverage(original_inline)
    if len(serialize_context(projected_inline)) < len(serialize_context(original_inline)):
        coverage['inline_raster'] = projected_inline
    # Follow-up actions are navigation. Delivery receipts and retry hashes are
    # server telemetry, not source evidence; copying them into every model
    # round can displace the very text the plan requests. Keep the actual
    # obligations, source handles, quality flags, reasons and read offsets.
    followup=output.get('evidence_followup')
    if isinstance(followup,dict) and isinstance(followup.get('actions'),list):
        for action in followup['actions']:
            if isinstance(action,dict):
                for key in ('model_delivery','retry_fingerprint','proposal_count'):
                    action.pop(key,None)
        followup['context_projection']='navigation_without_server_delivery_telemetry'
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
        if len(chunk['quote'])<=512:
            continue
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
    preferred = set(read_source_ids) | {
        sid
        for c in output.get("quantity_candidates", [])
        for sid in [c.get("source_id"), *c.get("operand_source_ids", [])]
        if sid
    }
    explicit_read=any((event.get('request') or {}).get('tool') in {'read_source','read_page'}
        for event in output.get('tool_results',[]))
    minimum_templates=1 if explicit_read else 4
    for field in ("parameter_candidates", "quantity_parameter_candidates", "normative_thickness_adjustments"):
        entries = output.get(field)
        if not isinstance(entries, list):
            continue

        def sid(entry):
            item=entry.get("proposal") or entry
            return item.get("source_id") or item.get("base_source_id")

        entries.sort(key=lambda e: sid(e) not in preferred)
        while len(entries) > minimum_templates and size(output) > target:
            removed = entries.pop()
            deferred.append(
                dict(source_id=sid(removed), kind="literal_template", field=field)
            )
    coverage=output.get('coverage') or {}
    catalogue=coverage.get('available_tables')
    if isinstance(catalogue,list) and len(catalogue)>8 and size(output)>target:
        # This duplicates the table-search preview. Exact selected cells and
        # discovered sums remain in their own fields and immutable registry.
        coverage['available_tables']=catalogue[:8]
        coverage['table_catalog_truncated']=True
        coverage['table_catalog_deferred']=dict(shown=8,previously_shown=len(catalogue),
            request=dict(tool='search_tables',query='уточнить материал и конструкцию'))
    if size(output)>target:
        # Earlier prose is not evidence. Preserve the claim, state and source
        # edges; the current answer must be based on the actual source text.
        for item in output.get('previous_review_items',[]):
            item.pop('explanation',None)
        for event in list(deferred):
            while deferred.count(event)>1:
                deferred.remove(event)
    discovery=output.get('prepared_quantity_tables')
    if size(output)>target and output.get('quantity_candidates') and isinstance(discovery,dict):
        from normative_agent import model_result
        output['prepared_quantity_tables']=model_result(discovery,2500)
        output['prepared_quantity_tables']['read_again']=True
        output['prepared_quantity_tables']['read_request']=dict(tool='search_tables',
            query=output.get('position',{}).get('work','')[:300])
    output["context_window"]["metadata_characters"] = size(output)
    return output
