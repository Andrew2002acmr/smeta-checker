"""Locate exact full source texts already embedded in bounded source contexts."""

CONTEXTS = {
    'methodology_context': 'literal_clauses',
    'manufacturer_context': 'literal_clauses',
    'collection_context': 'literal_chunks',
}


def literal_paths(inputs, sources):
    """Deduplication changes placement only; it never establishes evidence."""
    lookup = {source['id']: source for source in sources}
    duplicates = {sid for sid in lookup if sum(source['id']==sid for source in sources)>1}
    matches = {}
    conflicts = set()
    for context_name, field in CONTEXTS.items():
        context = inputs.get(context_name)
        if not isinstance(context, dict) or not context.get('kb'):
            continue
        chunks = context.get(field)
        if not isinstance(chunks, list):
            continue
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            sid = chunk.get('source_id')
            if not isinstance(sid, str) or sid not in lookup or sid in duplicates:
                continue
            source = lookup[sid]
            if (source.get('kind') != 'normative' or source.get('kb_identity') != context['kb']
                    or not isinstance(chunk.get('quote'), str) or not chunk['quote']
                    or chunk['quote'] != source.get('text')):
                conflicts.add(sid)
                continue
            matches.setdefault(sid, f'{context_name}.{field}[{index}].quote')
    return {sid: path for sid, path in matches.items() if sid not in conflicts}


def packing_record(supplied, sources, stage):
    embedded = literal_paths(supplied, sources)
    exposed = {source['id'] for source in supplied.get('sources', [])}
    return dict(stage=stage, registry_sources=len(sources),
        supplied_source_ids=sorted(exposed), embedded_full_source_ids=sorted(embedded),
        truncated_previews=[source['id'] for source in supplied.get('sources', [])
            if source.get('excerpt_truncated') and source['id'] not in embedded],
        omitted_normative_source_ids=[source['id'] for source in sources
            if source.get('kind') == 'normative' and source['id'] not in exposed and source['id'] not in embedded],
        archived_source_ids=list(supplied.get('archived_source_ids', [])))
