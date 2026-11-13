"""Repair pending header OCR in a separate durable preparation checkpoint; no LLM."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from pdf_header_preparation import complete_saved_headers, pending_header_pages
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / 'progress.json'
    progress = json.loads((path if path.exists() else args.checkpoint).read_text(encoding='utf-8-sig'))
    documents = progress['config']['prepared_documents']
    if len(documents) != 1:
        raise ValueError('Select one document checkpoint')
    name, state = next(iter(documents.items()))
    settings = AuditSettings.load()
    blobs = open_blob_store(settings.blob_store_url)
    repo = open_repository(settings.database_url, blobs, migrate_schema=False)
    version = repo.load_version(UUID(state['version_id']))
    raw = blobs.get(version.blob_key)
    if hashlib.sha256(raw).hexdigest() != version.sha256:
        raise ValueError('Source PDF hash mismatch')
    index = load_snapshot(blobs, state['snapshot'])
    parent = state.get('header_parent_snapshot', state['snapshot'])
    state.setdefault('header_repair_baseline', state['snapshot'])
    baseline = load_snapshot(blobs, state['header_repair_baseline'])
    before = {p: baseline.tables[p] for p in pending_header_pages(baseline)}

    def commit(updated):
        documents[name] = updated
        progress['config']['normative_corpora'][str(version.id)] = updated['snapshot']
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        print(json.dumps(dict(stage='headers', snapshot=updated['snapshot'],
            completed_pages=load_snapshot(blobs, updated['snapshot']).metadata.get('header_completed_pages'))), flush=True)

    state, index = complete_saved_headers(blobs, raw, state, index, commit=commit)
    for p, old in before.items():
        # Header ancestry is expected to improve; numeric readings and physical
        # body geometry must remain exactly the same.
        def body(ts):
            return [(t['table_id'], {k: v for k, v in c.items() if k in
                {'cell_id', 'text', 'bbox', 'row', 'column', 'rowspan', 'colspan'} or
                k.startswith('numeric')}) for t in ts for c in t['cells'] if c['role'] == 'cell']
        old_body, new_body = body(old), body(index.tables[p])
        if old_body != new_body:
            raise ValueError('Header preparation changed body cells')
    commit(state)
    report = dict(source_version=str(version.id), source_sha256=version.sha256, parent_snapshot=parent,
        snapshot=state['snapshot'], processed_pages=[p + 1 for p in before],
        incomplete_header_pages=[p + 1 for p in pending_header_pages(index)],
        model_requests=0, whole_page_ocr_calls=0, body_cells_unchanged=True,
        automatic_verdicts_changed=False, recognition_correctness_asserted=False)
    (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report['incomplete_header_pages']:
        raise SystemExit('Header observation is still incomplete')


if __name__ == '__main__':
    main()
