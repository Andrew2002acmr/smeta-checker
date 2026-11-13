"""Check retrieval against PDF annotations without VOR jobs, OCR or model calls."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from normative_agent import EvidenceTools
from pdf_fact_acceptance import evaluate
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint',type=Path,required=True)
    parser.add_argument('--annotations',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    documents=json.loads(args.checkpoint.read_text(encoding='utf-8-sig'))['config']['prepared_documents']
    if len(documents)!=1:
        raise ValueError('Select one document checkpoint')
    state=next(iter(documents.values()))
    annotations=json.loads(args.annotations.read_text(encoding='utf-8-sig'))
    settings=AuditSettings.load()
    blobs=open_blob_store(settings.blob_store_url)
    repo=open_repository(settings.database_url,blobs,migrate_schema=False)
    version=repo.load_version(UUID(state['version_id']))
    if hashlib.sha256(blobs.get(version.blob_key)).hexdigest()!=version.sha256:
        raise ValueError('Source PDF hash mismatch')
    index=load_snapshot(blobs,state['snapshot'])
    sources={}
    def discover(query,**params):
        tools=EvidenceTools.from_prepared(index,version.id)
        candidates=tools.quantity_candidates(query,**params)
        sources.clear()
        sources.update((source['id'],source)for source in tools.sources)
        return candidates
    result=evaluate(index,version.sha256,annotations,discover,resolve_source=sources.get)
    result.update(snapshot=state['snapshot'],source_sha256=version.sha256)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k]for k in ('total','retrieved','exact_group_top1','physical_values_verified','source_links_verified')}))
    if result['physical_values_verified']!=result['total'] or result['source_links_verified']!=result['total']:
        raise SystemExit('Source-anchored retrieval acceptance failed')


if __name__=='__main__':
    main()
