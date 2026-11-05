"""A batch must replay raw dimensioned rates, retaining conflicting candidates."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from normative_parameters import enrich
from normative_resource_consumption import EVENT
from normative_resource_recovery import hydrate
from normative_workbook_rate import PROOF, geometric_rate_replay_supported
from test_areal_resource_consumption import fixture
from test_durable_tasks import pipeline as pipeline


def prepared():
    work, raw = fixture()
    review = enrich(work, raw, {work.index: work}, {})
    review['position_key'] = 'areal-resource-projection'
    assert geometric_rate_replay_supported(work, review)
    assert any(e.get('kind') == PROOF and 'resource_rate_basis' in e
        for e in review['evidence_trace'])
    return work, review


def save(p, work, review):
    p.repo.save_normative_review(p.run.id, work.index, 'check',
        dict(position_key=review['position_key'], position_check=review), 'areal-projection-test')
    return p.repo.normative_validation_context(p.run.id)[work.index]['review']


def test_actual_postgres_batch_retains_typed_rate_inputs_without_unrelated_noise(pipeline):
    work, review = prepared()
    review['evidence_trace'].append(dict(kind='tool', output='private-ocr-noise' * 1000))
    unrelated = deepcopy(next(e for e in review['evidence_trace'] if e.get('kind') == EVENT))
    unrelated.update(norm_id='unrelated-norm', norm_source_id='unrelated-source')
    unrelated['resources'][0]['name'] = 'unrelated-resource-noise'
    review['evidence_trace'].append(unrelated)
    review['sources'].append(dict(id='unrelated-source', kind='normative', text='private-ocr-noise'))
    batch = save(pipeline, work, review)
    assert geometric_rate_replay_supported(work, batch)
    events = [e for e in batch['evidence_trace'] if e.get('kind') == EVENT]
    assert len(events) == 1 and events[0]['norm_id'] != 'unrelated-norm'
    assert not any(e.get('kind') == 'tool' for e in batch['evidence_trace'])
    assert not any(s['id'] == 'unrelated-source' for s in batch['sources'])


@pytest.mark.parametrize('mutation', ['conflicting_row', 'conflicting_event', 'other_selected_norm',
    'lost_rows', 'changed_literal'])
def test_actual_postgres_batch_cannot_hide_changed_or_ambiguous_raw_rate(pipeline, mutation):
    work, review = prepared()
    event = next(e for e in review['evidence_trace'] if e.get('kind') == EVENT)
    if mutation == 'conflicting_row':
        row = deepcopy(event['resources'][0])
        row['resource_code'] = 'different-code'
        event['resources'].append(row)
    elif mutation == 'conflicting_event':
        extra = deepcopy(event)
        extra['resources'][0]['resource_code'] = 'different-code'
        review['evidence_trace'].append(extra)
    elif mutation == 'other_selected_norm':
        extra = deepcopy(event)
        extra.update(norm_id='other-norm', norm_code='27-06-036-01', norm_source_id='other-base')
        extra['resources'][0]['source_id'] = 'other-resource'
        header = deepcopy(next(s for s in review['sources'] if s['id'] == event['norm_source_id']))
        header.update(id='other-base', label=header['label'].replace(event['norm_code'], extra['norm_code']))
        header['text'] = header['text'].replace(event['norm_code'][:-3], extra['norm_code'][:-3])
        resource = deepcopy(next(s for s in review['sources'] if s['id'] == event['resources'][0]['source_id']))
        resource.update(id='other-resource', text=resource['text'].replace(event['norm_code'], extra['norm_code']))
        review['sources'].extend([header, resource])
        review['evidence_trace'].append(extra)
        review['items'][0]['normative_source_ids'].append('other-base')
    elif mutation == 'lost_rows':
        review['evidence_trace'] = [e for e in review['evidence_trace'] if e.get('kind') != EVENT]
    else:
        next(s for s in review['sources'] if s['id'] == event['resources'][0]['source_id'])['text'] = 'Changed literal'
    batch = save(pipeline, work, review)
    assert not geometric_rate_replay_supported(work, review)
    assert not geometric_rate_replay_supported(work, batch)
    if mutation in {'conflicting_row', 'conflicting_event', 'other_selected_norm'}:
        assert [e for e in batch['evidence_trace'] if e.get('kind') == EVENT] == [
            e for e in review['evidence_trace'] if e.get('kind') == EVENT]


@pytest.mark.parametrize('conflict', [False, True])
def test_recovered_resource_namespace_survives_actual_postgres_projection(pipeline, conflict):
    from test_normative_resource_recovery import KB, numeric_formatted, qualified_review

    work, raw = qualified_review()
    raw['evidence_trace'] = []
    with patch('normative_service.candidate', side_effect=numeric_formatted()):
        hydrate(KB('27-06-035-01'), raw)
    review = enrich(work, raw, {work.index: work}, {})
    review['position_key'] = 'recovered-areal-projection'
    assert geometric_rate_replay_supported(work, review)
    event = next(e for e in review['evidence_trace'] if e.get('kind') == EVENT)
    assert event['norm_source_id'] == 'n' and event['recovery_prefix'].startswith('recovered:')
    if conflict:
        extra = deepcopy(event)
        extra['resources'][0]['resource_code'] = 'conflicting-resource'
        review['evidence_trace'].append(extra)
    batch = save(pipeline, work, review)
    assert geometric_rate_replay_supported(work, batch) is not conflict
    assert [e for e in batch['evidence_trace'] if e.get('kind') == EVENT] == [
        e for e in review['evidence_trace'] if e.get('kind') == EVENT]
