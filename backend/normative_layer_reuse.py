"""Reuse a verified physical work basis across its technological layer rows.

The immutable donor review remains the root. No model verdict is copied to a
different material or independent work, and consumption factors are not reused.
"""
from copy import deepcopy
import time


def reuse_layer_check(context, record, index, actor):
    started=time.monotonic()
    from normative_local_checks import layer_group
    from normative_parameters import clean,enrich
    from normative_verdict import publish,source_supported
    from audit_review_policy import review_source
    from audit_predicates import technical_specs
    from workspace_service import inputs_for_run
    from vor_material_links import material_links
    import normative_service as service
    positions={p.index:p for p in inputs_for_run(context,record)}
    position=positions[index]
    own=position.position_source
    if own is None:
        return None
    related=[dict(index=p.index,row=p.position_source.row,work=p.work_name,quantity=p.volume,unit=p.unit)
        for p in positions.values() if p.index!=index and abs(p.index-index)<=7 and p.position_source
        and p.position_source.sheet==own.sheet and p.position_source.sections==own.sections]
    group=layer_group(position,related)
    if not group:
        return None
    peers=[positions[int(r['index'])] for r in related if r['row']==group['other_row']]
    if len(peers)!=1:
        return None
    peer=peers[0]
    if clean(position.comment)!=clean(peer.comment) or position.position_type!=peer.position_type:
        return None
    links=material_links([p.legacy_task() for p in positions.values()])
    if index in links or peer.index in links:
        return None
    def children(owner):
        return [dict(**link,material_index=child,work=positions[child].work_name,
            quantity=positions[child].volume,unit=positions[child].unit)
            for child,link in links.items() if link['index']==owner]
    own_children,peer_children=children(index),children(peer.index)
    if sorted(clean(c['work']) for c in own_children)!=sorted(clean(c['work']) for c in peer_children):
        return None
    donor=next((r for r in context.repository.normative_reviews(record['id'],peer.index) if r['kind']=='check'),None)
    if donor is None:
        return None
    data=donor['payload']
    proof=data.get('position_check') or {}
    if (data.get('vor_version_id')!=str(record['vor_version_id']) or data.get('position_key')!=service.position_key(peer)
            or proof.get('execution_state')!='complete' or proof.get('pipeline_version',0)<3
            or any(e.get('kind')=='reused_layer_basis' for e in proof.get('evidence_trace',[]))):
        return None
    if data.get('kb')!=service.corpus(profile=service.profile_for(record)).identity:
        return None
    pinned=set((record['config'].get('pdf_versions') or {}).values())
    project=[s for s in proof['sources'] if s['kind']=='project']
    if not project or any(s.get('version_id') not in pinned for s in project):
        return None
    # Revalidate a donor under the current rules before trusting its published
    # status. An older green selection may contradict a cited condition.
    from normative_conditional_selection import apply_guard
    proof=deepcopy(proof)
    apply_guard(peer,proof,positions,data.get('work_relations') or {})
    def stored_for(p):
        raw=next((r['result'] for r in record['indexed_results'] if r['position_index']==p.index),None)
        return review_source(raw,p.legacy_task()) if raw is not None else None
    def report_for(stored,review,key):
        base={'Статус аудита':'ОК' if stored.get('status')=='ОК' else 'Требуется ручная проверка','Что не так':''}
        return publish(base,stored,key,{'stage':'done','review':{**review,'position_key':key}},enabled=True)
    peer_stored,own_stored=stored_for(peer),stored_for(position)
    if peer_stored is None or own_stored is None or report_for(peer_stored,proof,data['position_key'])['Статус аудита']!='ОК':
        return None
    confirmed={c for i in proof['items'] if i['state']=='confirmed' and source_supported(i) for c in i['claim_ids']}
    shared=confirmed & {'presence','quantity','unit','method','normative:applicability'}
    def specs(p):
        return {s.claim_id:s for s in technical_specs(p.work_name,p.comment,p.position_type,[],p.formula)}
    a,b=specs(position),specs(peer)
    shared|={key for key in confirmed & a.keys() & b.keys() if a[key].expected_text==b[key].expected_text
        and a[key].property_label in {'thickness','consumption'}}
    if not {'quantity','normative:applicability'}<=shared:
        return None
    mapping={s['id']:f'layer-{peer.index}:'+s['id'] for s in proof['sources']}
    sources=[{**deepcopy(s),'id':mapping[s['id']]} for s in proof['sources']]
    proposals=[]
    for proposal in proof.get('parameter_proposals',[]):
        if proposal['parameter'] not in {'quantity','thickness','unit_scale'} or proposal['source_id'] not in mapping:
            continue
        proposal=deepcopy(proposal)
        proposal['source_id']=mapping[proposal['source_id']]
        proposal['operand_source_ids']=[mapping[s] for s in proposal.get('operand_source_ids',[]) if s in mapping]
        if proposal.get('operation')=='sum':
            for field in ('quote','scope_quote'):
                proposal[field]=proposal.get(field,'').partition(' Ячейки:')[0].rstrip('.')
        proposals.append(proposal)
    selected_projects={sid for card in proof.get('parameter_cards',[]) if card['id'] in {'quantity','thickness'}
        for sid in card.get('source_ids',[])}
    selected_projects|={sid for item in proof['items'] if item['state']=='confirmed' and 'normative:applicability' in item['claim_ids']
        for sid in item.get('project_source_ids',[])}
    project_ids=[mapping[s['id']] for s in project if s['id'] in selected_projects]
    norm_ids=list(dict.fromkeys(mapping[sid] for i in proof['items'] if i['state']=='confirmed'
        and 'normative:applicability' in i['claim_ids'] for sid in i['normative_source_ids'] if sid in mapping))
    relation=dict(parent=None,children=own_children[:12],children_truncated=len(own_children)>12)
    derived=dict(pipeline_version=proof['pipeline_version'],execution_state='complete',state='needs_facts',
        summary='Общее основание технологических слоёв проверено по сохранённым источникам.',
        items=[dict(topic='Общее основание технологических слоёв',state='confirmed',required=True,
            claim_ids=sorted(shared),project_source_ids=project_ids,normative_source_ids=norm_ids,
            explanation=f'Проверенное основание позиции №{peer.number or peer.index+1}: одна конструкция, одинаковая площадь и материал; '
                f"толщины {group['first']} + {group['second']} = {group['height']} см. Количества, цитаты и единицы повторно проверены программно.")],
        sources=sources,parameter_proposals=proposals,related_positions=related,
        evidence_trace=[dict(kind='reused_layer_basis',review_id=donor['id'],position_index=peer.index)],
        coverage=proof.get('coverage',{}),local_rules_version=proof.get('local_rules_version',0),requires_engineer_review=True)
    derived=enrich(position,derived,positions,relation)
    key=service.position_key(position)
    if report_for(own_stored,derived,key)['Статус аудита']!='ОК':
        return None  # Additional target-specific claims still need normal analysis.
    derived['state']='confirmed'
    derived['telemetry']=dict(interpretation='Общее подтверждённое основание технологических слоёв',queries=[],missing_facts=[],
        warnings=[f"Источники и расход исходной проверки сохранены отдельно: {donor['id']}"],
        model='layer-evidence-cache',provider='local',requests=0,usage={},
        elapsed_seconds=round(time.monotonic()-started,3),trace_id=None,trace_url=None)
    facts,origins,citations=service.project_context(context,record,index)[1:]
    payload=deepcopy(data)
    payload.update(work_item_id=f"{record['id']}:{index}",position_key=key,query=position.work_name,
        work_relations=relation,position_check=derived,status='candidate',facts=facts.model_dump(),fact_origins=origins,
        project_citations=citations,context_analysis=None,comment=f"Проверенное общее основание из {donor['id']}; исходный результат не изменялся.")
    return context.repository.save_normative_review(record['id'],index,'check',payload,actor)
