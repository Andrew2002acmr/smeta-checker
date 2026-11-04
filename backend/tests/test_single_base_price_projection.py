"""Bounded native construction + original pricing choices survive batch reads."""
from copy import deepcopy
import json

import pytest

from normative_conditional_selection import apply_guard
from normative_layer_assembly import single_base_price_application
from normative_parameters import build_cards
from normative_verdict import REVIEW, publish
from test_durable_tasks import pipeline as pipeline
from test_material_projection import offline_projection, material_pipeline as material_pipeline
from test_single_base_price_application import fixture


def review_fixture():
    position, review, positions = fixture()
    review['parameter_cards'] = build_cards(position, review, positions, {})
    assert not apply_guard(position, review, positions, {})
    review['position_key'] = 'single-base-key'
    # native is deliberately absent from model item refs and numeric proposals.
    assert all('native' not in i.get('project_source_ids', []) for i in review['items'])
    assert all(p['source_id'] != 'native' for p in review['parameter_proposals'])
    review['sources'].append(dict(id='unrelated', kind='project', text='private-ocr-noise'))
    review['evidence_trace'].extend([
        dict(kind='tool', output='private-ocr-noise'),
        dict(kind='tool', nested=dict(kind='single_base_price_application_v1', root_source_ids=['unrelated']))])
    review['telemetry'] = dict(private='private-ocr-noise')
    return position, review, positions


def assert_projected(position, review, positions, projected):
    assert single_base_price_application(position, projected, positions, {}) == single_base_price_application(position, review, positions, {})
    assert next(s for s in projected['sources'] if s['id']=='native') == next(s for s in review['sources'] if s['id']=='native')
    proof = next(e for e in projected['evidence_trace'] if e['kind']=='single_base_price_application_v1')
    assert set(proof['root_source_ids']) == {s['id'] for s in projected['sources']}
    choices = [e for e in projected['evidence_trace'] if e['kind']=='norm_thickness_adjustment_proposal_v1']
    assert len(choices)==2
    assert choices == [e for e in review['evidence_trace'] if e['kind']=='norm_thickness_adjustment_proposal_v1' and e['base_source_id']!='unrelated']
    assert 'private-ocr-noise' not in json.dumps(projected)
    assert projected['parameter_proposals']==review['parameter_proposals']


def test_selective_single_base_projection_preserves_unreferenced_native_and_original_choices():
    position, review, positions = review_fixture()
    original = deepcopy(review)
    projected, queries = offline_projection(review)
    assert_projected(position, review, positions, projected)
    assert api_status(position, review)==api_status(position, projected)
    assert api_status(position, projected)['Статус аудита']=='ОК'
    paths = ' '.join(str(v) for q in queries for v in q.params.values())
    assert 'single_base_price_application_v1' in paths
    assert 'norm_thickness_adjustment_proposal_v1' in paths
    assert review==original


def api_status(position, review):
    return publish({'Статус аудита': 'ОК'}, {'checks': []}, 'single-base-key',
        dict(stage='done', review=review), enabled=True, position=position)


def test_conflicting_third_choice_must_not_disappear_from_projection():
    position, review, positions = review_fixture()
    review['evidence_trace'].append(dict(kind='norm_thickness_adjustment_proposal_v1',
        base_source_id='base06', adjustment_source_id='step', signed_steps=-4,
        base_quote='толщиной 15 см', adjustment_quote='Другая поправка'))
    projected, _ = offline_projection(review)
    assert len([e for e in projected['evidence_trace'] if e['kind']=='norm_thickness_adjustment_proposal_v1'])==3
    assert single_base_price_application(position, review, positions, {}) is None
    assert single_base_price_application(position, projected, positions, {}) is None
    assert api_status(position, review)==api_status(position, projected)
    assert api_status(position, projected)['Статус аудита']==REVIEW


@pytest.mark.parametrize('mutation', ['none', 'native', 'choice', 'parameters', 'third_proposal', 'scope', 'unit', 'norm'])
def test_postgres_single_base_projection_replays_current_literals(material_pipeline, mutation):
    position, review, positions = review_fixture()
    if mutation=='native':
        next(s for s in review['sources'] if s['id']=='native')['text']='Другая конструкция'
    elif mutation=='choice':
        next(e for e in review['evidence_trace'] if e['kind']=='norm_thickness_adjustment_proposal_v1')['signed_steps']=-4
    elif mutation=='parameters':
        review['parameter_proposals']=[]
    elif mutation=='third_proposal':
        review['evidence_trace'].append(dict(kind='norm_thickness_adjustment_proposal_v1',
            base_source_id='unrelated', adjustment_source_id='step', signed_steps=99,
            base_quote='noise', adjustment_quote='noise'))
    elif mutation=='scope':
        next(s for s in review['sources'] if s['id']=='native')['text']=next(s for s in review['sources'] if s['id']=='native')['text'].replace('Тротуар', 'Съезд')
    elif mutation=='unit':
        next(s for s in review['sources'] if s['id']=='base07')['text']=next(s for s in review['sources'] if s['id']=='base07')['text'].replace('1000 м2', '100 м2')
    elif mutation=='norm':
        next(s for s in review['sources'] if s['id']=='step')['source_id']='different-norm'
    p = material_pipeline
    p.repo.save_normative_review(p.run.id, position.index, 'check',
        dict(position_key=review['position_key'], position_check=review), 'single-base-projection')
    projected = p.repo.normative_validation_context(p.run.id)[position.index]['review']
    assert api_status(position, review)==api_status(position, projected)
    if mutation=='none':
        assert_projected(position, review, positions, projected)
        assert api_status(position, projected)['Статус аудита']=='ОК'
    else:
        assert single_base_price_application(position, review, positions, {}) is None
        assert single_base_price_application(position, projected, positions, {}) is None
        assert api_status(position, projected)['Статус аудита']==REVIEW
