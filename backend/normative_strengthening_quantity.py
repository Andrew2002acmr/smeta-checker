"""Bind a complete physical area pair to one explicit native strengthening layer.

Only already located table pages are read. No model, expected VOR quantity,
neighbouring layer quantity or normative conclusion supplies the area.
"""
from copy import deepcopy
import re

from normative_construction_scope import verified_layer
from normative_parameters import measure, number
from normative_quantity_sum_repair import prepare_quantity_pair

TRACE='explicit_strengthening_quantity_v1'


def apply(context,record,index,position,result,kb):
    if verified_layer(position,result) is None:
        return False
    from normative_agent import EvidenceTools
    return project(position,result,EvidenceTools(context,record,index,[],kb,{}))


def project(position,result,tools):
    layer=verified_layer(position,result)
    if not layer or layer['version_id']!=tools.version or measure('1',position.unit)!=measure('1','м2'):
        return False
    pages=sorted({s['page'] for s in result['sources'] if s.get('version_id')==tools.version
        and ':cell:' in s.get('id','') and type(s.get('page')) is int and s['page']>0})
    if not 1<=len(pages)<=4:
        return False
    from normative_agent import EvidenceRequest
    from normative_source_registry import SourceRegistryConflict
    # All previously located physical tables participate; never pick the pair
    # whose sum happens to match the expected VOR amount.
    for page in pages:
        for table in tools._tables(page):
            totals=[c['row'] for c in table.get('cells',[]) if c['column']==0
                and re.fullmatch(r'\s*Всего\s*[:.]?\s*',c['text'],re.I)]
            if totals:
                try:
                    tools.execute(EvidenceRequest(tool='read_table',page=page,
                        table_id=table['table_id'],row_ids=[max(totals)]))
                except SourceRegistryConflict:
                    return False  # Preserve the review; conflicting reads cannot prove it.
    sections=getattr(position.position_source,'sections',[]) if position.position_source else []
    section=' '.join(s.get('text','') if isinstance(s,dict) else s.text for s in sections)
    candidate=prepare_quantity_pair(tools,owner_work=position.work_name,
        work_scope=position.work_name+' '+layer['scope_quote'],owner_unit=position.unit,
        section_context=section)
    if candidate is None:
        return False
    proposal={**candidate['proposal'],'scope_state':'verified'}
    ids=[*proposal['operand_source_ids'],proposal['source_id']]
    fresh=[s for s in tools.sources if s['id'] in ids]
    previous=[s for s in result['sources'] if s['id'] in ids]
    # A canonical cell cannot change its literal value, units or PDF location.
    for old in previous:
        new=next(s for s in fresh if s['id']==old['id'])
        if (any(old.get(k)!=new.get(k) for k in ('version_id','page','rects','origin','numeric_verified','unit','unit_verified'))
                or number(old['text'].rsplit('\n',1)[-1])!=number(new['text'].rsplit('\n',1)[-1])):
            return False
    original=[p for p in result.get('parameter_proposals',[]) if p.get('parameter')=='quantity']
    if any(p.get('scope_state')=='verified' and p!=proposal for p in original):
        return False  # Conflicting scoped evidence needs resolution, not replacement.
    if original==[proposal] and all(s in result['sources'] for s in fresh):
        return False
    result['sources']=[s for s in result['sources'] if s['id'] not in ids]+deepcopy(fresh)
    result['parameter_proposals']=[p for p in result.get('parameter_proposals',[]) if p.get('parameter')!='quantity']+[proposal]
    from normative_parameters import build_cards
    card=next(c for c in build_cards(position,result,{position.index:position},{}) if c['id']=='quantity')
    superseded=[]
    if card['state']=='confirmed':
        superseded=[i for i in result.get('items',[]) if i.get('state')=='needs_facts'
            and i.get('claim_ids')==['quantity'] and not i.get('normative_source_ids')]
        if superseded:
            result['items']=[i for i in result['items'] if i not in superseded]+[dict(
                topic='Количество работы по ПД',state='confirmed',required=True,claim_ids=['quantity'],
                explanation='Полный итог собственных колонок слоя: '+proposal['quote']+'. '+card['reason'],
                project_source_ids=ids,normative_source_ids=[])]
    result.setdefault('evidence_trace',[]).append(dict(kind=TRACE,layer=layer,
        original_proposals=deepcopy(original),original_sources=deepcopy(previous),
        superseded_quantity_items=deepcopy(superseded),
        replacement_proposal=deepcopy(proposal),physical_provenance=candidate['provenance'],
        physical_trace=deepcopy(tools.events),
        scope='Unique explicit street strengthening layer, same mix and literal thickness; complete left/right physical totals. Normative claims unchanged.'))
    return True
