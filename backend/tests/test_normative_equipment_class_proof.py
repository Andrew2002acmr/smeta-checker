"""Exact real-pilot replay and fail-closed class/function boundaries, offline."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
import normative_equipment_class_proof as proof


@pytest.fixture
def sample(monkeypatch):
    data = json.loads((Path(__file__).parent / 'fixtures/equipment-class-v9-pilot2.json').read_text(encoding='utf-8'))
    data['position'] = VorPosition.model_validate(data['position'])
    data['manufacturer'] = [deepcopy(source) for source in data['check']['sources']
        if source.get('evidence_role') == 'manufacturer_equipment_classification']
    data['kb'] = SimpleNamespace(identity=deepcopy(data['identity']['kb']),
        entity=lambda kind, key: deepcopy(data['norm']) if kind == 'norm' and key == data['norm']['id'] else {})
    def manufacturer(kb, comment, definitions, *, expected_pin):
        from normative_manufacturer_context import _relevant
        claims = _relevant(comment, definitions)
        return dict(sources=deepcopy(data['manufacturer']) if claims else [], context=dict(claim_ids=claims))
    monkeypatch.setattr(proof, 'manufacturer_context', manufacturer)
    data['matching'] = dict(checks=[])
    return data


def run(data, result=None, raw=True):
    return proof.apply_equipment_class_proof(data['position'], result or data['check'], kb=data['kb'],
        expected_pin=data['identity']['kb'], project_versions=set(data['identity']['corpora']),
        matching=data['matching'], raw_items=data['raw_items'] if raw else None)


def owned(result):
    return [item for item in result['items'] if item['topic'] == proof.TOPIC]


def item(data, claim):
    return next(item for item in data['check']['items'] if claim in item['claim_ids'])


def source(data, sid):
    return next(source for source in data['check']['sources'] if source['id'] == sid)


def test_real_position2_derived_class_only_and_no_mutation(sample):
    before = deepcopy(sample['check'])
    result = run(sample)
    assert len(owned(result)) == 1
    derived = owned(result)[0]
    assert derived['claim_ids'] == [proof.CLAIM] and derived['state'] == 'confirmed'
    assert derived['project_source_ids'] == ['agent:p50:0']
    assert derived['normative_source_ids'][:2] == ['n2', 'n2:resources:25']
    assert sample['check'] == before
    assert result['parameter_cards'] == before['parameter_cards']
    assert next(i for i in result['items'] if i['claim_ids'] == ['related:3:rate']) == item(sample, 'related:3:rate')
    event = next(e for e in result['evidence_trace'] if e.get('kind') == proof.PROOF)
    assert event['original_item'] == item(sample, proof.CLAIM)
    assert event['raw_item']['state'] == 'needs_facts'
    assert result['state'] == 'needs_facts'  # Final publication remains the existing verdict's job.


@pytest.mark.parametrize('model', ['HD120', 'HD+130', 'HD130C', 'HD130 / Bomag BW161', 'HD130 массой 13 т'])
def test_exact_model_not_alternative_or_qualifier(sample, model):
    sample['position'].comment = sample['position'].comment.replace('HD130', model)
    assert not owned(run(sample))


@pytest.mark.parametrize('change', ['без уплотнения', 'Уплотнение покрытия', 'Уплотнение основания только',
    'Уплотнение основания производится тяжёлым', 'Уплотнение основания массой 13 т'])
def test_unknown_or_extra_function_not_inferred(sample, change):
    sample['position'].comment = sample['position'].comment.replace('Уплотнение основания', change)
    assert not owned(run(sample))


def test_second_machine_same_function_ambiguous(sample):
    sample['position'].comment += '\nУплотнение основания производится тандемным виброкатком Bomag BW161'
    assert not owned(run(sample))


@pytest.mark.parametrize('claim', ['method', 'normative:applicability'])
@pytest.mark.parametrize('state', ['needs_facts', 'possible_mismatch'])
def test_independent_norm_and_method_must_pass(sample, claim, state):
    item(sample, claim)['state'] = state
    assert not owned(run(sample))


@pytest.mark.parametrize('change', ['no_project', 'other_project', 'no_norm', 'manufacturer_only', 'unknown_norm'])
def test_no_arbitrary_link_inheritance(sample, change):
    target = item(sample, 'method')
    if change == 'no_project':
        target['project_source_ids'] = []
    elif change == 'other_project':
        target['project_source_ids'] = ['agent:sum:ea3f8bc61fa62ef5f0b6f647']
    elif change == 'no_norm':
        target['normative_source_ids'] = []
    elif change == 'manufacturer_only':
        target['normative_source_ids'] = [sample['manufacturer'][0]['id']]
    else:
        target['normative_source_ids'] = ['fabricated']
    assert not owned(run(sample))


@pytest.mark.parametrize('field,value', [('version_id', 'other-version'), ('origin', 'ocr_unreviewed'), ('kind', 'normative')])
def test_project_pinned_and_readable(sample, field, value):
    source(sample, 'agent:p50:0')[field] = value
    assert not owned(run(sample))


@pytest.mark.parametrize('field,value', [('kb_identity', {}), ('text', 'HD 120 tandem roller'),
    ('source_sha256', '0' * 64), ('classification_claim_ids', ['method:comment:equipment:2']),
    ('origin', 'extracted_native_unreviewed'), ('evidence_role', 'normative')])
def test_both_manufacturer_cards_current_and_immutable(sample, field, value):
    source(sample, sample['manufacturer'][0]['id'])[field] = value
    assert not owned(run(sample))


def test_missing_manufacturer_and_wrong_release(sample):
    sample['manufacturer'].pop()
    assert not owned(run(sample))
    sample['kb'].identity = {**sample['kb'].identity, 'sha256': '0' * 64}
    assert not owned(run(sample))


@pytest.mark.parametrize('card_id', ['quantity', 'thickness', 'unit_scale:n2'])
def test_missing_required_numeric_proof(sample, card_id):
    sample['check']['parameter_cards'] = [card for card in sample['check']['parameter_cards'] if card['id'] != card_id]
    assert not owned(run(sample))


@pytest.mark.parametrize('card_id', ['quantity', 'thickness', 'unit_scale:n2'])
def test_core_numeric_proof_cannot_be_scoped_away(sample, card_id):
    card = next(card for card in sample['check']['parameter_cards'] if card['id'] == card_id)
    card.update(required=False, state='needs_facts')
    assert not owned(run(sample))


@pytest.mark.parametrize('change', ['second_allowed_pd', 'quantity_other_pd', 'thickness_other_source', 'unit_other_norm', 'duplicate_card'])
def test_core_numeric_proofs_belong_to_same_scope(sample, change):
    sample['identity']['corpora']['other-pd'] = {}
    cards = {card['id']: card for card in sample['check']['parameter_cards']}
    if change == 'second_allowed_pd':
        source(sample, 'agent:p50:0')['version_id'] = 'other-pd'
    elif change == 'quantity_other_pd':
        source(sample, cards['quantity']['source_ids'][0])['version_id'] = 'other-pd'
    elif change == 'thickness_other_source':
        other = {**source(sample, 'agent:p50:0'), 'id': 'other-thickness'}
        sample['check']['sources'].append(other)
        cards['thickness']['source_ids'] = [other['id']]
    elif change == 'unit_other_norm':
        cards['unit_scale:n2']['source_ids'] = ['n8']
    else:
        sample['check']['parameter_cards'].append(deepcopy(cards['thickness']))
    assert not owned(run(sample))


@pytest.mark.parametrize('change', ['no_compaction', 'altered_norm', 'wrong_norm_pdf', 'no_resource',
    'zero_resource', 'negative_resource', 'project_resource', 'manual_resource', 'wrong_resource_page',
    'resource_ocr', 'rule', 'unknown_check'])
def test_literal_applicable_norm_function_and_positive_resource(sample, change):
    norm = sample['norm']
    if change == 'no_compaction':
        source(sample, 'n2')['text'] = source(sample, 'n2')['text'].replace('уплотнение', 'разрыхление')
    elif change == 'altered_norm':
        norm['text'] += 'changed'
        source(sample, 'n2')['text'] += 'different'
    elif change == 'wrong_norm_pdf':
        norm['source_id'] = 'different-source'
    elif change in {'rule', 'unknown_check'}:
        event = next(e for e in sample['check']['evidence_trace']
            if e.get('kind') == 'normative_resource_consumption_candidates' and e['norm_source_id'] == 'n2')
        event['rules' if change == 'rule' else 'checks'] = [dict(state='unknown')]
    else:
        resources = [resource for resource in norm['resources'] if resource['name'].startswith('Катки')]
        if change == 'no_resource':
            norm['resources'] = []
        for resource in resources:
            if change == 'zero_resource':
                resource['quantity'] = '0'
            elif change == 'negative_resource':
                resource['quantity'] = '-1'
            elif change == 'project_resource':
                resource['quantity_kind'] = 'project'
            elif change == 'manual_resource':
                resource['name'] = 'Катки ручные'
            elif change == 'wrong_resource_page':
                resource['citation']['physical_page'] = 26
            elif change == 'resource_ocr':
                resource['status'] = 'ocr_unreviewed'
    assert not owned(run(sample))


@pytest.mark.parametrize('kind', ['target_red', 'mixed_target', 'raw_red', 'another_red', 'unknown_constraint',
    'matching_conflict', 'numeric_gap', 'technical_failure', 'missing_raw'])
def test_conflicts_and_other_unknowns_never_waived(sample, kind):
    if kind == 'target_red':
        item(sample, proof.CLAIM)['state'] = 'possible_mismatch'
    elif kind == 'mixed_target':
        item(sample, proof.CLAIM)['claim_ids'].append('attribute:comment:power')
    elif kind == 'raw_red':
        next(i for i in sample['raw_items'] if proof.CLAIM in i['claim_ids'])['state'] = 'possible_mismatch'
    elif kind in {'another_red', 'unknown_constraint'}:
        sample['check']['items'].append(dict(topic='Масса катка', claim_ids=['attribute:comment:mass'],
            required=True, state='possible_mismatch' if kind == 'another_red' else 'needs_facts',
            project_source_ids=[], normative_source_ids=[], explanation='Масса не доказана.'))
    elif kind == 'matching_conflict':
        sample['matching']['checks'] = [dict(claim_id=proof.CLAIM, state='source_conflict')]
    elif kind == 'numeric_gap':
        sample['check']['parameter_cards'][0]['state'] = 'needs_facts'
    elif kind == 'technical_failure':
        sample['check']['execution_state'] = 'technical_failure'
    else:
        sample['raw_items'] = []
    assert not owned(run(sample))


def test_replay_revalidates_proof_and_restores_original_on_changed_source(sample):
    first = run(sample)
    second = run(sample, first, raw=False)
    assert second == first
    # Losing either literal restores the original uncertain item; it cannot use its own old green.
    sample['manufacturer'] = []
    third = run(sample, second, raw=False)
    assert not owned(third)
    assert next(i for i in third['items'] if i['claim_ids'] == [proof.CLAIM]) == item(sample, proof.CLAIM)


@pytest.mark.parametrize('part', ['item', 'original', 'raw', 'position'])
def test_modified_saved_proof_cannot_remain_confirmed(sample, part):
    first = run(sample)
    event = next(e for e in first['evidence_trace'] if e.get('kind') == proof.PROOF)
    if part == 'item':
        owned(first)[0]['explanation'] += 'changed'
    elif part == 'original':
        event['original_item']['state'] = 'confirmed'
    elif part == 'raw':
        event['raw_item']['state'] = 'possible_mismatch'
    else:
        event['position_key'] = 'other'
    assert not owned(run(sample, first, raw=False))
