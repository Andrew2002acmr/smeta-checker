"""Addressable engineering navigation, independent of a model/provider.

Relationships are navigation evidence, never an implicit position verdict.
"""

from copy import deepcopy


def attach(tools, context, record):
    if getattr(context.repository, 'supports_analysis_replay', False) is not True:
        return
    from normative_engineering_relations import load_for_run
    tools.engineering_graph = load_for_run(context, record)
    if not tools.engineering_graph.get('entities'):
        from audit_models import VorPosition
        from normative_engineering_relations import build
        positions = {p['index']: VorPosition.model_validate(p)
            for p in record['config'].get('position_inputs', [])}
        if positions:
            tools.engineering_graph = build(record, positions)
    tools.coverage['engineering_navigation'] = dict(available=bool(tools.engineering_graph.get('entities')),
        semantic_bindings_require_evidence=True)


def persist(context, record, position, result, positions, relation):
    """Persist ledgers and navigation at the same main-pipeline boundary."""
    from normative_fact_ledger import store
    ledger = store(context, record, position, result, positions, relation)
    if getattr(context.repository, 'supports_analysis_replay', False) is not True:
        return
    from normative_engineering_relations import build, store as store_graph
    graph = build(record, positions, ledgers=[ledger], sources=result.get('sources', []))
    result.setdefault('coverage', {})['engineering_relations'] = store_graph(context, record, graph)


def execute(tools, request):
    # Keep the project archive on the server. Register only an explicitly read
    # handle, rather than filling the model's source catalogue and tool budget
    # with every previously known source before its first search.
    if request.tool == 'read_source' and request.source_id and not request.entity_id:
        graph = getattr(tools, 'engineering_graph', None) or {}
        source = next((s for s in graph.get('sources', []) if s['id'] == request.source_id), None)
        if source is not None:
            try:
                # _add applies the same explicit unit facts to both envelopes
                # before the registry checks identity. It never overwrites a
                # different previously registered source.
                registered = tools._add(deepcopy(source))
            except ValueError:
                return {'error': 'engineering_source_collision', 'source_id': request.source_id}
            if 'error' in registered:
                return registered
    if request.tool == 'search_project' and request.entity_kind:
        graph = getattr(tools, 'engineering_graph', None)
        if not graph:
            return {'error': 'engineering_graph_unavailable'}
        from normative_engineering_relations import search
        selected = dict(graph, entities=[e for e in graph['entities'] if e['kind'] == request.entity_kind])
        return {'engineering_entities': search(selected, request.query, offset=request.entity_offset)}
    if not request.entity_id:
        return None
    if request.tool not in {'read_source', 'search_project'}:
        return {'error': 'entity_navigation_requires_read_source_or_search_project'}
    graph = getattr(tools, 'engineering_graph', None)
    if not graph:
        return {'error': 'engineering_graph_unavailable'}
    from normative_engineering_relations import related
    try:
        result = related(graph, request.entity_id, offset=request.offset)
    except KeyError:
        return {'error': 'unknown_engineering_entity'}
    tools.events.append(dict(kind='engineering_navigation_v1', entity_id=request.entity_id,
        offset=request.offset, result=deepcopy(result)))
    return result
