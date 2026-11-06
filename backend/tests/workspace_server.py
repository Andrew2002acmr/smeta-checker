"""Explicitly synthetic E2E server. Never used by the production entry point."""

import argparse
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi.testclient import TestClient
import pymupdf
from sqlalchemy.engine import make_url
from openpyxl import Workbook

from api import ApiContext, create_app
from audit_models import AuditSettings
from storage_blobs import FileBlobStore
from storage_db import open_repository
from test_api import _fake_pdf_bytes, _ok_client_factory


def write_selection_fixtures(root: Path) -> None:
    """Create a text table and two image-only pages for real PDF selection/OCR."""
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=400)
        for x in (50, 350, 420, 550):
            page.draw_line((x, 80), (x, 180))
        for y in (80, 110, 145, 180):
            page.draw_line((50, y), (550, y))
        for x, y, text in (
            (60, 100, "Work"), (360, 100, "Unit"), (430, 100, "Quantity"),
            (60, 132, "Copper cable"), (360, 132, "m"), (430, 132, "45"),
            (60, 167, "NEXT ROW"), (360, 167, "m"), (430, 167, "999"),
        ):
            page.insert_text((x, y), text, fontsize=11)
        document.save(root / "selection-table.pdf")

    with pymupdf.open() as scan:
        for left, right in (("COPPER CABLE", "222.78"), ("UNIT", "")):
            with pymupdf.open() as source:
                page = source.new_page(width=600, height=400)
                page.insert_text((50, 60), left, fontsize=24)
                if right:
                    page.insert_text((415, 60), right, fontsize=24)
                image = page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).tobytes("png")
            page = scan.new_page(width=600, height=400)
            page.insert_image(page.rect, stream=image)
        scan.save(root / "multi-scan.pdf")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8116)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--normative-profile", choices=["roads", "electrical"], help="Synthetic normative check fixture only")
    parser.add_argument("--ocr-container", help="Test only: run real crop Tesseract in this local Docker container")
    args = parser.parse_args()
    if args.ocr_container:
        import re
        import subprocess
        import ocr_engine
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", args.ocr_container):
            raise SystemExit("Invalid test OCR container name")
        def container_tsv(png_bytes, language="rus+eng", psm=6, timeout=10):
            code = f"import sys; from ocr_engine import run_tsv; sys.stdout.write(run_tsv(sys.stdin.buffer.read(), language={language!r}, psm={psm!r}, timeout={timeout!r}))"
            result = subprocess.run(["docker", "exec", "-i", args.ocr_container, "python", "-c", code], input=png_bytes, capture_output=True, timeout=timeout + 15)
            if result.returncode:
                raise RuntimeError("Test container OCR failed: " + result.stderr.decode("utf-8", "replace")[-200:])
            return result.stdout.decode("utf-8")
        ocr_engine.run_tsv = container_tsv
    database = os.environ["TEST_POSTGRES_URL"]
    if make_url(database).database != "smeta_test_ui":
        raise SystemExit("E2E requires its own smeta_test_ui database")
    fixture_files = ("run-id.txt", "vor.xlsx", "project.pdf", "selection-table.pdf",
                     "multi-scan.pdf", "table-run.json", "road-run.json", "progressive-run.json")
    if not args.resume and any((args.root / name).exists() for name in fixture_files):
        raise SystemExit("Use a fresh E2E fixture root; existing fixtures are never reset")
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
    import json
    from types import SimpleNamespace
    import normative_service

    def profile_for(record):
        for filename, profile in (("road-run.json", "roads"), ("table-run.json", "electrical")):
            path = args.root / filename
            if path.is_file() and str(record["project_id"]) == json.loads(path.read_text())["project_id"]:
                return profile
        return args.normative_profile or "electrical"

    normative_service.profile_for = profile_for
    audit_client_factory = _ok_client_factory()

    def client_factory(**kwargs):
        audit_client = audit_client_factory(**kwargs)

        def complete(**request):
            try:
                data = json.loads(request["input"])
            except (ValueError, TypeError):
                data = None
            if not isinstance(data, dict) or "sources" not in data:
                return audit_client.responses.create(**request)
            project = next(s for s in data["sources"] if s["kind"] == "project")
            norm = next(s for s in data["sources"] if s["kind"] == "normative")
            answer = dict(summary="Синтетическая точечная проверка: исходный прогон не изменён.", items=[dict(topic="ПД и норматив", state="needs_facts", explanation="Синтетический стенд: проверьте исходный норматив и проектную строку.", project_source_ids=[project["id"]], normative_source_ids=[norm["id"]])])
            return SimpleNamespace(output_text=json.dumps(answer), usage=SimpleNamespace(input_tokens=120, output_tokens=30, total_tokens=150))

        return SimpleNamespace(responses=SimpleNamespace(create=complete), close=audit_client.close)
    app = create_app(
        ApiContext(
            settings=settings,
            repository=repository,
            blob_store=blobs,
            client_factory=client_factory,
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
    write_selection_fixtures(args.root)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer synthetic-e2e-only"
        created = client.post(
            "/api/v1/projects", json={"name": "Синтетический стенд · интеграция"}
        )
        created.raise_for_status()
        project_id = created.json()["id"]
        departments = client.get(f"/api/v1/projects/{project_id}/departments")
        departments.raise_for_status()
        department_id = next(
            department["id"] for department in departments.json()
            if department["name"] == "Служба изысканий"
        )
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
                data={"project_id": project_id, "department_id": department_id, "kind": kind, "new_document": "true"},
                files={"file": (filename, (args.root / filename).read_bytes(), mime)},
            )
            response.raise_for_status()
            versions.append(response.json()["version_id"])
        response = client.post(
            "/api/v1/audits",
            json={
                "project_id": project_id,
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
    from progressive_fixture import seed as seed_progressive
    from table_layout_fixture import seed as seed_table

    seed_table(args.root, repository, blobs)
    seed_table(args.root, repository, blobs, road=True)
    seed_progressive(args.root, repository)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
