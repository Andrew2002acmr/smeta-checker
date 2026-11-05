"""Recover complete resources from the exact pinned norm, without model calls."""
import hashlib
import json
from functools import lru_cache


def same_physical_resource_event(events):
    """Collapse transport aliases only; retain every conflicting observation.

    Hydration keeps original events for audit. Recovered rows can therefore
    describe the exact same norm a second time under new source handles.
    IDs of resource cards and recovery_prefix are transport, all normative
    rows, quantities, units, pages, checks and rules must remain identical.
    """
    shapes = set()
    for event in events:
        normalized = dict(event)
        normalized.pop('recovery_prefix', None)
        normalized['resources'] = [{k: v for k, v in row.items() if k != 'source_id'}
            for row in event.get('resources', [])]
        shapes.add(json.dumps(normalized, sort_keys=True, ensure_ascii=False))
    return events[0] if events and len(shapes) == 1 else None


def same_physical_resource_source(sources):
    """Source handles may differ; every physical payload field must agree."""
    shapes = {json.dumps({k: v for k, v in source.items() if k != 'id'},
        sort_keys=True, ensure_ascii=False) for source in sources}
    return min(sources, key=lambda s: s['id']) if sources and len(shapes) == 1 else None


@lru_cache(maxsize=128)
def _norm_ids(kb,source_id,code):
    with kb.connect() as db:
        return tuple(row['id'] for row in db.execute(
            'SELECT id FROM norms WHERE source_id=? AND code=? ORDER BY id LIMIT 2',(source_id,code)))


@lru_cache(maxsize=128)
def _norm_json(kb,norm_id):
    # Corpus instances are pinned to an immutable SHA. Cache serialized source
    # rows only; facts/checks/rules remain per-position and are never cached.
    return json.dumps(kb.entity('norm',norm_id),ensure_ascii=False)


def hydrate(kb,result,facts=None):
    from normative_thickness_rules import source_definition
    from normative_agent import resource_sources
    from normative_service import candidate
    if not all(callable(getattr(kb,name,None)) for name in ('connect','entity','checks','rules')):
        return result
    selected={sid for item in result.get('items',[]) if 'normative:applicability' in item.get('claim_ids',[])
        for sid in item.get('normative_source_ids',[])}
    seen=set()
    recovered=[]
    for source in list(result.get('sources',[])):
        if source['id'] not in selected or not (definition:=source_definition(source)):
            continue
        key=definition['source_id'],definition['code']
        if key in seen:
            continue
        seen.add(key)
        rows=_norm_ids(kb,*key)
        if len(rows)!=1:
            continue
        item=json.loads(_norm_json(kb,rows[0]))
        if item['status']!=source.get('origin') or not item.get('resources'):
            continue
        item['checks'],item['missing_facts']=kb.checks(item,facts or {})
        item['rules']=kb.rules(item,facts or {})
        formatted=candidate(kb,item)
        prefix='recovered:'+hashlib.sha256((str(kb.identity)+':'+str(key)).encode()).hexdigest()[:24]
        trace=[]
        sources=resource_sources(formatted,prefix,evidence_trace=trace)
        # The prefix owns resource cards, not a full norm-definition card.
        # Keep the recovered rows bound to the already selected definition.
        for event in trace:
            if event.get('kind')=='normative_resource_consumption_candidates':
                event.update(norm_source_id=source['id'],recovery_prefix=prefix)
        existing={s['id']:s for s in result['sources']}
        if any(s['id'] in existing and s!=existing[s['id']] for s in sources):
            continue
        result['sources'].extend(s for s in sources if s['id'] not in existing)
        events=result.setdefault('evidence_trace',[])
        events[:]=[e for e in events if not (e.get('kind')=='normative_resource_consumption_candidates'
            and (e.get('recovery_prefix')==prefix or e.get('norm_source_id')==prefix))]
        events.extend(trace)
        recovered.append(dict(norm_id=item['id'],code=item['code'],base_source_id=source['id'],
            source_ids=[s['id'] for s in sources],resource_count=len(item['resources']),
            resources_truncated=len(item['resources'])>80,
            project_rate_resources=[r['name'] for r in item['resources'] if r.get('quantity_kind')=='project']))
    events=result.setdefault('evidence_trace',[])
    events[:]=[e for e in events if e.get('kind')!='pinned_resource_recovery_v1']
    if recovered:
        events.append(dict(kind='pinned_resource_recovery_v1',kb=kb.identity,norms=recovered,
            model_requests=0,applicability_confirmed=False))
    return result
