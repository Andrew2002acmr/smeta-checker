"""Factory classification is not a norm, even after legacy source compaction."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from normative_manufacturer_context import SOURCES
from normative_parameters import build_cards
from normative_position_check import AgentAnswer, CheckedItem, answer_feedback, validate_items
from normative_source_roles import CLASSIFICATION_ROLE, classification_source
from normative_public_sources import public_sources
from workspace_models import NormativeCheckSource, NormativeParameterProposal


CLAIM = 'method:comment:equipment:1'


def sources():
    raw = json.loads((Path(__file__).parent / 'fixtures/manufacturer-hd130-reviewed.json').read_text(encoding='utf8'))
    result = [dict(id='p', kind='project', label='PD', page=1, origin='native', text='Project work.')]
    for expected, metadata in zip(SOURCES, raw):
        result.append(dict(id='manufacturer:' + expected['clause_id'], kind='normative',
            label=expected['designation'], page=expected['page'], origin='reviewed_literal_transcription',
            text=metadata['reviewed_clauses'][0]['text'], source_id=expected['source_id'],
            evidence_role=CLASSIFICATION_ROLE, classification_claim_ids=[CLAIM],
            source_sha256=expected['sha256'], text_sha256=expected['text_sha256'],
            kb_identity={'profile': 'roads', 'release_id': 'test', 'sha256': 'a' * 64}))
    return result


def item(claim='normative:applicability', state='confirmed', refs=None):
    return CheckedItem(topic='Source role', state=state, claim_ids=[claim],
        project_source_ids=['p'], normative_source_ids=refs or [s['id'] for s in sources()[1:]],
        explanation='A controlled model assertion to test the evidence gate.')


@pytest.mark.parametrize('claim', ['normative:applicability', 'unit', 'basis:consumption',
    'derived:rate', 'attribute:comment:condition', 'method', 'method:comment:power:1'])
def test_real_manufacturer_literals_cannot_alone_prove_other_normative_claims(claim):
    current = item(claim)
    assert validate_items([current], sources(), {claim})[0]['state'] == 'needs_facts'
    issues = answer_feedback(AgentAnswer(summary='', items=[current]), sources(),
        SimpleNamespace(parameter_evidence={}))
    assert any('производителя' in issue for issue in issues)


def test_paired_unchanged_sources_support_only_the_exact_permitted_class_claim():
    current = item(CLAIM)
    assert validate_items([current], sources(), {CLAIM})[0]['state'] == 'confirmed'
    assert answer_feedback(AgentAnswer(summary='', items=[current]), sources(),
        SimpleNamespace(parameter_evidence={})) == []
    assert validate_items([item('presence')], sources(), {'presence'})[0]['state'] == 'confirmed'


@pytest.mark.parametrize('change', ['one_source', 'no_permission', 'wrong_permission', 'changed_text',
    'wrong_sha', 'different_pin', 'dto_strip', 'generic_retrieval', 'label_only'])
def test_incomplete_or_stripped_class_proof_fails_closed_without_becoming_a_norm(change):
    ss = sources()
    current = item(CLAIM)
    if change == 'one_source':
        current.normative_source_ids = [ss[1]['id']]
    elif change in {'no_permission', 'wrong_permission'}:
        ss[1]['classification_claim_ids'] = [] if change == 'no_permission' else ['method:comment:equipment:2']
    elif change == 'changed_text':
        ss[1]['text'] += ' altered'
    elif change == 'wrong_sha':
        ss[1]['source_sha256'] = 'b' * 64
    elif change == 'different_pin':
        ss[1]['kb_identity'] = {'release_id': 'another'}
    elif change == 'dto_strip':
        ss = public_sources({'position_check': {'sources': ss}})['position_check']['sources']
        ss = [NormativeCheckSource.model_validate(s).model_dump() for s in ss]
    elif change in {'generic_retrieval', 'label_only'}:
        for n, s in enumerate(ss[1:]):
            s.pop('evidence_role')
            s['id'] = 'agent:n:' + str(n)
            if change == 'label_only':
                s.pop('source_id')
        current.normative_source_ids = [s['id'] for s in ss[1:]]
    assert all(classification_source(s) for s in ss[1:])
    assert validate_items([current], ss, {CLAIM})[0]['state'] == 'needs_facts'
    norm_item = item(refs=current.normative_source_ids)
    assert validate_items([norm_item], ss, {'normative:applicability'})[0]['state'] == 'needs_facts'


@pytest.mark.parametrize('state', ['confirmed', 'possible_mismatch'])
def test_actual_norm_plus_manufacturer_retains_existing_positive_and_conflict_gates(state):
    ss = sources()
    ss.append(dict(id='norm', kind='normative', label='ГЭСН 27-04-003-06', page=17,
        origin='native', text='Selected work norm.'))
    current = item(state=state, refs=[ss[1]['id'], ss[2]['id'], 'norm'])
    assert validate_items([current], ss, {'normative:applicability'})[0]['state'] == state
    current.project_source_ids = []
    assert validate_items([current], ss, {'normative:applicability'})[0]['state'] == 'needs_facts'


@pytest.mark.parametrize('parameter', ['unit_scale', 'coefficient'])
def test_manufacturer_numbers_never_become_norm_scales_or_calculation_coefficients(parameter):
    ss = sources()
    source = ss[1]
    source['text'] += '\nИзмеритель: 100 м2\nКоэффициент уплотнения 1.26 при толщине равной 20 см.'
    proposal = NormativeParameterProposal(parameter=parameter, source_id=source['id'],
        value='100' if parameter == 'unit_scale' else '1.26', unit='м2' if parameter == 'unit_scale' else '',
        quote='Измеритель: 100 м2' if parameter == 'unit_scale' else 'Коэффициент уплотнения 1.26',
        scope_state='verified', scope_quote=source['text'])
    tools = SimpleNamespace(parameter_evidence={parameter: proposal.model_dump()})
    feedback = answer_feedback(AgentAnswer(summary='', items=[], parameters=[proposal]), ss, tools, owner_unit='м2')
    assert any('производителя' in issue for issue in feedback)
    assert parameter not in tools.parameter_evidence
    p = VorPosition(index=0, work_name='Устройство основания h=20 см', volume='126', unit='м2',
        formula='100*1.26', comment='Коэффициент уплотнения 1.26')
    result = dict(pipeline_version=4, sources=ss, items=[], parameter_proposals=[proposal.model_dump()])
    cards = build_cards(p, result, {0:p}, {})
    target_id = 'unit_scale:' + source['id'] if parameter == 'unit_scale' else 'factor:0:1.26'
    target = next(c for c in cards if c['id'] == target_id)
    assert target['state'] == 'needs_facts' and 'производителя' in target['reason']


def test_source_roles_survive_backend_saved_json_but_public_dto_is_conservative():
    ss = sources()
    saved = json.loads(json.dumps(deepcopy(ss)))
    assert saved == ss
    assert validate_items([item(CLAIM)], saved, {CLAIM})[0]['state'] == 'confirmed'
    view = public_sources({'position_check': {'sources': ss}})['position_check']['sources']
    public = [NormativeCheckSource.model_validate(s).model_dump() for s in view]
    assert all('evidence_role' not in s for s in public)
    assert validate_items([item(CLAIM)], public, {CLAIM})[0]['state'] == 'needs_facts'
