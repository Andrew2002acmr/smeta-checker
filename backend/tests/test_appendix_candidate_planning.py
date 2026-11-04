"""Planning boundaries only: a synthetic conditional candidate certifies nothing."""
from copy import deepcopy

import pytest

from analysis_replay import digest
from audit_models import VorPosition
from normative_evidence_plan import build_work_plan


def data():
    position = VorPosition(index=57, work_name='Смесь для верхнего слоя', volume='2.4', unit='т',
        position_type='Материал', comment='Расход на 1000м2 (расчёт) 6+(2*3)=12т',
        calculation={'expression': '=200/1000*12'}).model_dump(mode='json')
    sources = [dict(id='table', kind='normative', source_id='test-norm-pdf', page=3,
        origin='native', label='Synthetic table', text='Synthetic source for planning tests'),
        dict(id='project', kind='project', version_id='test-pd', page=5,
            origin='native', label='Synthetic project scope', text='Unrelated verified work area')]
    review = dict(sources=sources, items=[
        dict(state='needs_facts', required=True, claim_ids=['derived:rate'],
            normative_source_ids=['table'], project_source_ids=['project'], explanation='Conditions open'),
        dict(state='confirmed', required=True, claim_ids=['quantity'],
            normative_source_ids=[], project_source_ids=['project'], explanation='Independent work area')],
        parameter_cards=[dict(id='factor:57:12', state='needs_facts', role='coefficient',
            source_ids=[], reason='Derived rate is not a literal source number'),
            dict(id='quantity', state='confirmed', source_ids=['project'], role='project_value')],
        parameter_proposals=[], evidence_trace=[], coverage={})
    candidate = dict(kind='appendix_derived_rate_candidate_v1', rate='12.00', unit='т/1000м2',
        expression='6+(2*3)', root_source_ids=[s['id'] for s in sources],
        source_sha256={s['id']: digest(s) for s in sources},
        proposals_sha256=digest([]), conditional_arithmetic_verified=True,
        selected_applicability_sha256=digest([]),
        normative_basis_verified=False, dependent_output_verified=False,
        pending_condition_ids=['edition_chain', 'project_consumption_priority', 'edge_prescription'],
        pending_conditions=['Synthetic conditions only; not a normative premise'])
    review['evidence_trace'] = [dict(kind='workbook_area_rate_v1', appendix_rate_candidate=candidate)]
    return position, review, candidate


def test_rate_plan_checks_remaining_conditions_without_reopening_independent_area():
    position, review, _ = data()
    original = deepcopy(review)
    plan = build_work_plan(position, review, {})
    rate = next(a for a in plan['actions'] if a['key'] == 'claim:derived:rate')
    factor = next(a for a in plan['actions'] if a['key'] == 'card:factor:57:12')
    assert rate['action'] == factor['action'] == 'verify_applicability_conditions'
    assert rate['state'] == 'source_derived_candidate_requires_conditions'
    assert rate['conditional_rate_candidate']['rate'] == '12.00'
    assert rate['conditional_rate_candidate']['pending_condition_ids'] == [
        'edition_chain', 'project_consumption_priority', 'edge_prescription']
    assert not any(a['key'] in {'claim:quantity', 'card:quantity'} for a in plan['actions'])
    assert review == original


@pytest.mark.parametrize('change', ['text', 'missing_source', 'proposals', 'wrong_rate',
    'wrong_unit', 'duplicate_candidate', 'duplicate_source', 'certified_flag', 'missing_conditions', 'selected_app'])
def test_stale_or_unbound_candidate_does_not_redirect_normal_evidence_work(change):
    position, review, candidate = data()
    if change == 'text':
        review['sources'][0]['text'] += ' changed'
    elif change == 'missing_source':
        review['sources'].pop()
    elif change == 'proposals':
        review['parameter_proposals'].append({'parameter': 'quantity', 'source_id': 'project'})
    elif change == 'wrong_rate':
        candidate['rate'] = '13'
    elif change == 'wrong_unit':
        candidate['unit'] = 'м3/1000м2'
    elif change == 'duplicate_candidate':
        review['evidence_trace'].append(deepcopy(review['evidence_trace'][0]))
    elif change == 'duplicate_source':
        candidate['root_source_ids'].append('table')
    elif change == 'certified_flag':
        candidate['normative_basis_verified'] = True
    elif change == 'selected_app':
        review['items'].append(dict(state='confirmed', claim_ids=['normative:applicability'],
            project_source_ids=['project'], normative_source_ids=['table']))
    else:
        candidate['pending_condition_ids'] = []
    plan = build_work_plan(position, review, {})
    assert not any(a['action'] == 'verify_applicability_conditions' for a in plan['actions'])
    assert not any('conditional_rate_candidate' in a for a in plan['actions'])


def test_unread_registered_input_is_read_before_reviewing_candidate_conditions():
    from normative_source_registry import source_fingerprint
    position, review, _ = data()
    review['coverage']['model_context_packing'] = [dict(response_state='completed',
        delivery_contract_version=1, source_fingerprints={s['id']: source_fingerprint(s)
            for s in review['sources']}, full_text_source_ids=['project'],
        supplied_source_ids=['project'], next_offset_by_source={})]
    plan = build_work_plan(position, review, {})
    action = next(a for a in plan['actions'] if a['key'] == 'card:factor:57:12')
    assert action['action'] == 'read_registered_source'
    assert action['read_requests'] == [dict(tool='read_source', source_id='table', offset=0)]
    assert 'conditional_rate_candidate' in action


def test_required_unknown_aspect_survives_a_broad_confirmed_item_of_same_claim():
    position, review, _ = data()
    review['items'] += [dict(state='confirmed', required=True,
        claim_ids=['normative:applicability'], normative_source_ids=['table'],
        project_source_ids=['project'], explanation='Broad model C'),
        dict(state='needs_facts', required=True, claim_ids=['normative:applicability'],
            normative_source_ids=['table'], project_source_ids=[], explanation='Specific condition unknown')]
    plan = build_work_plan(position, review, {})
    action = next(a for a in plan['actions'] if a['key'] == 'claim:normative:applicability')
    assert action['reason'] == 'Specific condition unknown'
    assert not any(a['key'] == 'claim:quantity' for a in plan['actions'])
