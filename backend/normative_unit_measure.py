"""Project an independently validated work/norm measure into the unit claim.

Norm applicability and material consumption remain separate. A prior generated
item is restored before every replay so missing evidence revokes the projection.
"""
from copy import deepcopy

from analysis_replay import digest
from normative_parameters import measure, unit_scale_quoted

TRACE='validated_work_norm_measure_v1'
TOPIC='Единица работы и измеритель выбранной нормы'


def literal_definition_card(result, position):
    """Read the definition's measure without selecting or approving its norm."""
    import re

    from normative_conditional_selection import TRACE as CONDITION
    from normative_source_registry import full_text_handle, normative_evidence_identity
    from normative_thickness_rules import definition_text, source_definition

    if result.get('execution_state') != 'complete':
        return None
    sources = {source['id']: source for source in result.get('sources', [])}
    cited = {sid for item in result.get('items', []) if 'normative:applicability' in item.get('claim_ids', [])
             for sid in item.get('normative_source_ids', [])}
    definitions = [(source, definition) for source in sources.values()
                   if (definition := source_definition(source))]
    candidates = [(source, definition) for source, definition in definitions if source['id'] in cited]
    conditions = {(event['allowed_base_codes'][0], sources[event['condition_source_id']].get('source_id'))
                  for event in result.get('evidence_trace', []) if event.get('kind') == CONDITION
                  and event.get('reason') == 'base_selection_ambiguous'
                  and not event.get('independent_literal_premise')
                  and len(event.get('allowed_base_codes', [])) == 1
                  and normative_evidence_identity(sources.get(event.get('condition_source_id'), {}))}
    if not candidates and len(conditions) == 1:
        candidates = [(s, d) for s, d in definitions if (d['code'], d['source_id']) in conditions]
    if len({normative_evidence_identity(s) for s, _ in candidates}) != 1:
        return None
    source, definition = candidates[0]
    prefix, marker, _ = source['id'].rpartition(':full:')
    if (not normative_evidence_identity(source) or not marker
            or source['id'] != full_text_handle(prefix, source['text'])):
        return None
    lines = re.findall(r'^\s*(Измеритель\s*:[^\r\n]+)', definition_text(definition['text']), re.I | re.M)
    if len(lines) != 1:
        return None
    quote = lines[0].strip()
    literal = re.fullmatch(r'Измеритель\s*:\s*(?:(\d+(?:[.,]\d+)?)\s+)?(м[23²³]?|шт\.?|т|кг|га)', quote, re.I)
    if not literal:
        return None
    value, unit = literal[1] or '1', literal[2].lower()
    parsed = measure(value, unit)
    if not parsed or parsed[1] <= 0 or not unit_scale_quoted(value, unit, quote, source['text']):
        return None
    sid = source['id']
    if any(card['id'] == 'unit_scale:'+sid for card in result.get('parameter_cards', [])):
        return None
    return dict(id='unit_scale:'+sid, role='unit_scale', label='Измеритель нормативного определения',
        unit=position.unit, declared_value=position.volume, found_value=value, found_unit=unit,
        state='confirmed', required=False, source_ids=[sid], quote=quote,
        source_anchors=[dict(source_id=sid, quote=quote, rects=[])],
        reason=f'Измеритель определения {definition["code"]} прочитан в закреплённом источнике. '
               'Выбор нормы, её применимость и расчёт проверяются отдельно.')


def apply(position,result,positions,relation):
    owner=positions.get((relation.get('parent') or {}).get('index'),position)
    if owner.index!=position.index:
        return False  # A material's own mass unit is a different claim.
    previous=[e for e in result.get('evidence_trace',[]) if e.get('kind')==TRACE]
    generated=[i for i in result.get('items',[]) if i.get('topic')==TOPIC]
    if generated:
        if (len(previous)==len(generated)==1 and previous[0].get('item_sha256')==digest(generated[0])
                and previous[0].get('original_sha256')==digest(previous[0].get('original_items'))):
            originals=deepcopy(previous[0]['original_items'])
        else:
            originals=[dict(topic='Единица работы требует подтверждения',state='needs_facts',required=True,
                claim_ids=['unit'],project_source_ids=[],normative_source_ids=[],
                explanation='Сохранённое доказательство измерителя не прошло повторную проверку.')]
        result['items']=[i for i in result['items'] if i not in generated]+originals
    result['evidence_trace']=[e for e in result.get('evidence_trace',[]) if e.get('kind')!=TRACE]
    items=[i for i in result.get('items',[]) if i.get('claim_ids')==['unit']]
    if not items or any(i.get('state')!='needs_facts' for i in items):
        return False
    cards=result.get('parameter_cards',[])
    quantities=[c for c in cards if c.get('id')=='quantity' and c.get('state') in {'confirmed','possible_mismatch'}]
    if len(quantities)!=1:
        return False
    quantity=quantities[0]
    unit=measure('1',owner.unit)
    if not unit or measure('1',quantity.get('found_unit',''))!=unit or not quantity.get('source_ids'):
        return False
    sources={s['id']:s for s in result.get('sources',[])}
    if any(sid not in sources or sources[sid].get('kind')!='project'
            or not sources[sid].get('version_id') for sid in quantity['source_ids']):
        return False
    selected={sid for i in result['items'] if 'normative:applicability' in i.get('claim_ids',[])
        for sid in i.get('normative_source_ids',[])}
    from normative_layer_conditions import _selected_base
    base=_selected_base(result,selected)
    if not base:
        return False
    source,definition=base
    origin=source.get('origin','')
    if (not origin or origin=='uncertain_table'
            or origin.startswith('ocr') and source.get('numeric_verified') is not True):
        return False
    scales=[c for c in cards if c.get('id')=='unit_scale:'+source['id']]
    if len(scales)!=1 or scales[0].get('state')!='confirmed' or scales[0].get('source_ids')!=[source['id']]:
        return False
    scale=scales[0]
    value=measure(scale.get('found_value'),scale.get('found_unit',''))
    if (not value or value[0]!=unit[0] or value[1]<=0
            or not unit_scale_quoted(scale['found_value'],scale['found_unit'],scale.get('quote',''),source['text'])):
        return False
    # Unit items citing another norm may be a genuine unresolved conflict.
    if any(set(i.get('normative_source_ids',[]))-{source['id']} for i in items):
        return False
    item=dict(topic=TOPIC,state='confirmed',required=any(i.get('required',True) for i in items),
        claim_ids=['unit'],project_source_ids=list(quantity['source_ids']),normative_source_ids=[source['id']],
        explanation='Единица работы ВОР — '+owner.unit+'; '+scale['quote']+'. '
            'Размерности совпадают, масштаб учтён программно. Применимость нормы и расход материалов проверяются отдельно.')
    result['items']=[i for i in result['items'] if i not in items]+[item]
    result['evidence_trace'].append(dict(kind=TRACE,original_items=deepcopy(items),original_sha256=digest(items),
        item_sha256=digest(item),norm_code=definition['code'],norm_source_id=source['id'],
        quantity_card=deepcopy(quantity),scale_card=deepcopy(scale)))
    return True
