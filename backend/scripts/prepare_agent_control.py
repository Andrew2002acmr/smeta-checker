"""Freeze a fresh-model control plan over an already prepared document.

Reads only: never creates an audit, dispatches a task, calls OCR or an LLM.
Past normative answers are not loaded or supplied as expected model answers.
"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from analysis_replay import algorithm, digest, model_identity
from audit_models import AuditSettings
from audit_snapshot import load_snapshot
from normative_benchmark_preflight import inspect
from normative_corpus import Corpus
from storage_blobs import open_blob_store
from storage_db import open_repository

PREPARED_VIEW = UUID("00000000-0000-0000-0000-000000000031")


class PreparedView:
    def __init__(self, repository, prepared):
        self.repository = repository
        self.prepared = prepared

    def load_run(self, run_id):
        if run_id == PREPARED_VIEW:
            return deepcopy(self.prepared)
        return self.repository.load_run(run_id)

    def __getattr__(self, name):
        return getattr(self.repository, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matching-run", required=True, type=UUID)
    parser.add_argument("--preparation", required=True, type=Path)
    parser.add_argument("--normative-root", required=True, type=Path)
    parser.add_argument("--normative-database", required=True)
    parser.add_argument("--normative-sha256", required=True)
    parser.add_argument("--indices", required=True, nargs="+", type=int)
    parser.add_argument("--expected-count", type=int, default=103)
    parser.add_argument("--inspect-pages", nargs="*", type=int, default=[])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if len(set(args.indices)) != len(args.indices) or not 1 <= len(args.indices) <= 20:
        raise ValueError("Select 1..20 distinct position indices")
    progress = json.loads(
        (args.preparation / "progress.json").read_text(encoding="utf-8")
    )
    preparation = json.loads(
        (args.preparation / "report.json").read_text(encoding="utf-8")
    )
    if preparation.get("ready") is not True:
        raise ValueError("Document preparation has not passed its readiness gate")
    settings = AuditSettings.load()
    blobs = open_blob_store(settings.blob_store_url)
    repository = open_repository(settings.database_url, blobs, migrate_schema=False)
    matching = repository.load_run(args.matching_run)
    prepared = deepcopy(matching)
    prepared["id"] = PREPARED_VIEW
    prepared["config"].update(progress["config"])
    view = PreparedView(repository, prepared)
    context = SimpleNamespace(repository=view, blob_store=blobs)
    preflight = inspect(
        context,
        args.matching_run,
        PREPARED_VIEW,
        indices=args.indices,
        expected_count=args.expected_count,
    )
    kb = Corpus(
        args.normative_root,
        relative=args.normative_database,
        expected_sha=args.normative_sha256,
    )
    missing = []
    with kb.connect() as db:
        for source in db.execute("SELECT id,sha256 FROM sources"):
            path = kb.source_pdf(source["id"])
            if not path.is_file():
                missing.append(source["id"])
                continue
            with path.open("rb") as stream:
                if (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    != source["sha256"]
                ):
                    raise ValueError("Normative original hash changed: " + source["id"])
    index = load_snapshot(blobs, preparation["snapshot"])
    page_quality = {}
    for page in args.inspect_pages:
        if not 1 <= page <= len(index.texts):
            raise ValueError("Inspection page is outside the prepared document")
        tables = (index.tables or [])[page - 1]
        page_quality[str(page)] = dict(
            text_chars=len(index.texts[page - 1]),
            tables=len(tables),
            cells=sum(len(t.get("cells", [])) for t in tables),
            quality=index.quality.get(page - 1),
        )
    plan = dict(
        schema=1,
        mode="prepared_fresh_model_control",
        ready_for_control=True,
        status="prepared_not_dispatched",
        selected_indices=sorted(args.indices),
        matching_run_id=str(args.matching_run),
        project_id=str(matching["project_id"]),
        preflight=preflight,
        preparation=preparation,
        normative_pin=kb.identity,
        model=model_identity(settings),
        page_quality=page_quality,
        deferred_visual_pages=preparation.get("deferred_visual_pages", []),
        code_fingerprint=algorithm("normative"),
        missing_normative_originals=missing,
        execution_config=dict(
            normative_fresh_model=True,
            normative_background_followups=False,
            documents_prepared=True,
            document_preparation_version=progress["config"][
                "document_preparation_version"
            ],
            normative_kb_pin=kb.identity,
            normative_corpora=progress["config"]["normative_corpora"],
            normative_coefficient_policy="estimator_value_normative_conditions",
            result_batch_size=10,
        ),
        limitations=[
            "New preparation snapshot; this is not byte-identical restoration of the home OCR snapshot.",
            "Normative edition applicability is evaluated separately; no automatic claim of currentness.",
            "Normative release is pinned for this control only; global CURRENT is unchanged.",
            "Confirmation count is not guaranteed and historical confirmed labels are not model inputs.",
            "A paid-call budget must be explicitly supplied at execution; this command sends no requests.",
        ],
        model_requests=0,
        ocr_requests=0,
        database_writes=0,
    )
    plan["plan_fingerprint"] = digest(plan)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2, default=str)
    print(
        json.dumps(
            dict(
                status=plan["status"],
                positions=len(args.indices),
                normative_pin=kb.identity,
                model=plan["model"],
                missing_originals=missing,
                model_requests=0,
                ocr_requests=0,
            )
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
