"""Prove one literal HAMM compaction class after independent work validation.

This is neither machine fitness approval nor a normative applicability rule.
The model's original item is archived and every dependency is checked on replay.
"""
from copy import deepcopy
import hashlib
import json
import re

from audit_models import VorPosition
from audit_predicates import technical_specs
from human_basis import position_key
from normative_manufacturer_context import manufacturer_context
from normative_source_roles import classification_source
from normative_thickness_rules import source_definition
from normative_verdict import source_supported

PROOF = 'equipment_class_function_proof_v1'
TOPIC = 'Класс катка для подтверждённого способа работ'
EXPECTED = 'Уплотнение основания производится тандемным виброкатком'
CLAIM = 'method:comment:equipment:1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _plain(value):
    return re.sub(r'\s+', ' ', str(value)).strip().rstrip('.;').casefold()


def _raw_items(result, supplied, previous):
    if supplied is not None:
        return [item.model_dump() if hasattr(item, 'model_dump') else item for item in supplied]
    if previous and previous.get('raw_item', {}).get('state') == 'needs_facts':
        return [previous['raw_item']]
    responses = (result.get('telemetry') or {}).get('responses') or []
    if responses:
        try:
            answer = json.loads(responses[-1]['text'])
            return answer['items'] if isinstance(answer.get('items'), list) else []
        except (KeyError, TypeError, ValueError):
            pass
    return []


def _restore(result, key):
    previous = None
    owned = [item for item in result.get('items', []) if item.get('topic') == TOPIC]
    if owned:
        events = [event for event in result.get('evidence_trace', []) if event.get('kind') == PROOF]
        if len(owned) == len(events) == 1:
            event = events[0]
            if (event.get('position_key') == key and event.get('item_sha256') == _digest(owned[0])
                    and event.get('original_sha256') == _digest(event.get('original_item'))
                    and event.get('raw_sha256') == _digest(event.get('raw_item'))
                    and event.get('original_item', {}).get('claim_ids') == [CLAIM]
                    and event.get('original_item', {}).get('state') == 'needs_facts'):
                previous = event
        restored = deepcopy(previous['original_item']) if previous else dict(
            topic='Проверка класса оборудования', claim_ids=[CLAIM], state='needs_facts', required=True,
            project_source_ids=[], normative_source_ids=[],
            explanation='Сохранённое доказательство класса не прошло повторную проверку.')
        result['items'] = [restored if item.get('topic') == TOPIC else item for item in result['items']]
        result['summary'] = previous.get('original_summary', '') if previous else restored['explanation']
    result['evidence_trace'] = [event for event in result.get('evidence_trace', []) if event.get('kind') != PROOF]
    return previous, bool(owned and previous is None)


def _work_basis(result, lookup, versions):
    application = [item for item in result['items'] if 'normative:applicability' in item.get('claim_ids', [])]
    method = [item for item in result['items'] if 'method' in item.get('claim_ids', [])]
    if not application or not method:
        return None
    items = application + method
    if any(item.get('state') != 'confirmed' or not source_supported(item) for item in items):
        return None
    projects = set.intersection(*(set(item['project_source_ids']) for item in items))
    norms = set.intersection(*(set(item['normative_source_ids']) for item in items))
    projects = sorted(sid for sid in projects if sid in lookup and lookup[sid].get('kind') == 'project'
        and lookup[sid].get('version_id') in versions and lookup[sid].get('page')
        and lookup[sid].get('origin') in {'native', 'human'} and lookup[sid].get('text'))
    bases = [(lookup[sid], source_definition(lookup[sid])) for sid in norms if sid in lookup
        and not classification_source(lookup[sid])]
    bases = [(source, definition) for source, definition in bases if definition
        and 'Состав работ:' in source.get('text', '')
        and re.search(r'\bуплотнение\b', source['text'].split('Состав работ:', 1)[1], re.I)]
    return (projects, bases[0][0], bases[0][1]) if projects and len(bases) == 1 else None


def _resource(kb, result, base, definition):
    events = [event for event in result.get('evidence_trace', [])
        if event.get('kind') == 'normative_resource_consumption_candidates'
        and event.get('norm_source_id') == base['id'] and event.get('norm_code') == definition['code']]
    from normative_resource_recovery import same_physical_resource_event, same_physical_resource_source
    event = same_physical_resource_event(events)
    if event is None:
        return None
    norm = kb.entity('norm', event['norm_id'])
    if (norm.get('source_id') != base['source_id'] or norm.get('code') != definition['code']
            or norm.get('status') != base.get('origin')
            or not norm.get('text') or not norm['text'].startswith(base['text'])
            or norm.get('status') in {'native_definition_ambiguous', 'uncertain_table'}
            or str(norm.get('status', '')).startswith('ocr')
            or any(check.get('state') in {'mismatch', 'unknown'} for check in event.get('checks', []))
            or event.get('rules')):
        return None
    from normative_corpus import decimal
    from normative_resource_consumption import resource_quote
    for resource in norm.get('resources', []):
        citation = resource.get('citation') or {}
        quantity = decimal(resource.get('quantity'))
        if (not re.fullmatch(r'Катки самоходные гладкие вибрационные, масса \d+(?:[.,]\d+)? т',
                            resource.get('name', ''), re.I)
                or resource.get('quantity_kind') != 'numeric' or quantity is None or quantity <= 0
                or resource.get('unit') != 'маш.-ч' or str(resource.get('status', '')).startswith('ocr')
                or citation.get('source_id') != base['source_id'] or not citation.get('physical_page')):
            continue
        quote = resource_quote(dict(code=resource.get('resource_code'), name=resource['name'],
            unit=resource['unit'], quantity=resource['quantity'], raw_quantity=resource.get('raw_quantity')))
        sources = [source for source in result['sources'] if source.get('kind') == 'normative'
            and source.get('source_id') == base['source_id'] and source.get('page') == citation['physical_page']
            and source.get('origin') == norm['status']
            and source.get('text', '').startswith('Ресурсы нормы ' + norm['code'] + '; Измеритель: ')
            and quote in source.get('text', '').splitlines()]
        physical = same_physical_resource_source(sources)
        if physical is not None:
            return dict(source_id=physical['id'], quote=quote, norm_id=norm['id'],
                norm_code=norm['code'], resource_id=resource['id'])
    return None


def _numeric_scope(cards, projects, base_id, lookup):
    core = {}
    for key in ('quantity', 'thickness', 'unit_scale:' + base_id):
        matches = [card for card in cards if card.get('id') == key]
        if len(matches) != 1 or matches[0].get('state') != 'confirmed' or not matches[0].get('source_ids'):
            return []
        core[key] = matches[0]
    if core['unit_scale:' + base_id]['source_ids'] != [base_id]:
        return []
    shared = sorted(set(projects) & set(core['thickness']['source_ids']))
    versions = {lookup[sid].get('version_id') for sid in shared}
    if not shared or len(versions) != 1 or None in versions:
        return []
    for card in (core['quantity'], core['thickness']):
        if any(sid not in lookup or lookup[sid].get('kind') != 'project'
               or lookup[sid].get('version_id') not in versions for sid in card['source_ids']):
            return []
    return shared


def apply_equipment_class_proof(position, result, *, kb, expected_pin, project_versions,
                                matching, raw_items=None):
    """Return a detached review; never waive another claim or approve equipment fitness."""
    output = deepcopy(result)
    if not isinstance(position, VorPosition):
        return output
    key = position_key(position)
    previous, broken = _restore(output, key)
    if (broken or output.get('execution_state') != 'complete' or output.get('pipeline_version', 0) < 3
            or position.position_type.casefold() != 'работа' or kb.identity != expected_pin
            or not project_versions or not re.search(r'основани', position.work_name, re.I)
            or not re.search(r'уплотнен', position.work_name, re.I)):
        return output
    definitions = [spec.model_dump() for spec in technical_specs(position.work_name, position.comment,
        position.position_type, [], position.formula)]
    targets = [spec for spec in definitions if spec['claim_id'] == CLAIM]
    if len(targets) != 1 or _plain(targets[0]['expected_text']) != _plain(EXPECTED):
        return output
    items = [item for item in output['items'] if CLAIM in item.get('claim_ids', [])]
    if len(items) != 1 or items[0].get('claim_ids') != [CLAIM] or items[0].get('state') != 'needs_facts':
        return output
    if any(item.get('state') == 'possible_mismatch' or (item.get('required', True)
            and item is not items[0] and item.get('state') != 'confirmed') for item in output['items']):
        return output
    if any(check.get('state') in {'contradiction', 'source_conflict', 'possible_mismatch'}
           for check in (matching.get('checks') or [])):
        return output
    cards = output.get('parameter_cards') or []
    if not cards or any(card.get('required', True) and card.get('state') != 'confirmed' for card in cards):
        return output
    raw = _raw_items(result, raw_items, previous)
    raw_targets = [item for item in raw if CLAIM in item.get('claim_ids', [])]
    if (len(raw_targets) != 1 or raw_targets[0].get('claim_ids') != [CLAIM]
            or raw_targets[0].get('state') != 'needs_facts'
            or any(item.get('state') == 'possible_mismatch' for item in raw)):
        return output
    lookup = {source['id']: source for source in output['sources']}
    if len(lookup) != len(output['sources']):
        return output
    basis = _work_basis(output, lookup, set(project_versions))
    if not basis:
        return output
    projects, base, definition = basis
    projects = _numeric_scope(cards, projects, base['id'], lookup)
    if not projects:
        return output
    try:
        manufacturer = manufacturer_context(kb, position.comment, definitions, expected_pin=expected_pin)
        proof_sources = manufacturer['sources']
        if (len(proof_sources) != 2 or manufacturer['context'].get('claim_ids') != [CLAIM]
                or any(lookup.get(source['id']) != source for source in proof_sources)):
            return output
        resource = _resource(kb, output, base, definition)
    except (KeyError, TypeError, ValueError, OSError):
        return output
    if not resource or kb.identity != expected_pin:
        return output
    item = dict(topic=TOPIC, claim_ids=[CLAIM], state='confirmed', required=True,
        project_source_ids=projects[:8],
        normative_source_ids=[base['id'], resource['source_id'], *[source['id'] for source in proof_sources]],
        explanation='Требование «' + EXPECTED + '» подтверждено в пределах класса и функции машины. '
            'Для этой позиции способ работ и применимость нормы уже подтверждены по той же конструкции ПД; '
            'состав нормы включает уплотнение, ресурс — вибрационный каток. Два проверенных источника '
            'производителя совместно подтверждают класс HAMM HD 130 как тандемного виброкатка. '
            'Это не подтверждение массы, пригодности конкретного экземпляра, замены остальных машин '
            'или расхода материала; эти свойства данным выводом не устанавливаются.')
    original = items[0]
    output['items'] = [item if value is original else value for value in output['items']]
    output['evidence_trace'].append(dict(kind=PROOF, position_key=key, kb_identity=deepcopy(expected_pin),
        original_summary=output.get('summary', ''),
        original_item=deepcopy(original), original_sha256=_digest(original),
        raw_item=deepcopy(raw_targets[0]), raw_sha256=_digest(raw_targets[0]),
        item_sha256=_digest(item), source_sha256={sid: _digest(lookup[sid]) for sid in
            [*projects, base['id'], resource['source_id'], *[source['id'] for source in proof_sources]]},
        resource=resource, scope='exact_model_class_and_norm_compaction_function_only'))
    output['summary'] = ' '.join(value['explanation'] for value in output['items']
                                if value.get('required', True))[:2500]
    return output
