"""Resume an interrupted run after stopping old workers; completed results stay intact."""

import argparse
from uuid import UUID

from audit_models import AuditSettings
from storage_blobs import open_blob_store
from storage_db import open_repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()
    settings = AuditSettings.load()
    repository = open_repository(
        settings.database_url,
        blob_store=open_blob_store(settings.blob_store_url),
        migrate_schema=False,
    )
    repository.resume_run(args.run_id)
    print(f"Audit {args.run_id} queued for resumption; start task-relay and workers.")


if __name__ == "__main__":
    main()
