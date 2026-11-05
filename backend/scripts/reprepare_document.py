"""Resume standalone PD preparation, preserving every existing audit verdict.

Only prepared blobs and the versioned preparation cache are written. No audit
queue, OCR fallback during checking, or model request is dispatched here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from analysis_replay import prepared_identity, store_prepared
from audit_models import AuditSettings
from audit_snapshot import load_snapshot, load_json, store_json, store_snapshot
from document_preparation import prepare_documents, assemble_document
from storage_blobs import open_blob_store
from storage_db import open_repository
from pdf_page_routing import deferred_drawing_errors, VERSION as ROUTING_VERSION


class LocalPreparation:
    supports_analysis_replay = False

    def __init__(self, path, source, *, compatible_retry=False):
        self.path = path
        self.source = source
        if path.exists():
            self.value = json.loads(path.read_text(encoding="utf-8"))
            if self.value["source"] != source:
                previous = self.value["source"]
                if not compatible_retry or any(
                    previous.get(k) != source.get(k)
                    for k in ("stage", "project_id", "inputs")
                ):
                    raise ValueError(
                        "Preparation checkpoint belongs to another source/settings"
                    )
                self.value.setdefault("previous_processing_identities", []).append(
                    previous
                )
                self.value["source"] = source
        else:
            self.value = dict(source=source, config={})

    def load_run(self, run_id, **_):
        return dict(id=run_id, config=self.value["config"])

    def update_run_config(self, run_id, patch):
        self.value["config"].update(patch)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        progress = self.value["config"].get("pdf_preparation", {})
        print(json.dumps(dict(stage="preparation", **progress)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-id", required=True, type=UUID)
    parser.add_argument("--template-run", required=True, type=UUID)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    settings = AuditSettings.load()
    blobs = open_blob_store(settings.blob_store_url)
    repo = open_repository(settings.database_url, blobs, migrate_schema=False)
    document = repo.load_version(args.version_id)
    record = repo.load_run(args.template_run, include_results=False)
    if document.kind != "pdf" or str(document.id) not in set(
        record["config"].get("pdf_versions", {}).values()
    ):
        raise ValueError("Template does not pin the selected PDF version")
    raw = blobs.get(document.blob_key)
    if hashlib.sha256(raw).hexdigest() != document.sha256:
        raise ValueError("Source PDF hash differs from the registered version")
    identity = prepared_identity(record, raw, document.id)
    local = LocalPreparation(
        args.out / "progress.json", identity, compatible_retry=args.retry_failed
    )
    context = SimpleNamespace(repository=local, blob_store=blobs, settings=settings)

    def checkpoint():
        if (args.out / "CANCEL").exists():
            raise InterruptedError("Preparation paused; committed pages can be resumed")

    snapshots = prepare_documents(
        context,
        args.template_run,
        {document.filename: raw},
        {document.filename: document},
        checkpoint=checkpoint,
        cancelled=lambda: (args.out / "CANCEL").exists(),
    )
    if snapshots is None:
        raise InterruptedError("Preparation paused")
    ref = snapshots[document.filename]
    index = load_snapshot(blobs, ref)
    retry_pages = sorted(set(index.errors) - set(deferred_drawing_errors(index)))
    if args.retry_failed and retry_pages:
        from contextlib import closing
        import pymupdf
        from pdf_text import PDFTextSource

        state = local.value["config"]["prepared_documents"][document.filename]
        native = load_json(blobs, state["native"])
        pages = list(state["pages"])
        with (
            pymupdf.open(stream=bytes(raw), filetype="pdf") as doc,
            closing(
                PDFTextSource(doc, native_state=native, full_document=True)
            ) as source,
        ):
            for page in retry_pages:
                checkpoint()
                old_ref = pages[page]
                repaired = source.prepare_page(page)
                pages[page] = store_json(blobs, repaired)
                state.setdefault("page_retry_history", []).append(
                    dict(
                        page=page + 1,
                        previous=old_ref,
                        replacement=pages[page],
                        errors=repaired["errors"],
                    )
                )
                state["pages"] = list(pages)
                local.update_run_config(
                    args.template_run,
                    {"prepared_documents": {document.filename: state}},
                )
                print(
                    json.dumps(
                        dict(
                            stage="retry_page", page=page + 1, errors=repaired["errors"]
                        )
                    ),
                    flush=True,
                )
        index = assemble_document(native, [load_json(blobs, page) for page in pages])
        ref = store_snapshot(blobs, index)
        state["snapshot"] = ref
        state["failed_pages"] = list(index.metadata["failed_pages"])
        local.update_run_config(
            args.template_run,
            dict(
                prepared_documents={document.filename: state},
                normative_corpora={str(document.id): ref},
            ),
        )
    report = dict(
        source_sha256=document.sha256,
        version_id=str(document.id),
        snapshot=ref,
        pages=len(index.texts),
        tables=sum(len(page) for page in index.tables or []),
        errors=index.errors,
        pending_ocr=list(index.pending_ocr),
        complete=bool(index.metadata.get("preparation_complete")),
        identity=identity,
        model_requests=0,
        audit_tasks_dispatched=0,
    )
    deferred = deferred_drawing_errors(index)
    if deferred:
        index.metadata["page_routing_version"] = ROUTING_VERSION
        index.metadata["deferred_visual_pages"] = list(deferred.values())
        index.metadata["preparation_state"] = "complete_with_deferred_drawings"
        ref = store_snapshot(blobs, index)
        state = local.value["config"]["prepared_documents"][document.filename]
        state["snapshot"] = ref
        local.update_run_config(
            args.template_run,
            dict(
                prepared_documents={document.filename: state},
                normative_corpora={str(document.id): ref},
            ),
        )
        report["snapshot"] = ref
    report["deferred_visual_pages"] = list(deferred.values())
    report["blocking_errors"] = {
        p: e for p, e in index.errors.items() if p not in deferred
    }
    if report["blocking_errors"] or index.pending_ocr or not report["complete"]:
        report["ready"] = False
    else:
        real = SimpleNamespace(repository=repo, blob_store=blobs, settings=settings)
        state = local.value["config"]["prepared_documents"][document.filename]
        try:
            registered = store_prepared(real, identity, state)
            # First-committed caches must not silently return an unavailable old blob.
            cached = load_snapshot(blobs, registered["snapshot"])
            report["cache_snapshot"] = registered["snapshot"]
            blocked = set(cached.errors) - set(deferred_drawing_errors(cached))
            report["ready"] = (
                bool(cached.metadata.get("preparation_complete"))
                and not cached.pending_ocr
                and not blocked
            )
        except (ValueError, FileNotFoundError, KeyError) as exc:
            report["ready"] = False
            report["cache_registration_error"] = str(exc)
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
    if not report["ready"]:
        raise SystemExit(
            "Preparation has unresolved errors; model checking must remain blocked"
        )


if __name__ == "__main__":
    main()
