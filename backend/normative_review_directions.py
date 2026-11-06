"""Separate project, normative and dependency conclusions over validated evidence."""
import re

from audit_core import build_claim_specs
from normative_verdict import absence_claim, source_supported, verified_allocation_dependency
from normative_parent_basis import verified_geometry_dependency
from normative_parameters import measure
from normative_verification_contract import contract
from normative_workbook_arithmetic import arithmetic_claims
from normative_workbook_conflicts import PROOF as WORKBOOK_CONFLICT
from normative_workbook_rate import project_consumption_supported


# Only an isolated equipment specification is a method check. Arbitrary
# separate notes may contain mandatory project specifications.
EQUIPMENT_NOTE = re.compile(r'\s*(?:устройство|разравнивание|планировка|уплотнение)\s+'
    r'(?:основания|покрытия|слоя)\s+производится\s+'
    r'(?:бульдозером|автогрейдером|катком)\s+мощностью\s+\d+(?:[.,]\d+)?\s+кВт[.\s]*', re.I)
NORM_LAYER_NOTE = re.compile(r'\b(?:ГЭСН|ФЕР|ТЕР)\s+\d+.*\bп\.\s*\d+(?:\.\d+)+'
    r'.*\bпри\s+толщин.*\bнормировать\b', re.I)


def direction(key, spec=None, *, comment=''):
    if key.startswith(('derived:', 'related:', 'basis:', 'quantity:consumption')) or key == 'input:area':
        return 'relationships'
    if (key.startswith(('normative:', 'method', 'unit_scale:'))
            or getattr(spec, 'property_label', '') == 'normative_instruction'
            or key.endswith(':separate_notes') and spec and EQUIPMENT_NOTE.fullmatch(spec.expected_text)
            or key.startswith('geometry:comment:thickness:') and spec and any(
                spec.label in line and NORM_LAYER_NOTE.search(line) for line in comment.splitlines())):
        return 'normative'
    return 'project'


def state_for(entries):
    blocking = [entry for entry in entries if entry['policy'] == 'blocking']
    if any(entry['state'] == 'possible_mismatch' for entry in blocking):
        return 'possible_mismatch'
    if any(entry['state'] == 'needs_facts' for entry in blocking):
        return 'needs_facts'
    return 'confirmed' if blocking else 'not_applicable'


def assess(position, result, matching, plan, *, derived=False, relation=None):
    relation = relation or {}
    comment = getattr(position, 'comment', '') or ''
    specs = {spec.claim_id: spec for spec in build_claim_specs(position)}
    checks = {check['claim_id']: check for check in matching.get('checks', []) if check.get('claim_id')}
    actions = {action['key']: action for action in plan['actions']}
    items = result.get('items', [])
    cards = {card['id']: card for card in result.get('parameter_cards', [])}
    sources = {source['id']: source for source in result.get('sources', [])}
    entries = []

    def add(key, domain, state, policy, requirement, basis, evidence, ids=(), reason='', *, proved=False):
        entries.append(dict(key=key, direction=domain, state=state, policy=policy,
            requirement=requirement, obligation_basis=basis, evidence_required=evidence,
            source_ids=sorted(set(ids) & sources.keys()), reason=reason,
            mismatch_level=('proved' if proved else 'suspected') if state == 'possible_mismatch' else None))

    def supported(item):
        return (source_supported(item) and all(
                    sources.get(sid, {}).get('kind') == kind
                    for kind in ('project','normative') for sid in item.get(kind+'_source_ids', []))
                and project_consumption_supported(item, list(sources.values()), position=position,
                    parameters=result.get('parameter_proposals', []), items=items))

    quantity = cards.get('quantity')
    qty_bound = bool(quantity and quantity['state'] == 'confirmed' and quantity.get('source_ids')
        and all(sources.get(sid, {}).get('kind') == 'project' and sources[sid].get('version_id')
                for sid in quantity['source_ids'])
        and (unit := measure('1', position.unit))
        and measure('1', quantity.get('found_unit', '')) == unit)
    arithmetic = arithmetic_claims(result)
    parent_proof = derived and (verified_geometry_dependency(result, position)
                               or verified_allocation_dependency(result, position))
    keys = {*specs, *checks, 'normative:applicability',
            *(key[6:] for key in actions if key.startswith('claim:')),
            *(key for item in items for key in item.get('claim_ids', []))}
    if derived:
        keys.update(('derived:parent', 'derived:rate'))
    if getattr(position, 'formula', '') or getattr(position, 'calculation', None):
        keys.add('workbook:formula')
    for key in sorted(keys):
        spec, check = specs.get(key), checks.get(key, {})
        verification = contract(key, spec)
        candidates = [item for item in items if key in item.get('claim_ids', [])]
        required_items = [item for item in candidates if item.get('required', True)]
        action = actions.get('claim:'+key, {})
        reasons = check.get('reasons', [])
        policy = 'blocking'
        if check.get('state') == 'not_applicable' and reasons and not required_items and not action:
            # Additional VOR dimensions were formerly skipped by the matching
            # presentation layer. This is not proof of per-operation thickness.
            if reasons != ['additional_vor_dimension']:
                policy = 'not_applicable'
        if policy == 'blocking' and not required_items and not action and (
                spec is not None and not spec.required or spec is None and candidates):
            policy = 'advisory'
        scoped = required_items if policy == 'blocking' else candidates
        ids = [sid for i in scoped for field in ('project_source_ids', 'normative_source_ids') for sid in i.get(field, [])]
        state = 'needs_facts'
        negative = next((i for i in scoped if i['state']=='possible_mismatch' and supported(i) and not absence_claim(i)), None)
        if policy == 'not_applicable':
            state = 'not_applicable'
        elif negative or check.get('state') in {'contradiction', 'source_conflict'}:
            state = 'possible_mismatch'
        elif (key in arithmetic or key == 'derived:parent' and parent_proof
              or not action and scoped and all(i['state']=='confirmed' and supported(i) for i in scoped)):
            state = 'confirmed'
        elif not derived and quantity and qty_bound and key in {'presence', 'quantity', 'unit'} and not action:
            state = 'confirmed'
            ids += quantity['source_ids']
        elif (key.startswith('geometry:name:thickness:') and spec and 'thickness' in cards
              and cards['thickness']['state']=='confirmed' and cards['thickness'].get('source_ids')
              and (parts := spec.expected_text.split()) and len(parts) == 2
              and (expected := measure(parts[0], parts[1]))
              and expected == measure(cards['thickness'].get('found_value'), cards['thickness'].get('found_unit'))):
            state = 'confirmed'
            ids += cards['thickness']['source_ids']
        add('claim:'+key, direction(key,spec,comment=comment), state, policy,
            spec.label if spec else key, ', '.join(reasons) or 'Проверка по исходному требованию ВОР и плану доказательств',
            verification['source_routes'], [*ids, *action.get('source_ids', [])],
            (negative or {}).get('explanation') or action.get('reason') or
            ('Требуется проверяемая связь с источником.' if state=='needs_facts' else ''))

    for key, card in cards.items():
        domain = 'relationships' if derived and key in {'quantity','thickness'} and not qty_bound else direction(key)
        if key.startswith('factor:'):
            domain = 'relationships'
        state = card['state']
        ids = card.get('source_ids', [])
        if key in {'quantity','thickness','input:area'} and state=='confirmed' and not (
                ids and all(sources.get(sid, {}).get('kind')=='project' and sources[sid].get('version_id') for sid in ids)):
            state = 'needs_facts'
        proved = state == 'possible_mismatch' and key in {'quantity','thickness','input:area'} and bool(ids) and all(
            sources.get(sid, {}).get('kind')=='project' and sources[sid].get('version_id') for sid in ids)
        add('card:'+key, domain, state, 'blocking' if card.get('required',True) else 'advisory',
            card['label'], 'Серверная карточка доказательства', ['project'] if domain=='project' else ['registered_source'],
            ids, card['reason'], proved=proved)
    if not qty_bound and (not quantity or quantity['state'] == 'confirmed' or derived):
        add('project:own_quantity', 'project', 'needs_facts', 'blocking',
            'Собственное количество и единица позиции', 'Количественная проверка ПД',
            ['project'], reason='Проверенный расчётный вход не подтверждает собственное количество позиции.')
    thickness = cards.get('thickness', {})
    if thickness.get('state') == 'confirmed' and thickness.get('compared_value') and (
            measure(thickness['compared_value'], thickness['unit']) !=
            measure(thickness.get('declared_value'), thickness['unit'])):
        add('dependency:operation_allocation', 'relationships', 'needs_facts', 'blocking',
            'Распределение общей толщины по технологическим операциям', 'Толщина ПД относится ко всей конструкции',
            ['project', 'workbook'], thickness.get('source_ids', []),
            'Подтверждённая общая толщина не определяет толщину отдельных операций.')

    for action in plan['actions']:
        key = action['key']
        if key.startswith(('claim:', 'card:')):
            continue
        add(key, 'normative' if action['family']=='norm_selection' else 'project', 'needs_facts', 'blocking',
            key, 'Открытое обязательство штатного плана', action.get('verification', {}).get('source_routes', []),
            action.get('source_ids', []), action.get('reason',''))
    if relation.get('children') or relation.get('children_truncated'):
        add('dependency:child_quantities', 'relationships', 'needs_facts', 'blocking',
            'Полнота расчёта связанных материалов и отсутствие двойного учёта',
            'Формальные связи с дочерними строками', ['workbook', 'project', 'validated_child_review'],
            reason='Связь в ВОР установлена. Подтверждение дочерних количеств требует отдельных проверок.')
    for event in result.get('evidence_trace', []):
        if event.get('kind') == WORKBOOK_CONFLICT:
            add('workbook:arithmetic_conflict', 'project', 'possible_mismatch', 'blocking',
                'Арифметика строки ВОР', 'Проверка исходных ячеек', ['workbook'], reason=event['message'], proved=True)
    for note in matching.get('workbook_notes', []):
        if note.get('kind') == 'formula-mismatch':
            add('workbook:formula_mismatch', 'project', 'possible_mismatch', 'blocking',
                'Арифметика строки ВОР', 'Проверка формулы ВОР', ['workbook'],
                reason=note.get('message', 'Количество расходится с формулой ВОР.'), proved=True)
    if result.get('execution_state') != 'complete':
        for domain in ('project','normative','relationships'):
            add('execution:'+domain, domain, 'needs_facts', 'blocking', 'Выполнение проверки', 'Статус исполнения', [])

    directions = {}
    for domain in ('project','normative','relationships'):
        own = [entry for entry in entries if entry['direction']==domain]
        directions[domain] = dict(state=state_for(own), obligations=own)
    overall = state_for(entries)
    legacy = result['state']
    if legacy == 'possible_mismatch' or overall == 'confirmed' and legacy != 'confirmed':
        overall = legacy
    return dict(schema=1, project_state=directions['project']['state'], overall_state=overall,
                legacy_state=legacy, directions=directions)


def report_text(audit):
    """Render directional statuses for the existing result and Word report."""
    labels = {'project': 'Соответствие ПД', 'normative': 'Нормативы', 'relationships': 'Взаимосвязи'}
    states = {'confirmed': 'подтверждено', 'needs_facts': 'нужны доказательства',
              'possible_mismatch': 'возможное расхождение требует проверки',
              'not_applicable': 'неприменимо'}
    parts = []
    for key, label in labels.items():
        result = audit['directions'][key]
        remarks = list(dict.fromkeys(entry['reason'] for entry in result['obligations']
            if entry['state'] in {'needs_facts','possible_mismatch'}
            and entry['reason'] and entry['reason'] != 'Требуется проверяемая связь с источником.'))[:2]
        status = states[result['state']]
        if result['state'] == 'possible_mismatch' and any(
                entry['policy']=='blocking' and entry['mismatch_level']=='proved' for entry in result['obligations']):
            status = 'расхождение подтверждено'
        parts.append(f'{label} — {status}.')
        parts.extend(remarks)
    return ' '.join(parts)
