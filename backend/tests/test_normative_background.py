"""Queue isolation and failure safety against an isolated PostgreSQL database."""
from unittest.mock import Mock
from uuid import UUID

from audit_tasks import DEFAULT_TASK_ROUTES
from test_durable_tasks import pipeline as pipeline


NAME = "audit_tasks.normative_position"


def test_standalone_relay_uses_the_same_normative_queue_as_workers():
    from scripts.dispatch_tasks import queue_for_task
    assert queue_for_task(NAME) == DEFAULT_TASK_ROUTES[NAME]['queue'] == 'normative'
    assert queue_for_task('audit_tasks.audit_position') == 'llm'
    assert queue_for_task('audit_tasks.prepare_run') == 'pdf'


def test_completion_atomically_schedules_only_once(pipeline):
    p = pipeline
    p.repo.update_run_config(p.run.id, {"normative_after_match": True})
    p.repo.create_position_job(p.run.id, 0, queue="llm", payload={})
    p.repo.finish_position_job(p.run.id, 0, "done", result={"status": "ОК"})
    p.repo.finish_position_job(p.run.id, 0, "done", result={"status": "ОК"})
    deliveries = p.repo.reserve_deliveries()
    assert len(deliveries) == 1 and deliveries[0]['name'] == NAME
    assert DEFAULT_TASK_ROUTES[NAME]['queue'] == 'normative'
    assert DEFAULT_TASK_ROUTES['audit_tasks.audit_position']['queue'] == 'llm'
    # The outbox stores the index inside JSON payload, not as an ORM column.
    assert p.repo.normative_validation_context(p.run.id) == {0: {'stage': 'dispatched'}}


def test_large_matching_backlog_does_not_starve_normative_or_consume_its_slots(pipeline):
    p = pipeline
    for i in range(130):
        p.repo.enqueue_task('audit_tasks.audit_position', dict(run_id=str(p.run.id), position_index=i))
    for i in range(5):
        p.repo.enqueue_task(NAME, dict(run_id=str(p.run.id), position_index=i))
    deliveries = p.repo.reserve_deliveries()
    assert sum(t['name'] == NAME for t in deliveries) == 2
    assert sum(t['name'] != NAME for t in deliveries) == 32
    assert p.repo.reserve_deliveries() == []


def test_normative_can_finish_after_matching_and_failure_never_fails_run(pipeline):
    p = pipeline
    p.repo.enqueue_task(NAME, dict(run_id=str(p.run.id), position_index=0))
    p.repo.finish_run(p.run.id, 'done')
    delivery = p.repo.reserve_deliveries()[0]
    task, token = UUID(delivery['id']), UUID(delivery['token'])
    assert p.repo.claim_delivery(task, token, timeout=30)
    assert not p.repo.claim_delivery(task, token, timeout=30)
    p.repo.settle_delivery(task, token, error='synthetic model failure')
    assert p.repo.run_status(p.run.id) == 'done'
    assert p.repo.normative_stage(p.run.id, 0)['status'] == 'failed'
    counts = p.repo.run_progress_records(run_id=p.run.id)[0]['normative_counts']
    assert counts == {'failed': 1}
    assert p.repo.reserve_deliveries() == []  # No automatic paid retry.


def test_stopped_run_does_not_start_normative(pipeline):
    p = pipeline
    p.repo.enqueue_task(NAME, dict(run_id=str(p.run.id), position_index=0))
    p.repo.finish_run(p.run.id, 'cancelled')
    assert p.repo.reserve_deliveries() == []
    assert p.repo.normative_stage(p.run.id, 0)['status'] == 'cancelled'


def test_page_cache_is_shared_across_positions_but_not_pdf_versions(pipeline):
    p = pipeline
    build = Mock(return_value=dict(origin='ocr_unverified', text='synthetic', words_key='words'))
    assert p.repo.cached_normative_page(p.run.id, 'version1:89:v3', build)['text'] == 'synthetic'
    p.repo.cached_normative_page(p.run.id, 'version1:89:v3', build)
    assert build.call_count == 1
    p.repo.cached_normative_page(p.run.id, 'version2:89:v3', build)
    assert build.call_count == 2


def test_old_saved_reviews_are_requeued_locally_once_for_the_whole_run(pipeline):
    p=pipeline
    p.repo.update_run_config(p.run.id, {'normative_after_match':True})
    for index in (0,2):
        p.repo.save_normative_review(p.run.id,index,'check',dict(position_check={}), 'synthetic-old')
    p.repo.finish_run(p.run.id,'done')
    assert p.repo.schedule_local_normative_refresh(p.run.id,1)==2
    assert p.repo.schedule_local_normative_refresh(p.run.id,1)==0
    jobs=p.repo.reserve_deliveries()
    assert len(jobs)==2 and all(t['name']==NAME for t in jobs)
    assert all(t['payload']['local_only'] is True for t in jobs)
    assert p.repo.run_status(p.run.id)=='done'


def test_local_refresh_never_falls_back_to_a_paid_call_without_a_saved_review(pipeline):
    from normative_background import review_position
    from unittest.mock import patch
    p=pipeline
    p.repo.update_run_config(p.run.id, {'normative_after_match':True})
    with patch('normative_position_check.check') as paid:
        assert review_position(p.context,p.run.id,0,local_only=True)=={'skipped':'no_saved_review'}
        paid.assert_not_called()


def test_saved_review_upgrade_uses_local_validation_and_does_not_repeat_it(pipeline):
    from normative_background import review_position
    from unittest.mock import patch
    p=pipeline
    p.repo.update_run_config(p.run.id, {'normative_after_match':True})
    saved=p.repo.save_normative_review(p.run.id,2,'check',dict(position_check={}), 'synthetic-old')
    with patch('normative_position_check.check') as paid, patch('normative_position_check.validate_saved',return_value=saved) as local:
        assert review_position(p.context,p.run.id,2,local_only=True)['local_only']
        local.assert_called_once()
        paid.assert_not_called()
    p.repo.save_normative_review(p.run.id,2,'check',dict(position_check={'local_rules_version':1}), 'synthetic-upgraded')
    with patch('normative_position_check.validate_saved') as local:
        assert review_position(p.context,p.run.id,2,local_only=True)=={'skipped':'already_reviewed'}
        local.assert_not_called()
