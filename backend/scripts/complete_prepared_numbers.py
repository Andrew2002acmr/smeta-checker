"""Complete saved physical table numbers, without VOR inputs, page OCR or LLM calls."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from pdf_header_preparation import complete_saved_numbers
from pdf_numeric_preparation import pending_numeric_pages
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--pages', type=int, nargs='+')
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
    parent = state.get('numeric_parent_snapshot', state['snapshot'])
    original = load_snapshot(blobs, parent)

    def commit(updated):
        documents[name] = updated
        progress['config']['normative_corpora'][str(version.id)] = updated['snapshot']
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        print(json.dumps(dict(stage='numeric-cells', snapshot=updated['snapshot'])), flush=True)

    state, index = complete_saved_numbers(blobs, raw, state, index, commit=commit, pages=args.pages)
    commit(state)
    changes = []
    for p, tables in enumerate(index.tables or ()):
        old_cells = {c['cell_id']: c for t in original.tables[p] for c in t['cells']}
        for t in tables:
            for c in t['cells']:
                old = old_cells.get(c['cell_id'])
                if old and c['text'] != old['text']:
                    changes.append(dict(page=p + 1, cell_id=c['cell_id'], bbox=c['bbox'],
                        before=old['text'], after=c['text'], numeric_verified=c.get('numeric_verified')))
    pending = [p + 1 for p in pending_numeric_pages(index)]
    report = dict(source_sha256=version.sha256, parent_snapshot=parent, snapshot=state['snapshot'],
        changes=changes, incomplete_numeric_pages=pending, model_requests=0, whole_page_ocr_calls=0,
        source_version=str(version.id), whole_document_ready=not pending)
    (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
