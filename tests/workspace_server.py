"""Explicitly synthetic E2E server. Never used by the production entry point."""

import argparse
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api import ApiContext, create_app
from audit_models import AuditSettings
from storage_blobs import FileBlobStore
from storage_db import open_repository
from test_api import _fake_pdf_bytes, _ok_client_factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8116)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    database = os.environ["TEST_POSTGRES_URL"]
    if "/smeta_test_ui" not in database:
        raise SystemExit("E2E requires its own smeta_test_ui database")
    if args.root.exists() and not args.resume:
        raise SystemExit("Use a fresh E2E root; existing files are never reset")
    if args.resume and not (args.root / "run-id.txt").is_file():
        raise SystemExit("Resume requires a previously initialized E2E fixture")
    args.root.mkdir(parents=True, exist_ok=True)
    blobs = FileBlobStore(args.root / "blobs")
    repository = open_repository(database, blobs)
    if repository.list_projects() and not args.resume:
        raise SystemExit("Use an empty smeta_test_ui database")
    settings = AuditSettings(
        database_url=database,
        blob_store_url=(args.root / "blobs").resolve().as_uri(),
        api_token="synthetic-e2e-only",
        yandex_cloud_api_key="synthetic-not-a-provider-key",
        yandex_cloud_folder="synthetic-test",
    )
    app = create_app(
        ApiContext(
            settings=settings,
            repository=repository,
            blob_store=blobs,
            client_factory=_ok_client_factory(),
        )
    )
    if args.resume:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return
    book = Workbook()
    sheet = book.active
    sheet.title = "ВОР"
    sheet.append(
        [
            "№ п.п.",
            "Наименование работ",
            "Объём работ / Количество",
            "Ед. изм.",
            "Ссылка на ПД",
            "Наименование файла",
            "Номер страниц (через пробел)",
            "Формула",
        ]
    )
    sheet.append([1, "Copper cable", 45, "m", "", "project.pdf", "1", "45"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    (args.root / "vor.xlsx").write_bytes(output.getvalue())
    (args.root / "project.pdf").write_bytes(_fake_pdf_bytes())
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer synthetic-e2e-only"
        versions = []
        for filename, kind, mime in [
            (
                "vor.xlsx",
                "vor",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("project.pdf", "pdf", "application/pdf"),
        ]:
            response = client.post(
                "/api/v1/documents",
                data={"project": "Синтетический стенд · интеграция", "kind": kind},
                files={"file": (filename, (args.root / filename).read_bytes(), mime)},
            )
            response.raise_for_status()
            versions.append(response.json()["version_id"])
        response = client.post(
            "/api/v1/audits",
            json={
                "project": "Синтетический стенд · интеграция",
                "vor_version_id": versions[0],
                "pdf_version_ids": [versions[1]],
            },
        )
        response.raise_for_status()
        run = response.json()["run_id"]
        from uuid import UUID

        if not app.state.runner.wait(UUID(run), 30):
            raise RuntimeError("Synthetic audit did not complete")
        (args.root / "run-id.txt").write_text(run, encoding="utf-8")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
