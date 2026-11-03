"""Narrow, replayable corrections of reproducible frozen-parser mistakes."""
from copy import deepcopy
import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any

from audit_predicates import technical_specs,comment_metadata,typed_paver_comment,paver_class_condition,_LABEL_RE

VERSION='claim-parser-corrections-v1'
TYPED_PAVER='parser_equipment_model_with_explicit_class'
EQUIPMENT_NAME='parser_equipment_name_with_separate_required_function'
CONDITION_CONFLICT='typed_machine_condition_conflict_v1'


def _item_digest(item):
    return hashlib.sha256(json.dumps(item,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def record_condition_conflict(original, row, position, trace):
    """Keep the original negative state even when a later source gate downgrades it."""
    from audit_models import VorPosition
    from human_basis import position_key
    if (not isinstance(position, VorPosition) or trace is None
            or original.get('state') != 'possible_mismatch'):
        return
    shared=set(original.get('claim_ids',[]))&set(row.get('claim_ids',[]))
    paver='attribute:comment:condition' in shared and typed_paver_comment(position.work_name,position.comment) is not None
    named='attribute:comment:equipment_names' in shared and corrections(position)['retired'].get(
        'attribute:comment:equipment_names')==EQUIPMENT_NAME
    if not paver and not named:
        return
    event=dict(kind=CONDITION_CONFLICT,position_key=position_key(position),
        item_sha256=_item_digest(row),original_state='possible_mismatch',
        original_claim_ids=list(original['claim_ids']))
    if event not in trace:
        trace.append(event)


def corrections(position) -> dict[str, Any]:
    inputs={k:getattr(position,k,'') for k in ('work_name','comment','position_type','formula')}
    specs=technical_specs(inputs['work_name'],inputs['comment'],inputs['position_type'],[],inputs['formula'])
    retired={}
    added=[]
    equipment_names=[s for s in specs if s.claim_id=='attribute:comment:equipment_names']
    if len(equipment_names)==1 and not equipment_names[0].required and equipment_names[0].predicate=='advisory':
        retired['attribute:comment:equipment_names']=EQUIPMENT_NAME
    for origin,text in (('name',inputs['work_name']),('comment',inputs['comment'])):
        for index,label in enumerate(_LABEL_RE.finditer(text),1):
            key=f'geometry:{origin}:area:{index}'
            replacement=next((s for s in specs if s.claim_id==f'geometry:{origin}:thickness:{index}'
                and re.match(r'h\s*=',s.label,re.I)),None)
            if label.lastgroup=='area' and replacement:
                retired[key]='parser_area_captured_explicit_thickness'
                added.append(replacement.model_dump())
    if comment_metadata(inputs['comment']) is not None:
        retired['attribute:comment:condition']='parser_metadata_and_equipment_name'
    elif typed_paver_comment(inputs['work_name'], inputs['comment']) is not None:
        retired['attribute:comment:condition']=TYPED_PAVER
    added.extend(s.model_dump() for s in specs if s.claim_id.startswith('method:comment:equipment:'))
    if paver_class_condition(inputs['comment']) and typed_paver_comment(inputs['work_name'], inputs['comment']) is None:
        added.extend(s.model_dump() for s in specs if s.claim_id == 'attribute:comment:condition' and s.required)
    return dict(inputs=inputs,retired=retired,added=added)


def _normalize(stored, correction):
    result=deepcopy(stored)
    checks=result.setdefault('checks',[])
    existing={c.get('claim_id') for c in checks}
    for check in checks:
        reason=correction['retired'].get(check.get('claim_id'))
        if reason in {TYPED_PAVER,EQUIPMENT_NAME} and check.get('state') in {'contradiction', 'source_conflict'}:
            continue
        if reason:
            check.update(required=False,state='not_applicable',reasons=[reason])
    for spec in correction['added']:
        if spec['claim_id'] not in existing:
            checks.append(dict(claim_id=spec['claim_id'],kind=spec['kind'],state='not_found',required=True,
                reasons=['corrected_parser_requirement'],facts=[]))
    return result


def normalize_matching(position, stored):
    return _normalize(stored,corrections(position))


def apply_review_migration(position, result):
    correction=corrections(position)
    if not correction['retired'] and not correction['added']:
        return
    from human_basis import position_key
    key=position_key(position)
    prior=[e for e in result.get('evidence_trace',[]) if e.get('kind')==VERSION]
    originals=[i for e in prior for i in e.get('original_items',[])]
    items=[]
    for item in result.get('items',[]):
        affected=set(item.get('claim_ids',[])) & correction['retired'].keys()
        original_conflict=any(e.get('kind') == CONDITION_CONFLICT and e.get('position_key') == key
            and e.get('item_sha256') == _item_digest(item) and e.get('original_state') == 'possible_mismatch'
            and set(e.get('original_claim_ids',[]))&set(item.get('claim_ids',[]))&{
                'attribute:comment:condition','attribute:comment:equipment_names'}
            for e in result.get('evidence_trace', []))
        if item.get('state') == 'possible_mismatch' or original_conflict:
            affected={claim for claim in affected if correction['retired'][claim] not in {TYPED_PAVER,EQUIPMENT_NAME}}
        if not affected:
            items.append(item)
            continue
        if item not in originals and item.get('topic')!='Уточнение требований из ВОР':
            originals.append(deepcopy(item))
        remaining=[c for c in item['claim_ids'] if c not in affected]
        if remaining:
            items.append({**item,'claim_ids':remaining})
        items.append(dict(topic='Уточнение требований из ВОР',claim_ids=sorted(affected),
            state='needs_facts',required=False,project_source_ids=[],normative_source_ids=[],
            explanation='Исправлена классификация исходного текста ВОР: служебная информация о ПО и марке '
                'не требует буквального совпадения в ПД; явно указанная h проверяется как толщина. '
                'Способ работы и технические характеристики проверяются отдельно.'))
    result['items']=items
    digest=hashlib.sha256(json.dumps(correction['inputs'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    event=dict(kind=VERSION,position_key=key,input_sha256=digest,
        **correction,original_items=originals)
    result['evidence_trace']=[e for e in result.get('evidence_trace',[]) if e.get('kind')!=VERSION]+[event]


def migrated_matching(stored, review, position_key):
    """Use only a freshly reproducible server trace pinned to this VOR position."""
    events=[e for e in review.get('evidence_trace',[]) if e.get('kind')==VERSION]
    if len(events)!=1 or events[0].get('position_key')!=position_key:
        return stored
    event=events[0]
    inputs=event.get('inputs')
    if (not isinstance(inputs,dict) or set(inputs)!={'work_name','comment','position_type','formula'}
            or not all(isinstance(v,str) for v in inputs.values())):
        return stored
    digest=hashlib.sha256(json.dumps(inputs,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if digest!=event.get('input_sha256'):
        return stored
    correction=corrections(SimpleNamespace(**inputs))
    if any(correction[k]!=event.get(k) for k in ('retired','added')):
        return stored
    return _normalize(stored,correction)
