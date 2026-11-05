"""Bind a derived material to verified parent geometry, preserving its own rate check."""
from copy import deepcopy
import hashlib
import json


GEOMETRY_TOPIC='Подтверждённый параметр родительской работы'
GEOMETRY_TRACE='reused_parent_geometry'
DEPENDENCY_TRACE='parent_geometry_dependency_v1'


def verified_geometry_dependency(result,position=None):
    """Rebuild the transferred project facts, never trust a green parent flag.

    A dependency is the geometric input of a calculation, not approval of the
    parent's technology or norm. Both remain separate obligations.
    """
    from analysis_replay import digest
    from audit_models import VorPosition
    from normative_parameters import build_cards,measure
    import normative_service as service
    events=[e for e in result.get('evidence_trace',[]) if e.get('kind')==DEPENDENCY_TRACE]
    if len(events)!=1:
        return False
    event=events[0]
    proof=event.get('proof') or {}
    if event.get('sha256')!=digest(proof):
        return False
    try:
        parent=VorPosition.model_validate(proof['parent'])
        child=VorPosition.model_validate(proof['child'])
        if position is not None and (service.position_key(position)!=service.position_key(child)
                or digest(position.model_dump(mode='json'))!=digest(proof['child'])):
            return False
        if parent.index==child.index:
            return False
        available={s['id']:s for s in result.get('sources',[])}
        if any(sid not in available or digest(available[sid])!=sha
                for sid,sha in proof['source_sha256'].items()):
            return False
        proposals=[p for p in result.get('parameter_proposals',[])
            if p.get('source_id') in proof['source_sha256']]
        if digest(proposals)!=proof['proposals_sha256']:
            return False
        literal=dict(pipeline_version=4,sources=list(available.values()),
            parameter_proposals=[p for p in result.get('parameter_proposals',[])
                if p.get('parameter') in {'quantity','thickness'}],
            items=[],related_positions=proof['related_positions'])
        geometry_positions={p.index:p for p in (VorPosition.model_validate(raw)
            for raw in proof.get('assembly_positions',[]))}
        geometry_positions[parent.index]=parent
        cards={c['id']:c for c in build_cards(parent,literal,geometry_positions,{})}
        actual={c['id']:c for c in result.get('parameter_cards',[])}
        required=proof['required_parameters']
        if ('quantity' not in required or set(required)!=set(proof['facts'])
                or set(required)!={key for key in cards if key in {'quantity','thickness'}}):
            return False
        for kind in required:
            rebuilt=cards.get(kind) or {}
            current=actual.get(kind) or {}
            fact=proof['facts'][kind]
            if (rebuilt.get('state')!='confirmed' or current.get('state')!='confirmed'
                    or not _anchored_card(rebuilt,available)
                    or not _anchored_card(current,available)
                    or not set(fact['source_ids'])<=set(current.get('source_ids',[]))
                    or measure(rebuilt.get('found_value'),rebuilt.get('found_unit',''))
                        !=measure(fact['value'],fact['unit'])
                    or measure(current.get('found_value'),current.get('found_unit',''))
                        !=measure(fact['value'],fact['unit'])):
                return False
        return True
    except (KeyError,ValueError,TypeError,AttributeError):
        return False


def _clear_geometry(result,parent_index):
    """Remove only this binder's earlier partial contribution before replay."""
    output=deepcopy(result)
    prefix=f'parent-{parent_index}:geometry:'
    from normative_fact_contract import PROJECTION
    def generated(item):
        ids=item.get('project_source_ids') or []
        return bool(item.get('topic','').startswith(GEOMETRY_TOPIC)
            and item.get('state')=='confirmed' and not item.get('normative_source_ids')
            and item.get('claim_ids') in (['quantity'],['geometry:thickness'])
            and ids and all(sid.startswith(prefix) for sid in ids))
    projections=[e for e in output.get('evidence_trace',[])
        if e.get('kind')==PROJECTION and generated(e.get('original') or {})]
    def removable(item):
        return generated(item) or any(item==e.get('projected') for e in projections)
    if (not any(s['id'].startswith(prefix) for s in output['sources'])
            and not any(e.get('kind') in {GEOMETRY_TRACE,DEPENDENCY_TRACE,'parent_sum_supersedes_parts_v1','parent_geometry_supersedes_duplicate_v1'}
                and e.get('position_index')==parent_index for e in output.get('evidence_trace',[]))
            and not projections):
        return output
    output['items']=[i for i in output.get('items',[]) if not removable(i)]
    # Retire an alias still cited by another (e.g. coefficient) projection only
    # when the original literal envelope is present and exactly unchanged.
    # Otherwise preserve that citation/dispute rather than invent a new source.
    lookup={source['id']:source for source in output['sources']}
    remap={}
    def physical(source):
        return {key:value for key,value in source.items() if key not in {'id','physical_source_id'}}
    for event in output.get('evidence_trace',[]):
        if event.get('kind')!=GEOMETRY_TRACE or event.get('position_index')!=parent_index:
            continue
        for original,alias in event.get('source_mapping',{}).items():
            if (alias.startswith(prefix) and original in lookup and alias in lookup
                    and physical(lookup[original])==physical(lookup[alias])):
                remap[alias]=original
    for item in output['items']:
        item['project_source_ids']=list(dict.fromkeys(remap.get(sid,sid)
            for sid in item.get('project_source_ids',[])))
    # A new model dispute is not the binder's old derived downgrade. Retain
    # its literal citation even when the surrounding server alias is retired.
    retained={sid for i in output['items'] for sid in i.get('project_source_ids',[])}
    output['sources']=[s for s in output['sources'] if not s['id'].startswith(prefix) or s['id'] in retained]
    output['parameter_proposals']=[p for p in output.get('parameter_proposals',[])
        if not p.get('source_id','').startswith(prefix)]
    available={s['id'] for s in output['sources']}
    for event in output.get('evidence_trace',[]):
        if event.get('kind') not in {'parent_sum_supersedes_parts_v1','parent_geometry_supersedes_duplicate_v1'} or event.get('position_index')!=parent_index:
            continue
        for original in event.get('original_parameters',[]):
            if (original.get('source_id') in available
                    and all(sid in available for sid in original.get('operand_source_ids',[]))
                    and original not in output['parameter_proposals']):
                output['parameter_proposals'].append(deepcopy(original))
    output['evidence_trace']=[e for e in output.get('evidence_trace',[])
        if e not in projections and not (e.get('kind') in {GEOMETRY_TRACE,DEPENDENCY_TRACE,'parent_sum_supersedes_parts_v1','parent_geometry_supersedes_duplicate_v1'} and e.get('position_index')==parent_index)]
    return output


def _anchored_card(card,sources):
    from normative_parameters import clean
    ids=set(card.get('source_ids') or [])
    anchors=card.get('source_anchors') or []
    if not ids or {a.get('source_id') for a in anchors}!=ids:
        return False
    for anchor in anchors:
        source=sources.get(anchor.get('source_id'))
        quote=clean(anchor.get('quote',''))
        if (source is None or source['kind']!='project' or not quote
                or quote not in clean(source['text'])):
            return False
        # Retain only actual rectangles of that cited source, when available.
        if any(rect not in source.get('rects',[]) for rect in anchor.get('rects',[])):
            return False
    return True


def _proposal_dependencies(proposal,sources):
    """Check a saved sum against its actual independently readable operands."""
    from normative_parameters import number,measure
    source=sources[proposal['source_id']]
    operands=proposal.get('operand_source_ids') or []
    if proposal.get('operation','direct')=='direct':
        return [source['id']] if not operands and source['origin']!='calculated_from_cells' else []
    if (source['origin']!='calculated_from_cells' or not source.get('numeric_verified')
            or not 2<=len(operands)<=12 or len(set(operands))!=len(operands)):
        return []
    cells=[sources.get(sid) for sid in operands]
    if any(s is None or s['kind']!='project' or s['origin']=='uncertain_table'
            or (s['origin'].startswith('ocr') and not s.get('numeric_verified'))
            or not s.get('unit_verified') or measure('1',s.get('unit',''))!=measure('1',proposal['unit'])
            for s in cells):
        return []
    cells=[s for s in cells if s is not None]
    if len({(s.get('version_id'),s.get('page')) for s in [source,*cells]})!=1:
        return []
    values=[number(s['text'].rsplit('\n',1)[-1]) for s in cells]
    if any(v is None for v in values) or sum(v for v in values if v is not None)!=number(proposal['value']):
        return []
    return [source['id'],*operands]


def _reuse_geometry(result,parent,basis,positions,parent_relation,row,child=None):
    """Reuse separately proved parameters without approving the parent's work."""
    from normative_parameters import build_cards,measure
    output=_clear_geometry(result,parent.index)
    if parent_relation.get('parent'):
        return output  # Do not silently substitute a grandparent's geometry.
    sources={s['id']:s for s in basis['sources']}
    # A saved state/shortcut is not an independent proof. Rebuild from literal
    # proposals, scope, units and current OCR flags before selecting any card.
    literal={**basis,'local_work_basis':None,'quantity_composition':None}
    fresh={c['id']:c for c in build_cards(parent,literal,positions,parent_relation)}
    selected={}
    needed=set()
    for stored in basis.get('parameter_cards') or []:
        kind=stored.get('id')
        current=fresh.get(kind) or {}
        if (kind not in {'quantity','thickness'} or stored.get('state')!='confirmed'
                or current.get('state')!='confirmed' or not _anchored_card(stored,sources)
                or not _anchored_card(current,sources)
                or set(stored['source_ids'])!=set(current['source_ids'])
                or measure(stored.get('found_value'),stored.get('found_unit',''))
                    !=measure(current.get('found_value'),current.get('found_unit',''))):
            continue
        proposals=[]
        dependencies=set()
        for proposal in basis.get('parameter_proposals') or []:
            if (proposal['parameter']!=kind or proposal['source_id'] not in current['source_ids']
                    or measure(proposal['value'],proposal['unit'])
                        !=measure(current['found_value'],current['found_unit'])):
                continue
            ids=_proposal_dependencies(proposal,sources)
            if ids:
                proposals.append(proposal)
                dependencies.update(ids)
        if proposals and set(current['source_ids'])<=dependencies:
            selected[kind]=(current,proposals)
            needed.update(dependencies)
    if not selected:
        return output if output!=result else result
    # Replacing a certified fact's alias must not fabricate a second reading.
    # Require the entire physical dependency envelope, not equal numbers.
    def envelopes(proposal, lookup):
        if proposal.get('source_id') not in lookup:
            return None
        dependencies = _proposal_dependencies(proposal, lookup)
        if not dependencies:
            return None
        fields = ('version_id','page','rects','text','unit','origin','numeric_verified','unit_verified')
        physical=[]
        for sid in dependencies:
            envelope={key:lookup[sid].get(key) for key in fields}
            envelope['rects']=sorted(tuple(rect) for rect in (envelope['rects'] or []))
            physical.append(json.dumps(envelope,sort_keys=True))
        return sorted(physical)
    current_sources = {s['id']:s for s in output['sources']}
    duplicates = [proposal for proposal in output.get('parameter_proposals', [])
        if proposal.get('scope_state') == 'verified' and any(
            proposal['parameter'] == kind
            and measure(proposal['value'],proposal['unit']) == measure(original['value'],original['unit'])
            and envelopes(proposal,current_sources) is not None
            and envelopes(proposal,current_sources) == envelopes(original,sources)
            for kind,(_,proposals) in selected.items() for original in proposals)]
    if duplicates:
        output['parameter_proposals'] = [p for p in output['parameter_proposals'] if p not in duplicates]
        output.setdefault('evidence_trace', []).append(dict(kind='parent_geometry_supersedes_duplicate_v1',
            position_index=parent.index, original_parameters=deepcopy(duplicates)))
    # A whole-work sum supersedes the child's earlier direct readings of its
    # exact operands. Keeping both parts and their sum fabricates ambiguity.
    from normative_quantity_anchors import sum_supersedes_operand
    quantity = selected.get('quantity')
    if quantity:
        current_sources = {s['id']: s for s in output['sources']}
        covered = [p for p in output.get('parameter_proposals', [])
            if any(sum_supersedes_operand(p, total, sources)
                and current_sources.get(p['source_id']) == sources.get(p['source_id'])
                for total in quantity[1])]
        if covered:
            output['parameter_proposals'] = [p for p in output['parameter_proposals'] if p not in covered]
            output.setdefault('evidence_trace', []).append(dict(kind='parent_sum_supersedes_parts_v1',
                position_index=parent.index, original_parameters=deepcopy(covered)))
    mapping={sid:f'parent-{parent.index}:geometry:'+hashlib.sha256(sid.encode()).hexdigest()[:24]
        for sid in needed}
    output['sources'] += [{**deepcopy(s),'id':mapping[s['id']],
        'physical_source_id':s.get('physical_source_id',s['id'])} for s in basis['sources'] if s['id'] in needed]
    for kind,(card,proposals) in selected.items():
        for original in proposals:
            proposal=deepcopy(original)
            proposal['source_id']=mapping[original['source_id']]
            proposal['operand_source_ids']=[mapping[sid] for sid in original.get('operand_source_ids',[])]
            if proposal.get('operation')=='sum':
                for field in ('quote','scope_quote'):
                    proposal[field]=proposal.get(field,'').partition(' Ячейки:')[0].rstrip('.')
            output['parameter_proposals'].append(proposal)
        label='Количество' if kind=='quantity' else 'Толщина'
        statement='Количество подтверждено' if kind=='quantity' else 'Толщина подтверждена'
        output['items'].append(dict(topic=GEOMETRY_TOPIC+': '+label.lower(),state='confirmed',required=True,
            claim_ids=['quantity' if kind=='quantity' else 'geometry:thickness'],
            project_source_ids=[mapping[sid] for sid in card['source_ids']],normative_source_ids=[],
            explanation=f'{statement} источниками ПД для работы №{parent.number or parent.index+1}. '
                'Технология работы, нормативная применимость и расход материала проверяются отдельно.'))
    provenance={'review_id':row['id']} if row is not None else {'basis':'current_material_literal_geometry'}
    output.setdefault('evidence_trace',[]).append(dict(kind=GEOMETRY_TRACE,**provenance,position_index=parent.index,
        parameters=sorted(selected),source_mapping=mapping))
    required={key for key in fresh if key in {'quantity','thickness'}}
    if child is not None and 'quantity' in required and required<=selected.keys():
        from analysis_replay import digest
        source_ids={mapping[sid]:digest(next(s for s in output['sources'] if s['id']==mapping[sid]))
            for sid in needed}
        proposals=[p for p in output['parameter_proposals'] if p.get('source_id') in source_ids]
        proof=dict(parent=parent.model_dump(mode='json'),child=child.model_dump(mode='json'),
            required_parameters=sorted(required),related_positions=deepcopy(basis.get('related_positions',[])),
            source_sha256=source_ids,proposals_sha256=digest(proposals),
            facts={kind:dict(value=card['found_value'],unit=card['found_unit'],
                source_ids=[mapping[sid] for sid in card['source_ids']]) for kind,(card,_) in selected.items()})
        from normative_layer_assembly import layer_assembly
        assembly=layer_assembly(parent,positions)
        if assembly:
            first,last=min(assembly['work_indices']),max(assembly['work_indices'])
            proof['assembly_positions']=[p.model_dump(mode='json') for i,p in sorted(positions.items()) if first<=i<=last]
        output['evidence_trace'].append(dict(kind=DEPENDENCY_TRACE,position_index=parent.index,
            **provenance,proof=proof,sha256=digest(proof)))
    return output



def _current_material_geometry(result,parent,child,positions,record):
    """Rebuild literal current inputs when no parent review can supply geometry.

    Explicit VOR ownership only selects the work whose cards must be rebuilt;
    it never proves a material output, allocation, rate or normative condition.
    """
    from normative_parameters import build_cards
    from vor_material_ownership import material_owners
    from analysis_replay import digest
    if (child is None or child.index not in positions
            or digest(child.model_dump(mode='json'))!=digest(positions[child.index].model_dump(mode='json'))
            or not record.get('vor_version_id')):
        return result
    owner=material_owners([p.legacy_task() for p in positions.values()]).get(child.index)
    if not owner or owner['index']!=parent.index:
        return result
    pinned=set((record.get('config',{}).get('pdf_versions') or {}).values())
    # Keep only current, pinned project inputs; normative/coefficient proposals
    # remain in the child result and cannot become geometric evidence here.
    sources=[deepcopy(s) for s in result.get('sources',[])
        if s.get('kind')=='project' and s.get('version_id') in pinned]
    ids={s['id'] for s in sources}
    proposals=[deepcopy(p) for p in result.get('parameter_proposals',[])
        if p.get('parameter') in {'quantity','thickness'} and p.get('source_id') in ids
        and all(sid in ids for sid in p.get('operand_source_ids',[]))]
    basis=dict(pipeline_version=result.get('pipeline_version',4),sources=sources,
        parameter_proposals=proposals,items=[],related_positions=deepcopy(result.get('related_positions',[])))
    basis['parameter_cards']=build_cards(parent,basis,positions,{})
    return _reuse_geometry(result,parent,basis,positions,{},None,child)


def bind_parent_basis(context,record,result,positions,relation,*,expected_kb=None,position=None):
    from normative_verdict import publish,source_supported
    from audit_review_policy import review_source
    import normative_service as service
    parent_index=(relation.get('parent') or {}).get('index')
    if not isinstance(parent_index,int) or parent_index not in positions:
        return result
    parent=positions[parent_index]
    # The caller supplies the exact current child. Do not guess from amount,
    # family, or the number of siblings when several materials share a work.
    child=position
    clean=_clear_geometry(result,parent_index)
    if child is not None and (child.index not in positions or child.index==parent_index
            or str(child.position_type).casefold()!='материал'
            or str(parent.position_type).casefold()!='работа'):
        return clean
    # This recipe uses a complete PD resource total and declared allocation,
    # not the work's area or physical depth of an individual operation.
    from normative_layer_assembly import material_allocation_proof
    allocation=material_allocation_proof(child,clean,positions,relation) if child is not None else None
    if allocation:
        pinned=set((record.get('config',{}).get('pdf_versions') or {}).values())
        lookup={source['id']:source for source in clean['sources']}
        if all(lookup[sid].get('version_id') in pinned for sid in allocation['root_source_ids']):
            return clean
    row=next((r for r in context.repository.normative_reviews(record['id'],parent_index) if r['kind']=='check'),None)
    if row is None:
        return _current_material_geometry(clean,parent,child,positions,record)
    data=row['payload']
    basis=data.get('position_check') or {}
    if result.get('pipeline_version', 0) >= 5 and basis.get('pipeline_version', 0) < 5:
        return _current_material_geometry(clean,parent,child,positions,record)  # A legacy locally assembled verdict is not a new agent decision.
    if (data.get('position_key')!=service.position_key(parent) or data.get('vor_version_id')!=str(record['vor_version_id'])
            or basis.get('execution_state')!='complete' or basis.get('pipeline_version',0)<3):
        return _current_material_geometry(clean,parent,child,positions,record)
    pinned=set((record['config'].get('pdf_versions') or {}).values())
    if any(s.get('version_id') not in pinned for s in basis['sources'] if s['kind']=='project'):
        return _current_material_geometry(clean,parent,child,positions,record)
    raw=next((r['result'] for r in record['indexed_results'] if r['position_index']==parent_index),None)
    if raw is None:
        return _current_material_geometry(clean,parent,child,positions,record)
    same_kb=data.get('kb')==(expected_kb if expected_kb is not None else
        service.corpus(profile=service.profile_for(record)).identity)
    if not same_kb:
        # Project geometry is pinned to VOR and PDF, not a normative edition.
        # This path transfers only independently rebuilt project facts; no norm
        # choice, measure, rate or applicability can cross the release boundary.
        reused=_reuse_geometry(result,parent,basis,positions,data.get('work_relations') or {},row,child)
        if any(e.get('kind')==DEPENDENCY_TRACE for e in reused.get('evidence_trace',[])):
            return reused
        current=_current_material_geometry(clean,parent,child,positions,record)
        return current if any(e.get('kind')==DEPENDENCY_TRACE for e in current.get('evidence_trace',[])) else reused
    from normative_conditional_selection import apply_guard
    # A stale green norm selection cannot establish a child's full work basis.
    # The existing partial branch can still retain independently proved PD geometry.
    basis=deepcopy(basis)
    apply_guard(parent,basis,positions,data.get('work_relations') or {})
    stored=review_source(raw,parent.legacy_task())
    report=publish({'Статус аудита':'ОК' if stored.get('status')=='ОК' else 'Требуется ручная проверка','Что не так':''},
        stored,data['position_key'],{'stage':'done','review':{**basis,'position_key':data['position_key']}},enabled=True)
    if report['Статус аудита']!='ОК':
        reused=_reuse_geometry(result,parent,basis,positions,data.get('work_relations') or {},row,child)
        if any(e.get('kind')==DEPENDENCY_TRACE for e in reused.get('evidence_trace',[])):
            return reused
        current=_current_material_geometry(clean,parent,child,positions,record)
        return current if any(e.get('kind')==DEPENDENCY_TRACE for e in current.get('evidence_trace',[])) else reused
    output=_clear_geometry(result,parent_index)
    mapping={s['id']:f'parent-{parent_index}:'+s['id'] for s in basis['sources']}
    output['sources']=[s for s in output['sources'] if s['id'] not in mapping.values()]
    output['sources'] += [{**deepcopy(s),'id':mapping[s['id']],
        'physical_source_id':s.get('physical_source_id',s['id'])} for s in basis['sources']]
    output['parameter_proposals']=[p for p in output.get('parameter_proposals',[]) if p['parameter']=='coefficient']
    for p in basis.get('parameter_proposals',[]):
        if p['parameter'] not in {'quantity','thickness','unit_scale'} or p['source_id'] not in mapping:
            continue
        p=deepcopy(p)
        p['source_id']=mapping[p['source_id']]
        p['operand_source_ids']=[mapping[s] for s in p.get('operand_source_ids',[]) if s in mapping]
        if p.get('operation')=='sum':
            for field in ('quote','scope_quote'):
                p[field]=p.get(field,'').partition(' Ячейки:')[0].rstrip('.')
        output['parameter_proposals'].append(p)
    items=[]
    for item in output.get('items',[]):
        if item.get('topic')=='Проверенное основание родительской работы':
            continue
        item['claim_ids']=[c for c in item.get('claim_ids',[]) if c not in {'derived:parent','quantity'}]
        if item['claim_ids']:
            items.append(item)
    project_ids=list(dict.fromkeys(mapping[sid] for c in basis['parameter_cards'] if c['id'] in {'quantity','thickness'}
        and c['state']=='confirmed' for sid in c.get('source_ids',[])))
    norm_ids=list(dict.fromkeys(mapping[sid] for i in basis['items'] if i['state']=='confirmed'
        and 'normative:applicability' in i['claim_ids'] and source_supported(i) for sid in i['normative_source_ids']))
    items.append(dict(topic='Проверенное основание родительской работы',state='confirmed',required=True,
        claim_ids=['derived:parent','quantity'],project_source_ids=project_ids,normative_source_ids=norm_ids,
        explanation=f'Площадь и проектная толщина взяты из подтверждённого основания работы №{parent.number or parent_index+1}. '
            'Отдельная строка расчётного материала в ПД не требуется. Формула, коэффициенты и характеристики материала проверяются отдельно.'))
    output['items']=items
    output.setdefault('evidence_trace',[]).append(dict(kind='reused_parent_parameters',review_id=row['id'],position_index=parent_index))
    return output
