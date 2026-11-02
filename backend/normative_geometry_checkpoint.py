"""Retain a live, independently checked thickness fact across a mixed child gap."""
from copy import deepcopy
import hashlib
import json
import re

from normative_parameters import build_cards, clean, construction_conflict

EVENT='retained_geometry_claim_v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def _geometry(claims):
    return {claim for claim in claims if claim.startswith('geometry:') and 'thickness' in claim}


def _explicit_geometry_doubt(item):
    """Conservative veto only; wording never supplies a positive proof."""
    for sentence in re.split(r'(?<=[.!?;])\s+',clean(item['explanation'])):
        if not re.search(r'толщин|конструкц|участ|геометр|принадлеж|привяз|сло[йяею]|\bос[ьи]',sentence):
            continue
        sentence=re.sub(r'противоречи[йя]\s+нет|нет\s+противоречи[йя]', '', sentence)
        if re.search(r'не\s+(?:подтвержд|установ|доказ|соответств)|\bнеяс|противореч|\bдруг(?:ой|ому|ая|ого)\b|\bиной\b|требует\w*\s+(?:подтвержд|сверк|провер)',sentence):
            return True
    return False


def reconcile_geometry(answer,sources,tools,position,positions,relation,related,known_claims=None):
    """Use only this live call's prior checkpoint, never infer legacy states.

    The last answer must keep the same verified literal parameter. A separate
    geometry doubt, changed source/pin, or changed layer context revokes reuse.
    Only a geometry claim mixed with a known child's rate can be separated.
    The residual item and all normative/rate decisions remain unresolved.
    """
    from normative_local_checks import layer_group
    from normative_parameter_anchors import resolve_anchors
    from normative_position_check import validate_items
    if relation.get('parent'):
        return
    current=answer.model_copy(deep=True)
    raw_proposals=[p.model_dump() for p in answer.parameters if p.parameter=='thickness']
    group=layer_group(position,related)
    work=position.work_name+' '+position.work_name+' '+' '.join(c.get('work','') for c in relation.get('children',[]))
    current.parameters=resolve_anchors(current.parameters,sources,current.items,work_scope=work,
        height_cm=group['height'] if group else None,owner_unit=position.unit)
    proposals=[p.model_dump() for p in current.parameters if p.parameter=='thickness']
    lookup={s['id']:s for s in sources}
    context=_digest(dict(position=position.model_dump(mode='json'),relation=relation,related=related))
    checkpoint=getattr(tools,'geometry_checkpoint',None)
    if (len(proposals)!=1 or proposals[0].get('scope_state')!='verified'
            or proposals[0].get('operation')!='direct'):
        tools.geometry_checkpoint=None
        return
    proposal=proposals[0]
    if (len(raw_proposals)!=1 or any(clean(raw_proposals[0].get(field,'').replace('\\n','\n'))
            !=clean(proposal.get(field,'')) for field in ('quote','scope_quote'))):
        # Ordinary anchor repair can resolve a malformed description. A live
        # checkpoint is stricter: only whitespace/escaped newlines may differ,
        # never an invented or changed role/location that happens to share 24.
        tools.geometry_checkpoint=None
        return
    source=lookup.get(proposal['source_id'])
    if (not source or source.get('kind')!='project' or source.get('origin')!='native'
            or not source.get('version_id') or not source.get('page')
            or len(lookup)!=len(sources)):
        tools.geometry_checkpoint=None
        return

    def snapshot(items):
        return dict(pipeline_version=4,execution_state='complete',sources=sources,
            items=validate_items(items,sources,known_claims),
            parameter_proposals=[p.model_dump() for p in current.parameters],related_positions=related)

    def card(items):
        return next((c for c in build_cards(position,snapshot(items),positions,relation) if c['id']=='thickness'),None)

    if checkpoint:
        stable=(checkpoint['context']==context and checkpoint['proposal']==proposal
            and checkpoint['raw_proposal']==raw_proposals[0]
            and all(sid in lookup and _digest(lookup[sid])==fingerprint
                for sid,fingerprint in checkpoint['sources'].items()))
        wanted=set(checkpoint['item']['claim_ids'])
        affected=[item for item in current.items if set(item.claim_ids)&wanted]
        mixed=affected[0] if len(affected)==1 else None
        children={f"related:{c['material_index']}:rate" for c in relation.get('children',[]) if 'material_index' in c}
        child_claims={claim for claim in mixed.claim_ids if claim.startswith('related:')} if mixed else set()
        eligible=(stable and mixed is not None and mixed.state=='needs_facts'
            and wanted<=set(mixed.claim_ids) and bool(child_claims) and child_claims<=children
            and not (_geometry(mixed.claim_ids)-wanted) and not _explicit_geometry_doubt(mixed.model_dump())
            and proposal['source_id'] in mixed.project_source_ids
            and not any(item is not mixed and item.state!='confirmed'
                and any(claim.startswith('geometry:') for claim in item.claim_ids) for item in current.items))
        if eligible and mixed is not None:
            for sid in mixed.project_source_ids:
                if sid in checkpoint['sources']:
                    continue
                extra=lookup.get(sid)
                if (not extra or extra.get('kind')!='project' or extra.get('version_id')!=source['version_id']
                        or extra.get('origin')!='calculated_from_cells' or not extra.get('numeric_verified')
                        or not extra.get('unit_verified')
                        or construction_conflict(extra.get('text',''),work,group['height'] if group else None)):
                    eligible=False
                    break
        if eligible and mixed is not None:
            residual=mixed.model_copy(update={'claim_ids':[c for c in mixed.claim_ids if c not in wanted]})
            retained=type(mixed).model_validate(checkpoint['item'])
            revised=[]
            for item in current.items:
                revised.extend([retained,residual] if item is mixed else [item])
            checked=card(revised) if len(revised)<=8 else None
            if checked and checked.get('state')=='confirmed' and _digest(checked)==checkpoint['card']:
                answer.items=revised
                tools.events.append(dict(kind=EVENT,original_item=mixed.model_dump(),
                    retained_item=deepcopy(checkpoint['item']),proposal=deepcopy(proposal),
                    source_fingerprints=deepcopy(checkpoint['sources']),context_fingerprint=context,
                    reason='same_live_verified_thickness_mixed_with_child_rate'))
                return
        # A changed or independently uncertain claim cannot inherit history.
        tools.geometry_checkpoint=None

    confirmed=[item for item in current.items if item.state=='confirmed' and item.claim_ids
        and set(item.claim_ids)==_geometry(item.claim_ids) and proposal['source_id'] in item.project_source_ids
        and not _explicit_geometry_doubt(item.model_dump())]
    if len(confirmed)!=1:
        return
    checked=card(current.items)
    item=confirmed[0].model_dump()
    ids=set(item['project_source_ids']+item['normative_source_ids'])
    if (not checked or checked.get('state')!='confirmed' or not ids<=lookup.keys()
            or any(sid not in lookup or not lookup[sid].get('text') for sid in ids)):
        return
    tools.geometry_checkpoint=dict(context=context,proposal=deepcopy(proposal),raw_proposal=deepcopy(raw_proposals[0]),item=item,
        sources={sid:_digest(lookup[sid]) for sid in ids},card=_digest(checked))
