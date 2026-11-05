"""Inspect saved matching and prepared PD; no LLM, OCR or database writes."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from audit_models import AuditSettings
from normative_benchmark_preflight import inspect
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matching-run',required=True,type=UUID)
    parser.add_argument('--prepared-run',required=True,type=UUID)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--expected-count',type=int,default=103)
    args=parser.parse_args()
    settings=AuditSettings.load()
    blobs=open_blob_store(settings.blob_store_url)
    repo=open_repository(settings.database_url,blobs,migrate_schema=False)
    report=inspect(SimpleNamespace(repository=repo,blob_store=blobs),args.matching_run,args.prepared_run,
        expected_count=args.expected_count)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps(dict(positions=len(report['jobs']),documents=len(report['documents']),
        relationship_counts=report['relationship_counts'],model_requests=0,ocr_requests=0,
        database_writes=0,input_fingerprint=report['input_fingerprint']),ensure_ascii=False))


if __name__=='__main__':
    main()
