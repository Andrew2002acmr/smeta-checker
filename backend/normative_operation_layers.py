"""Negative guard for explicit per-operation thickness limits in norm tables."""
import re

from normative_parameters import clean, measure

TABLE = r'\d{2}-\d{2}-\d{3}'
RULE = re.compile(r'(?:^|(?<=[.!?]) )нормами табл\. (?P<tables>'+TABLE+r'(?:, '+TABLE+r')*) '
    r'пользоваться при толщине слоя до (?P<limit>\d+(?:[.,]\d+)?) см\. '
    r'при толщине слоя более (?P<over>\d+(?:[.,]\d+)?) см его устройство '
    r'нормировать в два слоя \(для каждого слоя отдельно\) по нормам табл\. '
    r'(?P<bases>'+TABLE+r'(?:, '+TABLE+r')*) с корректировкой нормами табл\. '
    r'(?P<adjustments>'+TABLE+r'(?: и '+TABLE+r')*)\.')
TRACE = 'operation_layer_requirement_v1'


def findings(position, result, positions, relation, cards):
    from normative_conditional_selection import _readable
    from normative_layer_conditions import _selected_base, _same_family
    if result.get('pipeline_version', 0) < 3 or result.get('execution_state') == 'technical_failure':
        return []
    owner = positions.get((relation.get('parent') or {}).get('index'), position)
    # Read only the explicitly declared height of this operation, never a
    # combined layer-group total or a same-height neighbouring PD paragraph.
    heights = list(re.finditer(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', owner.work_name, re.I))
    if len(heights) != 1:
        return []
    height = measure(heights[0][1], heights[0][2])
    applications = [i for i in result.get('items', []) if 'normative:applicability' in i.get('claim_ids', [])]
    selected = {sid for item in applications for sid in item.get('normative_source_ids', [])}
    base = _selected_base({**result, 'parameter_cards': cards}, selected)
    if not base or not height:
        return []
    base_source, definition = base
    output = []
    for source in result['sources']:
        if (source.get('kind') != 'normative' or not _readable(source)
                or source.get('source_id') != definition['source_id'] or not _same_family(source, definition)):
            continue
        # PDF line wrapping may split a table code immediately after a hyphen.
        text = re.sub(r'(?<=-)\s+(?=\d)', '', clean(source['text']).casefold())
        for rule in RULE.finditer(text):
            limit = measure(rule['limit'], 'см')
            if (rule['limit'] != rule['over'] or not limit or height[0] != limit[0]
                    or height[1] <= limit[1] or definition['table_code'] not in rule['tables'].split(', ')
                    or definition['table_code'] not in rule['bases'].split(', ')):
                continue
            output.append(dict(kind=TRACE, source_id=source['id'], base_source_id=base_source['id'],
                quote=rule[0], declared_operation=owner.work_name, height=heights[0][0]))
    return output


def explanation(finding):
    return ('В ВОР заявлена толщина одной операции '+finding['height']+'. Условие выбранного норматива '
        'требует нормировать два слоя отдельно. Применимость расчёта одной операции на полную толщину '
        'не подтверждена. Проверьте разбиение и основание каждого слоя. Это не доказанная ошибка ВОР '
        'и не подтверждение принадлежности толщины ПД. Условие: «'+finding['quote']+'».')


def apply_guard(position, result, positions, relation, cards):
    found = findings(position, result, positions, relation, cards)
    result['evidence_trace'] = [e for e in result.get('evidence_trace', []) if e.get('kind') != TRACE] + found
    if not found:
        return False
    message = explanation(found[0])
    for item in result.get('items', []):
        if 'normative:applicability' in item.get('claim_ids', []) and item['state'] == 'confirmed':
            item['state'] = 'needs_facts'
            item['explanation'] += ' ' + message
    # Keep a separate blocking obligation even if another broad applicability
    # claim is later confirmed. No numeric card or source is changed here.
    item = dict(topic='Послойное нормирование операции', state='needs_facts', required=True,
        claim_ids=['normative:operation_layers'], project_source_ids=[],
        normative_source_ids=[found[0]['source_id'], found[0]['base_source_id']], explanation=message)
    if item not in result['items']:
        result['items'].append(item)
    if result.get('state') == 'confirmed':
        result['state'] = 'needs_facts'
    return True
