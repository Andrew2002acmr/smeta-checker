"""Confirm only literal quantity/unit claims, never a work method or applicability."""
import hashlib
import json
import re
from copy import deepcopy
from typing import Any

TOPIC = 'Количество и единица по проверенным источникам'
OMISSION = 'numeric_claim_source_omission_v1'
PROOF = 'quantity_unit_bridge_v1'
SUPERSEDED = 'numeric_claim_superseded_v1'
VERSION = 1
NUMERIC_CLAIMS = {'quantity', 'unit'}


def _fingerprint(item, sources):
    # The ordinary UI pass expands s0 handles in prose. Hash that same readable
    # form so a saved marker survives formatting, but not a changed statement.
    from normative_verdict import readable_review
    normalized = readable_review(dict(sources=sources, items=[item]))['items'][0]
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _readable(source):
    origin = source.get('origin', '')
    return bool(origin and origin != 'uncertain_table' and not origin.startswith('ocr_unreviewed')
        and (not origin.startswith('ocr') or source.get('numeric_verified')))


def _own_item(item, result):
    return bool(item.get('topic') == TOPIC and any(e.get('kind') == PROOF
        and e.get('item_fingerprint') == _fingerprint(item, result['sources'])
        for e in result.get('evidence_trace', [])))


def remove_previous(result):
    """Only this server's recorded synthetic item may be replaced on replay."""
    result['items'] = [i for i in result.get('items', []) if not _own_item(i, result)]
    result['evidence_trace'] = [e for e in result.get('evidence_trace', []) if e.get('kind') != PROOF]


def append_quantity_bridge(position, result, positions, relation):
    item = quantity_unit_item(position, result, positions, relation)
    if item:
        proof_fingerprint = _fingerprint(item, result['sources'])
        replaced = set()
        kept, archives = [], []
        for previous in result['items']:
            fingerprint = _fingerprint(previous, result['sources'])
            markers = [e for e in result.get('evidence_trace', []) if e.get('kind') == OMISSION
                and e.get('item_fingerprint') == fingerprint and e.get('original_state') == 'confirmed'
                and e.get('reason') == 'missing_project_source_ids']
            if (previous.get('state') == 'needs_facts' and previous.get('claim_ids')
                    and set(previous['claim_ids']) <= set(item['claim_ids'])
                    and not previous.get('project_source_ids') and markers):
                archives.append(dict(kind=SUPERSEDED, original_item=deepcopy(previous),
                    original_fingerprint=fingerprint, omission_events=deepcopy(markers),
                    replacement_fingerprint=proof_fingerprint, replacement_topic=TOPIC))
                replaced.add(fingerprint)
            else:
                kept.append(previous)
        result['items'] = kept
        # Consume the marker: an identical later uncertain statement must not
        # inherit authority from a historical omission. The full original and
        # its validation event remain archived, never restored as live claims.
        result['evidence_trace'] = [e for e in result.get('evidence_trace', [])
            if not (e.get('kind') == OMISSION and e.get('item_fingerprint') in replaced)] + archives
        result['items'].append(item)
        result['evidence_trace'].append(dict(kind=PROOF, version=VERSION, item_fingerprint=proof_fingerprint))
        if (replaced and all(i.get('state') == 'confirmed' or i.get('required') is False for i in result['items'])
                and all(c.get('state') == 'confirmed' or c.get('required') is False
                    for c in result.get('parameter_cards', []))):
            result['summary'] = ' '.join(i.get('explanation', '') for i in result['items'])[:2500]


def record_source_omission(original, validated, sources, trace):
    """Record a server validation downgrade, not a model's own uncertainty."""
    from normative_verdict import absence_claim
    if trace is None:
        return
    claims = set(original.get('claim_ids') or [])
    excluded = {'state', 'explanation', 'required'}
    available = {s['id']: s for s in sources}
    if (original.get('state') != 'confirmed' or validated.get('state') != 'needs_facts'
            or absence_claim({**original, 'state': 'possible_mismatch'})
            or not claims or not claims <= NUMERIC_CLAIMS or original.get('project_source_ids')
            or {k: v for k, v in original.items() if k not in excluded}
                != {k: v for k, v in validated.items() if k not in excluded}
            or any(sid not in available or available[sid]['kind'] != 'normative'
                or not _readable(available[sid]) for sid in validated.get('normative_source_ids', []))):
        return
    trace.append(dict(kind=OMISSION, original_state='confirmed', reason='missing_project_source_ids',
        item_fingerprint=_fingerprint(validated, sources)))


def _claim_scope_clear(result):
    from normative_verdict import source_supported
    recorded = {e.get('item_fingerprint') for e in result.get('evidence_trace', [])
        if e.get('kind') == OMISSION and e.get('original_state') == 'confirmed'
        and e.get('reason') == 'missing_project_source_ids'}
    for item in result.get('items', []):
        claims = set(item.get('claim_ids') or [])
        if not claims & NUMERIC_CLAIMS:
            continue
        if item.get('state') == 'confirmed' and source_supported(item):
            continue
        if (item.get('state') != 'needs_facts' or not claims <= NUMERIC_CLAIMS
                or item.get('project_source_ids')
                or _fingerprint(item, result['sources']) not in recorded):
            return False
    return True


def _project_dependencies(proposal, sources, work_scope):
    from normative_parameters import construction_conflict, measure, number
    source = sources[proposal['source_id']]
    operands = proposal.get('operand_source_ids') or []
    if proposal.get('operation', 'direct') == 'direct':
        return source['origin'] != 'calculated_from_cells' and not operands
    if (proposal.get('operation') != 'sum' or source['origin'] != 'calculated_from_cells'
            or not source.get('numeric_verified') or not source.get('unit_verified')
            or measure('1', source.get('unit', '')) != measure('1', proposal['unit'])
            or not 2 <= len(operands) <= 12 or len(set(operands)) != len(operands)):
        return False
    cells = [sources.get(sid) for sid in operands]
    if any(s is None or s['kind'] != 'project' or ':cell:' not in s['id'] or not _readable(s)
            or not s.get('unit_verified') or measure('1', s.get('unit', '')) != measure('1', proposal['unit'])
            or construction_conflict(s['text'], work_scope) for s in cells):
        return False
    cells = [s for s in cells if s is not None]
    if len({(s.get('version_id'), s.get('page')) for s in [source, *cells]}) != 1:
        return False
    physical = [json.dumps(s['rects'], sort_keys=True) for s in cells if s.get('rects')]
    if len(physical) != len(set(physical)):
        return False
    values = [number(s['text'].rsplit('\n', 1)[-1]) for s in cells]
    return bool(all(v is not None for v in values)
        and sum(v for v in values if v is not None) == number(proposal['value']))


def _literal_quantity(card, result, position):
    from normative_parameters import clean, measure, quoted, unit_quoted
    sources = {s['id']: s for s in result['sources']}
    ids = set(card.get('source_ids') or [])
    anchors = card.get('source_anchors') or []
    if not ids or len(ids) > 8 or {a.get('source_id') for a in anchors} != ids:
        return False
    for anchor in anchors:
        source = sources.get(anchor.get('source_id'))
        if (source is None or source['kind'] != 'project' or not _readable(source)
                or not source.get('version_id') or not source.get('page')
                or not quoted(card.get('found_value'), anchor.get('quote', ''), source['text'])
                or any(rect not in source.get('rects', []) for rect in anchor.get('rects', []))):
            return False
        if ':cell:' in source['id'] and (not source.get('unit_verified')
                or measure('1', source.get('unit', '')) != measure('1', card.get('found_unit', ''))):
            return False
        candidates = [p for p in result.get('parameter_proposals', [])
            if p.get('parameter') == 'quantity' and p.get('source_id') == source['id']
            and p.get('scope_state') == 'verified'
            and measure(p.get('value'), p.get('unit', '')) == measure(card.get('found_value'), card.get('found_unit', ''))
            and clean(p.get('quote', '')) == clean(anchor.get('quote', ''))
            and quoted(p['value'], p['quote'], p.get('scope_quote', ''))
            and clean(p.get('scope_quote', '')) in clean(source['text'])
            and unit_quoted(p['unit'], p['quote'])]
        if not candidates or not all(_project_dependencies(p, sources, position.work_name) for p in candidates):
            return False
    return True


def _norm_identity(source):
    codes = set(re.findall(r'\b\d{2}-\d{2}-\d{3}-\d{2,3}\b', source.get('label', '')))
    return (source['source_id'], next(iter(codes))) if source.get('source_id') and len(codes) == 1 else None


def _selected_scale(cards, result):
    from normative_parameters import measure, unit_scale_quoted
    from normative_verdict import source_supported
    sources = {s['id']: s for s in result['sources']}
    selected = {sid for item in result.get('items', []) if item.get('state') == 'confirmed'
        and 'normative:applicability' in item.get('claim_ids', []) and source_supported(item)
        and all(ref in sources and sources[ref]['kind'] == 'project'
            for ref in item.get('project_source_ids', []))
        for sid in item.get('normative_source_ids', [])}
    identities = {_norm_identity(sources[sid]) for sid in selected if sid in sources
        and sources[sid]['kind'] == 'normative' and _readable(sources[sid])}
    identities.discard(None)
    accepted = []
    for card in cards:
        if card.get('role') != 'unit_scale' or card.get('state') != 'confirmed':
            continue
        ids = set(card.get('source_ids') or [])
        anchors = card.get('source_anchors') or []
        if not ids or {a.get('source_id') for a in anchors} != ids:
            continue
        if all((source := sources.get(a.get('source_id'))) is not None
                and source['kind'] == 'normative' and _readable(source)
                and _norm_identity(source) in identities
                and unit_scale_quoted(card.get('found_value'), card.get('found_unit', ''), a.get('quote', ''), source['text'])
                for a in anchors):
            accepted.append(card)
    # A selected norm with conflicting measures cannot establish its scale.
    if not accepted or len({measure(c['found_value'], c['found_unit']) for c in accepted}) != 1:
        return []
    return list(dict.fromkeys(sid for c in accepted for sid in c['source_ids']))


def quantity_unit_item(position, result, positions, relation) -> dict[str, Any] | None:
    """Recompute from literal proposals; saved cards and shortcut states are not proof."""
    from normative_parameters import build_cards
    if (result.get('pipeline_version', 0) < 3 or result.get('execution_state') == 'technical_failure'
            or relation.get('parent') or str(getattr(position, 'position_type', '')).casefold() == 'материал'):
        return None
    literal = {**result, 'local_work_basis': None, 'quantity_composition': None,
        'items': [i for i in result.get('items', []) if not _own_item(i, result)]}
    if (len({s['id'] for s in result['sources']}) != len(result['sources'])
            or not _claim_scope_clear(literal)):
        return None
    cards = build_cards(position, literal, positions, relation)
    card = next((c for c in cards if c['id'] == 'quantity' and c['state'] == 'confirmed'), None)
    if not card or not _literal_quantity(card, literal, position):
        return None
    norm_ids = _selected_scale(cards, literal)
    if len(norm_ids) > 8:
        norm_ids = []
    claims = ['quantity', 'unit'] if norm_ids else ['quantity']
    return dict(topic=TOPIC, state='confirmed', required=True, claim_ids=claims,
        project_source_ids=card['source_ids'], normative_source_ids=norm_ids,
        explanation=f"Количество {card['found_value']} {card['found_unit']} подтверждено буквальными источниками ПД; "
            'значение ВОР сопоставлено с учётом единиц и точности записи.'
            + (' Измеритель выбранной нормы проверен по нормативному источнику.' if norm_ids else ''))
