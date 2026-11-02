"""Actual PostgreSQL projection must preserve publisher proof semantics."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from audit_models import VorPosition
from audit_review_policy import review_source
from normative_verdict import FIX, REVIEW, publish
from normative_claim_migration import VERSION as MIGRATION
from normative_workbook_arithmetic import PROOF as ARITHMETIC, arithmetic_claims
from test_durable_tasks import pipeline as pipeline


FIXTURE = Path(__file__).parent / 'fixtures/publication59-frozen.json'


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def save(pipeline, data, *, actor='projection-regression'):
    payload = dict(position_key=data['position_key'], position_check=data['position_check'])
    saved = pipeline.repo.save_normative_review(pipeline.run.id, data['position']['index'],
                                                'check', payload, actor)
    batch = pipeline.repo.normative_validation_context(pipeline.run.id)[data['position']['index']]
    return saved, batch


def verdict(data, review):
    position = VorPosition.model_validate(data['position'])
    task = position.legacy_task()
    task['derived_material'] = None
    stored = review_source(data['matching'], task)
    return publish({'Статус аудита': REVIEW}, stored, data['position_key'],
                   dict(stage='done', review=review), enabled=True, position=position)


def test_actual59_full_and_batch_ui_match_without_loading_or_mutating_tool_traces(pipeline):
    data = fixture()
    raw = deepcopy(data['position_check'])
    noise = [dict(kind='tool', text='private-ocr-trace-' * 2000),
             dict(kind='claim-parser-corrections-v1-extra', text='not-a-proof'),
             dict(kind=['claim-parser-corrections-v1'], text='not-a-typed-kind'),
             dict(kind='tool', nested=dict(kind=MIGRATION, text='not-top-level'))]
    data['position_check']['evidence_trace'].extend(noise)
    saved, batch = save(pipeline, data)
    full = dict(data['position_check'], position_key=data['position_key'])
    assert verdict(data, full)['Статус аудита'] == 'ОК'
    assert verdict(data, batch['review']) == verdict(data, full)
    expected = [event for event in raw['evidence_trace'] if event.get('kind') in {MIGRATION, ARITHMETIC}]
    assert batch['review']['evidence_trace'] == expected
    assert batch['review']['pipeline_version'] == raw['pipeline_version']
    assert not {'sources', 'telemetry', 'parameter_proposals'} & batch['review'].keys()
    assert 'private-ocr-trace-' not in json.dumps(batch, ensure_ascii=False)
    stored = pipeline.repo.normative_review(pipeline.run.id, 59, saved['id'])
    assert stored['payload']['position_check'] == data['position_check']

    # Exercise the actual row presenter, including its matching/report bridge.
    pipeline.repo.update_run_config(pipeline.run.id,
        dict(position_inputs=[data['position']], normative_after_match=True))
    pipeline.repo.save_position_result(pipeline.run.id, 59, data['matching'])
    pipeline.repo.finish_run(pipeline.run.id, 'done')
    from workspace_service import present_results
    with patch.object(pipeline.repo, 'schedule_local_normative_refresh', return_value=0):
        rows = present_results(pipeline.context, pipeline.repo.load_run(pipeline.run.id))
    row = next(row for row in rows if row.position_index == 59)
    assert row.queue == 'ok' and row.report_status == 'ОК'
    assert row.found_quantity == '874.42'


@pytest.mark.parametrize('mutation', ['wrong_proof_pin', 'wrong_review_pin', 'digest',
                                      'inputs', 'retired', 'duplicate', 'original_conflict'])
def test_bad_migration_proof_remains_blocking_in_full_and_batch(pipeline, mutation):
    data = fixture()
    proof = next(event for event in data['position_check']['evidence_trace'] if event.get('kind') == MIGRATION)
    expected_key = data['position_key']
    if mutation == 'wrong_proof_pin':
        proof['position_key'] = 'another-position'
    elif mutation == 'wrong_review_pin':
        data['position_key'] = 'another-position'
    elif mutation == 'digest':
        proof['input_sha256'] = '0' * 64
    elif mutation == 'inputs':
        proof['inputs']['comment'] = 'Укладка другим способом'
    elif mutation == 'retired':
        proof['retired'] = {}
    elif mutation == 'duplicate':
        data['position_check']['evidence_trace'].append(deepcopy(proof))
    elif mutation == 'original_conflict':
        check = next(c for c in data['matching']['checks'] if c['claim_id'] == 'attribute:comment:condition')
        check['state'] = 'source_conflict'
    _, batch = save(pipeline, data)
    full = dict(data['position_check'], position_key=data['position_key'])
    data['position_key'] = expected_key
    assert verdict(data, full)['Статус аудита'] == REVIEW
    assert verdict(data, batch['review'])['Статус аудита'] == REVIEW
    if mutation == 'duplicate':
        assert len([event for event in batch['review']['evidence_trace'] if event['kind'] == MIGRATION]) == 2


def test_latest_review_is_used_without_rewriting_older_proof(pipeline):
    data = fixture()
    first, _ = save(pipeline, data, actor='first-valid')
    old = deepcopy(first['payload'])
    next(event for event in data['position_check']['evidence_trace'] if event.get('kind') == MIGRATION)['input_sha256'] = '0' * 64
    second, batch = save(pipeline, data, actor='second-invalid')
    assert first['id'] != second['id']
    assert verdict(data, batch['review'])['Статус аудита'] == REVIEW
    assert pipeline.repo.normative_review(pipeline.run.id, 59, first['id'])['payload'] == old


@pytest.mark.parametrize('trace', ['missing', None, []])
def test_legacy_missing_or_null_trace_stays_empty_and_does_not_gain_migration(pipeline, trace):
    data = fixture()
    data['position_check'].pop('pipeline_version')
    if trace == 'missing':
        data['position_check'].pop('evidence_trace')
    else:
        data['position_check']['evidence_trace'] = trace
    _, batch = save(pipeline, data)
    assert batch['review']['evidence_trace'] == []
    assert batch['review']['pipeline_version'] == 0
    assert verdict(data, batch['review'])['Статус аудита'] == REVIEW


@pytest.mark.parametrize('tampered', [False, True])
def test_workbook_proof_is_preserved_and_recalculated_after_database_projection(pipeline, tampered):
    from test_normative_workbook_arithmetic import fixture as arithmetic_fixture, checked, status
    position, result = arithmetic_fixture()
    review = checked(position, result)
    assert arithmetic_claims(review) == {'workbook:formula'}
    if tampered:
        next(event for event in review['evidence_trace'] if event.get('kind') == ARITHMETIC)['expression'] = '108+54'
    review['evidence_trace'].append(dict(kind='tool', output='not-projected'))
    data = dict(position=position.model_dump(mode='json'), position_key='key', position_check=review)
    _, batch = save(pipeline, data)
    expected = REVIEW if tampered else 'ОК'
    assert status(review) == status(batch['review']) == expected
    assert arithmetic_claims(batch['review']) == (set() if tampered else {'workbook:formula'})
    assert {event['kind'] for event in batch['review']['evidence_trace']} <= {MIGRATION, ARITHMETIC}


def test_pipeline_version_keeps_numeric_conflict_gate_in_batch_view(pipeline):
    review = dict(pipeline_version=4, execution_state='complete', items=[
        dict(state='possible_mismatch', claim_ids=['quantity'], project_source_ids=['p'],
             normative_source_ids=['n'], explanation='Число якобы расходится.'),
        dict(state='needs_facts', claim_ids=['normative:applicability'], project_source_ids=[],
             normative_source_ids=[], explanation='Применимость ещё не подтверждена.')],
        parameter_cards=[dict(id='quantity', state='needs_facts', required=True,
                             label='Количество', reason='Число не подтверждено.')])
    data = dict(position=dict(index=1), position_key='numeric-key', position_check=review)
    _, batch = save(pipeline, data)
    stored = dict(checks=[dict(claim_id='quantity', state='contradiction', required=True)])

    def result(value):
        return publish({'Статус аудита': FIX}, stored, 'numeric-key',
                       dict(stage='done', review=dict(value, position_key='numeric-key')), enabled=True)['Статус аудита']

    assert result(review) == result(batch['review']) == REVIEW
    legacy = deepcopy(batch['review'])
    legacy.pop('pipeline_version')
    assert result(legacy) == FIX  # Proves why the version is semantic, not decorative.
