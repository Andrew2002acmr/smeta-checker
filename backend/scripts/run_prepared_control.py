"""Fresh normative control on saved matching and prepared PDF; no OCR or queue.

Keeps the entire VOR as relation context, but publishes only selected positions.
Real provider requests are counted before transport, including failed attempts.
"""
import argparse
import faulthandler
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import UUID

import openai

from analysis_replay import algorithm, model_identity
from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from normative_benchmark_preflight import inspect
from normative_corpus import Corpus
from normative_position_check import check
from scripts.prepare_agent_control import PreparedView, PREPARED_VIEW
from storage_blobs import open_blob_store
from storage_db import open_repository
from workspace_models import NormativePositionCheckRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matching-run', required=True, type=UUID)
    parser.add_argument('--preparation', required=True, type=Path)
    parser.add_argument('--normative-root', required=True, type=Path)
    parser.add_argument('--normative-database', required=True)
    parser.add_argument('--normative-sha256', required=True)
    parser.add_argument('--indices', required=True, type=int, nargs='+')
    parser.add_argument('--max-requests', required=True, type=int)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    faulthandler.dump_traceback_later(90, repeat=True)
    if not os.environ.get('NORMATIVE_SQLITE_CACHE_DIR', '').strip():
        raise ValueError('Set NORMATIVE_SQLITE_CACHE_DIR to a native Docker volume before control')
    indices = sorted(set(args.indices))
    if len(indices) != len(args.indices) or not 1 <= len(indices) <= 20 or not 1 <= args.max_requests <= 200:
        raise ValueError('Explicit selection of 1..20 positions and a request budget required')
    args.out.mkdir(parents=True, exist_ok=args.resume)
    settings = AuditSettings.load()
    blobs = open_blob_store(settings.blob_store_url)
    repo = open_repository(settings.database_url, blobs, migrate_schema=False)
    if args.resume:
        status = json.loads((args.out / 'status.json').read_text(encoding='utf-8'))
        run = SimpleNamespace(id=UUID(status['run_id']))
        existing = repo.load_run(run.id, include_results=False)
        if (existing['status'] != 'running'
                or existing['config'].get('control_mode') != 'fresh_normative_on_saved_matching'
                or status['indices'] != indices
                or status['matching_run'] != str(args.matching_run)
                or status['max_requests'] != args.max_requests):
            raise ValueError('Resume requires the exact existing control and budget')
        status['state'] = 'running'
        return execute(args, settings, blobs, repo, run, status)
    matching = repo.load_run(args.matching_run)
    progress = json.loads((args.preparation / 'progress.json').read_text(encoding='utf-8'))
    report = json.loads((args.preparation / 'report.json').read_text(encoding='utf-8'))
    if report.get('ready') is not True:
        raise ValueError('Preparation not ready')
    kb = Corpus(args.normative_root, relative=args.normative_database, expected_sha=args.normative_sha256)
    prepared = deepcopy(matching)
    prepared['id'] = PREPARED_VIEW
    prepared['config'].update(progress['config'])
    preflight = inspect(SimpleNamespace(repository=PreparedView(repo, prepared), blob_store=blobs),
        args.matching_run, PREPARED_VIEW, expected_count=len(matching['config']['position_inputs']), indices=indices)
    # These source records are preserved before creating the independent control.
    (args.out / 'source-run.json').write_text(json.dumps(matching, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    config = {key: deepcopy(value) for key, value in prepared['config'].items() if key in {
        'project', 'model', 'provider', 'pdf_versions', 'pdf_bindings', 'vor_filename',
        'preflight', 'has_critical_error', 'position_inputs', 'prepared_documents',
        'normative_corpora', 'document_preparation_version', 'documents_prepared',
        'pdf_preparation', 'normative_coefficient_policy',
    }}
    config.update(project=str(config.get('project', 'Проверка')) + ' · контроль ' + str(len(indices)) + ' позиций',
        control_mode='fresh_normative_on_saved_matching', control_selected_indices=indices,
        control_matching_run=str(args.matching_run), normative_fresh_model=True,
        normative_background_followups=False, normative_after_match=False,
        normative_kb_pin=kb.identity, result_batch_size=10,
        control_request_limit=args.max_requests, control_code_fingerprint=algorithm('normative'))
    for ref in config['normative_corpora'].values():
        snapshot = load_snapshot(blobs, ref)
        if snapshot.pending_ocr or not snapshot.metadata.get('preparation_complete'):
            raise ValueError('No incomplete corpus may enter control')
    run = repo.create_run(matching['project_id'], matching['vor_version_id'], config,
        pdf_pins={k: UUID(v) for k, v in config['pdf_versions'].items()}, background=False)
    results = {v['position_index']: v['result'] for v in matching['indexed_results']}
    for index in indices:
        job = repo.load_position_job(args.matching_run, index)
        payload = deepcopy(job.payload)
        version = config['pdf_versions'][payload['pdf_name']]
        payload['snapshot'] = config['normative_corpora'][version]
        payload['pdf_versions'] = config['pdf_versions']
        repo.create_position_job(run.id, index, queue='llm', payload=payload)
        repo.save_position_result(run.id, index, results[index])
        repo.finish_position_job(run.id, index, 'done', result=results[index])
    status = dict(run_id=str(run.id), url=f'http://localhost:3000/audits/{run.id}',
        matching_run=str(args.matching_run), indices=indices, state='running',
        max_requests=args.max_requests, requests=0, completed=[], model=model_identity(settings),
        preflight=preflight, ocr_calls=0, old_verdicts_overwritten=0)

    execute(args, settings, blobs, repo, run, status)


def execute(args, settings, blobs, repo, run, status):
    indices = status['indices']
    def checkpoint():
        temp = args.out / 'status.tmp'
        temp.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        temp.replace(args.out / 'status.json')
        repo.update_run_config(run.id, {'control_progress': {k: status[k] for k in ('state', 'requests', 'completed', 'max_requests')}})

    def factory(**kwargs):
        client = openai.OpenAI(**kwargs)
        original = client.chat.completions.create

        def counted(*a, **kw):
            if (args.out / 'STOP').exists() or status['requests'] >= args.max_requests:
                raise RuntimeError('Control request budget exhausted or explicitly stopped')
            status['requests'] += 1
            checkpoint()
            started = time.monotonic()
            event = dict(request=status['requests'], index=status.get('current_index'), model=kw.get('model'), state='started')
            try:
                answer = original(*a, **kw)
                event.update(state='complete', usage=answer.usage.model_dump() if answer.usage else {})
                return answer
            except Exception as exc:
                event.update(state='failed', error_type=type(exc).__name__, http_status=getattr(exc, 'status_code', None))
                raise
            finally:
                event['seconds'] = round(time.monotonic() - started, 3)
                with (args.out / 'requests.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                print(json.dumps(event, ensure_ascii=False), flush=True)

        client.chat.completions.create = counted
        return client

    context = SimpleNamespace(settings=settings, repository=repo, blob_store=blobs, client_factory=factory)
    checkpoint()
    print(json.dumps(dict(run_id=str(run.id), url=status['url'], indices=indices, requests=0, ocr_calls=0)), flush=True)
    try:
        for index in indices:
            if any(p['index'] == index for p in status['completed']):
                continue
            if (args.out / 'STOP').exists() or status['requests'] >= args.max_requests:
                status['state'] = 'stopped_at_budget'
                break
            status['current_index'] = index
            checkpoint()
            record = repo.load_run(run.id)
            review = check(context, record, index, NormativePositionCheckRequest(), 'prepared-fresh-model-control', automatic=True)
            value = review['payload']['position_check']
            (args.out / f'position-{index}.json').write_text(json.dumps(review, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            status['completed'].append(dict(index=index, state=value['state'], execution_state=value.get('execution_state'), review_id=str(review['id'])))
            checkpoint()
            if value.get('execution_state') != 'complete':
                status['state'] = 'stopped_at_technical_failure'
                break
        else:
            status['state'] = 'done'
        repo.finish_run(run.id, 'done' if status['state'] == 'done' else 'cancelled')
    except Exception as exc:
        status.update(state='failed', error_type=type(exc).__name__, error=str(exc)[:600])
        repo.finish_run(run.id, 'failed')
        raise
    finally:
        checkpoint()
        print(json.dumps({k: status[k] for k in ('run_id', 'state', 'requests', 'completed')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
