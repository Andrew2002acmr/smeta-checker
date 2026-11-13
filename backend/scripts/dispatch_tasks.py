"""Broker-independent outbox relay and lease recovery. Run one instance."""

import logging
import time
from pathlib import Path

from celery import Celery

from audit_delivery import publish_pending
from audit_models import AuditSettings
from storage_blobs import open_blob_store
from storage_db import open_repository


def queue_for_task(name):
    return {'audit_tasks.audit_position':'llm', 'audit_tasks.normative_position':'normative'}.get(name, 'pdf')


def main():
    logging.basicConfig(level=logging.INFO)
    settings = AuditSettings.load()
    repository = open_repository(
        settings.database_url,
        blob_store=open_blob_store(settings.blob_store_url),
        migrate_schema=False,
    )
    app = Celery("smeta-relay", broker=settings.broker_url)

    def dispatch(name, payload):
        app.send_task(
            name,
            kwargs=payload,
            queue=queue_for_task(name),
        )

    while True:
        try:
            publish_pending(repository, dispatch)
            Path("/tmp/smeta-relay-heartbeat").touch()
        except Exception:
            logging.exception("outbox relay iteration failed")
        time.sleep(5)


if __name__ == "__main__":
    main()
