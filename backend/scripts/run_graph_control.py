"""Reproducible bounded graph control on pinned originals.

Uses the production repository/preparation/outbox/graph/finalizer path.
Never invents answers, gold labels, or provider credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

EXPECTED_VOR_SHA256 = "4b5d03827ea0ce64892576eac956ba3c2f3c3e794d62be431e0255c61926c7aa"
EXPECTED_PDF_SHA256 = "e48108c661864b6e086f4c8c265a7ac0edd398cfc22cf0f6155c986751154c9f"
EXPECTED_POSITIONS = 103
EXPECTED_MODEL = "deepseek/deepseek-v4.1-flash"
EXPECTED_HOST = "routerai.ru"


def _database_url(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or "smeta_test" not in value and "graph_control" not in value:
        raise ValueError(f"{name} must point to an isolated test database")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_indices(text: str) -> list[int]:
    selected: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            selected.update(range(int(start), int(end) + 1))
        elif part:
            selected.add(int(part))
    return sorted(selected)


def _open_repository(database_url: str, root: Path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from storage_blobs import FileBlobStore
    from storage_db import open_repository

    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    return open_repository(database_url, blob_store=FileBlobStore(str(blobs)))


def cmd_prepare(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    if (root / "manifest.json").exists():
        raise ValueError("Refusing to overwrite an existing control manifest")
    root.mkdir(parents=True, exist_ok=True)
    vor = Path(args.vor)
    pdf = Path(args.pdf)
    vor_sha, pdf_sha = _sha256(vor), _sha256(pdf)
    if vor_sha != EXPECTED_VOR_SHA256 or pdf_sha != EXPECTED_PDF_SHA256:
        raise ValueError("Control inputs do not match the pinned originals")
    database_url = _database_url(args.database_env)
    repository = _open_repository(database_url, root)
    repository.reset_for_tests()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vor_parse import load_vor_positions

    positions, _ = load_vor_positions(vor.read_bytes(), vor.name)
    if len(positions) != EXPECTED_POSITIONS:
        raise ValueError(f"Expected {EXPECTED_POSITIONS} positions, got {len(positions)}")
    selected = _parse_indices(args.indices)
    project = repository.create_project("graph-control")
    vor_version = repository.add_document_version(
        project.id, vor.read_bytes(), filename=vor.name, kind="vor", new_document=True)
    pdf_version = repository.add_document_version(
        project.id, pdf.read_bytes(), filename=pdf.name, kind="pdf", new_document=True)
    run = repository.create_run(
        project.id, vor_version.id,
        {"project": "graph-control",
         "pdf_versions": {pdf.name: str(pdf_version.id)},
         "position_inputs": [position.model_dump(mode="json") for position in positions],
         "engine": "langgraph-evidence-v1", "normative_profile": args.profile,
         "max_http_attempts": int(args.max_http_attempts)},
        pdf_pins={pdf.name: pdf_version.id}, selected_indices=selected)
    manifest = {
        "schema": 1, "root": str(root), "database_env": args.database_env,
        "vor_sha256": vor_sha, "pdf_sha256": pdf_sha,
        "input_position_count": len(positions), "selected_indices": selected,
        "profile": args.profile, "max_http_attempts": int(args.max_http_attempts),
        "run_id": str(run.id), "project_id": str(project.id),
        "vor_version_id": str(vor_version.id), "pdf_version_id": str(pdf_version.id),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"run_id": str(run.id), "selected": len(selected)}, ensure_ascii=False))


def _load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text("utf-8"))
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported control manifest")
    return manifest


def cmd_run(args: argparse.Namespace) -> None:
    manifest = _load_manifest(Path(args.manifest))
    database_url = _database_url(args.database_env or manifest["database_env"])
    root = Path(manifest["root"])
    repository = _open_repository(database_url, root)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.bump_revision:
        from audit_assessment import PositionAssessment

        run_id = UUID(manifest["run_id"])
        bumped = 0
        for index in manifest["selected_indices"]:
            try:
                job = repository.load_position_job(run_id, int(index))
            except KeyError:
                continue
            if job.status == "done" and job.result:
                continue
            payload = dict(job.payload or {})
            try:
                stored = PositionAssessment.model_validate(job.result or {}).model_dump(mode="json")
                revision = int(stored.get("execution_revision", 0)) + 1
            except Exception:
                revision = int(payload.get("execution_revision", 0)) + 1
            payload["execution_revision"] = revision
            repository.requeue_position_job(run_id, int(index), queue="llm", payload=payload)
            bumped += 1
        print(json.dumps({"run_id": str(run_id), "revisions_bumped": bumped}, ensure_ascii=False))
        return
    if not args.fresh_model:
        raise ValueError("--fresh-model is required for a control measurement")
    from audit_models import AuditSettings
    from audit_tasks import TaskContext, finalize_run, prepare_run
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from storage_blobs import FileBlobStore
    from storage_db import DocumentVersionRow
    from storage_models import AuditModelCall

    settings = AuditSettings.load()
    credentials = settings.llm_credentials()
    if credentials.model_name != EXPECTED_MODEL or EXPECTED_HOST not in credentials.base_url:
        raise ValueError("Control requires the approved RouterAI model")
    run_id = UUID(manifest["run_id"])
    record = repository.load_run(run_id, include_results=False)
    status = str(record.get("status"))
    if args.resume:
        if status == "done":
            raise ValueError("Control run is done; nothing to resume")
        if status in {"failed", "cancelled"}:
            repository.resume_run(run_id, allow_running=False)
        else:
            recovered = repository.recover_stale_jobs(run_id, alive_workers=set())
            if recovered:
                print(json.dumps({"run_id": str(run_id), "recovered_jobs": recovered}, ensure_ascii=False))
    elif status != "running":
        raise ValueError(f"Control run is {status}; pass --resume to reopen it")
    vor_version = repository.load_version(UUID(manifest["vor_version_id"]))
    pdf_version = repository.load_version(UUID(manifest["pdf_version_id"]))
    blobs = FileBlobStore(str(root / "blobs"))
    context = TaskContext(settings=settings, repository=repository, blob_store=blobs, client_factory=None)
    pdf_row = DocumentVersionRow(
        id=pdf_version.id, document_id=pdf_version.document_id, version_no=pdf_version.version_no,
        sha256=pdf_version.sha256, blob_key=pdf_version.blob_key, size=pdf_version.size,
        filename=pdf_version.filename)
    out = {"continue": True}
    slices = 0
    while out.get("continue"):
        slices += 1
        if slices > 50:
            raise RuntimeError("Preparation did not converge")
        out = prepare_run(
            context, run_id, project="graph-control", vor_filename=vor_version.filename,
            vor_bytes=blobs.get(vor_version.blob_key),
            pdf_files={pdf_version.filename: blobs.get(pdf_version.blob_key)},
            pdf_versions={pdf_version.filename: pdf_row},
            worker="graph-control", slice_seconds=600)
    if out.get("cancelled"):
        raise RuntimeError("Control run was cancelled; reopen it before resume")
    from audit_tasks import audit_position

    max_attempts = int(manifest["max_http_attempts"])
    for index in manifest["selected_indices"]:
        with Session(repository._engine) as session:
            spent = session.scalar(select(func.coalesce(func.sum(AuditModelCall.attempt_no), 0)).where(
                AuditModelCall.run_id == run_id)) or 0
        if spent >= max_attempts:
            raise ValueError(f"Control budget exhausted: {spent}/{max_attempts} attempts spent")
        try:
            job = repository.load_position_job(run_id, int(index))
        except KeyError:
            job = None
        if job is not None and job.status == "done" and job.result:
            continue
        audit_position(context, run_id, int(index), worker="graph-control", request_id="graph-control")
    finalize_run(context, run_id, worker="graph-control")
    print(json.dumps({"run_id": str(run_id), "queued": out["queued"]}, ensure_ascii=False))


def cmd_report(args: argparse.Namespace) -> None:
    manifest = _load_manifest(Path(args.manifest))
    database_url = _database_url(args.database_env or manifest["database_env"])
    repository = _open_repository(database_url, Path(manifest["root"]))
    run_id = UUID(manifest["run_id"])
    record = repository.load_run(run_id)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from storage_models import AuditModelCall

    with Session(repository._engine) as session:
        ledger_rows, ledger_attempts = session.execute(select(
            func.count(), func.coalesce(func.sum(AuditModelCall.attempt_no), 0),
        ).where(AuditModelCall.run_id == run_id)).one()
    rows = []
    for item in record["indexed_results"]:
        stored = item["result"] or {}
        rows.append({
            "position_index": item["position_index"],
            "schema": stored.get("schema_version"),
            "project": (stored.get("project") or {}).get("state"),
            "normative": (stored.get("normative") or {}).get("state"),
            "relationships": (stored.get("relationships") or {}).get("state"),
            "completion": stored.get("completion"),
            "own_quantity": (stored.get("own_quantity") or {}).get("state"),
            "revision": stored.get("revision"),
            "http_attempts": (stored.get("execution") or {}).get("http_attempts", 0),
        })
    state_attempts = sum(row["http_attempts"] for row in rows)
    report = {"run_id": manifest["run_id"],
              "ledger_rows": int(ledger_rows), "ledger_http_attempts": int(ledger_attempts),
              "state_http_attempts": state_attempts, "total_http_attempts": state_attempts,
              "max_http_attempts": manifest["max_http_attempts"], "rows": rows}
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--database-env", required=True)
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--vor", required=True)
    prepare.add_argument("--pdf", required=True)
    prepare.add_argument("--profile", required=True, choices=["roads", "electrical"])
    prepare.add_argument("--indices", required=True)
    prepare.add_argument("--max-http-attempts", required=True)
    prepare.set_defaults(func=cmd_prepare)
    run = sub.add_parser("run")
    run.add_argument("--database-env", required=False, default=None)
    run.add_argument("--manifest", required=True)
    run.add_argument("--fresh-model", action="store_true")
    run.add_argument("--resume", action="store_true",
                     help="Reopen a failed/cancelled control run via resume_run (no raw SQL).")
    run.add_argument("--bump-revision", action="store_true",
                     help="Bump execution_revision on unfinished positions for a fresh thread_id.")
    run.set_defaults(func=cmd_run)
    report = sub.add_parser("report")
    report.add_argument("--database-env", required=False, default=None)
    report.add_argument("--manifest", required=True)
    report.set_defaults(func=cmd_report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
