"""Evaluate saved OCR against separately annotated PDF regions; never runs recognition."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from pdf_ocr_acceptance import evaluate
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    progress = json.loads(args.checkpoint.read_text(encoding='utf-8-sig'))
    documents = progress['config']['prepared_documents']
    if len(documents) != 1:
        raise ValueError('Select one document checkpoint')
    state = next(iter(documents.values()))
    annotations = json.loads(args.annotations.read_text(encoding='utf-8-sig'))
    settings = AuditSettings.load()
    blobs = open_blob_store(settings.blob_store_url)
    repo = open_repository(settings.database_url, blobs, migrate_schema=False)
    version = repo.load_version(UUID(state['version_id']))
    raw = blobs.get(version.blob_key)
    if hashlib.sha256(raw).hexdigest() != version.sha256:
        raise ValueError('Source PDF hash mismatch')
    index = load_snapshot(blobs, state['snapshot'])
    if index.metadata.get('document_version_id') != str(version.id):
        raise ValueError('Snapshot belongs to another document version')
    result = evaluate(index, version.sha256, annotations)
    result.update(snapshot=state['snapshot'], source_sha256=version.sha256)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(total=result['total'], passed=result['passed'], failed=result['failed']), ensure_ascii=False))
    if result['failed']:
        raise SystemExit('OCR acceptance samples failed')


if __name__ == '__main__':
    main()
