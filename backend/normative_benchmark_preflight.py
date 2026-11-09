"""Read-only binding of saved matching and prepared PD for an agent benchmark."""

from collections import Counter
from copy import deepcopy
import hashlib
from typing import Any
from uuid import UUID

from analysis_replay import digest
from audit_models import VorPosition
from audit_snapshot import load_snapshot
from normative_scope_graph import build


def pdf_identity(blob_store, document):
    """Attest both byte domains used by the existing preparation pipeline."""
    import pymupdf
    from pdf_text import PDFTextSource

    raw = blob_store.get(document.blob_key)
    original = hashlib.sha256(raw).hexdigest()
    if original != document.sha256:
        raise ValueError("Source PDF blob does not match the pinned document SHA")
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:
        extraction = PDFTextSource._pdf_sha256(pdf)
    return dict(original_sha256=original, extraction_sha256=extraction)


def inspect(
    context, matching_run_id, prepared_run_id, *, expected_count=103, indices=None
) -> dict[str, Any]:
    repo = context.repository
    matching = repo.load_run(matching_run_id)
    prepared = repo.load_run(prepared_run_id)
    if any(
        r["status"] not in {"done", "cancelled", "failed"} for r in (matching, prepared)
    ):
        raise ValueError("Benchmark inputs must come from stopped runs")
    if any(
        str(matching[k]) != str(prepared[k]) for k in ("project_id", "vor_version_id")
    ):
        raise ValueError(
            "Matching and preparation belong to different VOR/project versions"
        )
    config = matching["config"]
    updated = prepared["config"]
    if config.get("pdf_versions") != updated.get("pdf_versions") or config.get(
        "position_inputs"
    ) != updated.get("position_inputs"):
        raise ValueError(
            "PDF pins or VOR positions differ; names cannot establish equivalence"
        )
    positions = [
        VorPosition.model_validate(p) for p in config.get("position_inputs", [])
    ]
    if len(positions) != expected_count or len({p.index for p in positions}) != len(
        positions
    ):
        raise ValueError("Unexpected or duplicate VOR positions")
    selected = sorted({p.index for p in positions} if indices is None else set(indices))
    if not selected or not set(selected) <= {p.index for p in positions}:
        raise ValueError("Invalid benchmark selection")
    rows = matching["indexed_results"]
    indexed = {r["position_index"]: r["result"] for r in rows}
    if len(indexed) != len(rows):
        raise ValueError("Duplicate saved matching results")
    missing = sorted(set(selected) - indexed.keys())
    if missing:
        raise ValueError(
            "Saved matching is missing positions: " + ",".join(map(str, missing))
        )
    pins = config.get("pdf_versions") or {}
    corpora = updated.get("normative_corpora") or {}
    if not pins or not updated.get("document_preparation_version"):
        raise ValueError("Pinned complete PD preparation required")
    snapshots = {}
    documents = {}

    def snapshot(ref):
        key = digest(ref)
        if key not in snapshots:
            snapshots[key] = load_snapshot(context.blob_store, ref)
        return snapshots[key]

    jobs = []
    for index in selected:
        payload = repo.load_position_job(matching["id"], index).payload
        version = pins.get(payload.get("pdf_name"))
        if version is None and len(set(pins.values())) == 1:
            version = next(iter(pins.values()))
        if version is None or version not in corpora or not payload.get("snapshot"):
            raise ValueError(f"Position {index} has no unambiguous prepared PDF pin")
        if version not in documents:
            document = repo.load_version(UUID(version))
            prepared_index = snapshot(corpora[version])
            identity = pdf_identity(context.blob_store, document)
            snapshot_hash = prepared_index.metadata.get("pdf_sha256")
            from pdf_page_routing import deferred_drawing_errors

            deferred = deferred_drawing_errors(prepared_index)
            if (
                document.kind != "pdf"
                or snapshot_hash not in identity.values()
                or prepared_index.pending_ocr
                or not prepared_index.metadata.get("preparation_complete")
                or set(prepared_index.errors) - set(deferred)
            ):
                raise ValueError(
                    "Canonical prepared snapshot is incomplete or belongs to another PDF"
                )
            documents[version] = dict(
                version_id=version,
                pdf_sha256=snapshot_hash,
                pdf_identity=identity,
                snapshot=deepcopy(corpora[version]),
                pages=len(prepared_index.texts),
                tables=sum(len(t) for t in prepared_index.tables or []),
                deferred_visual_pages=list(deferred.values()),
            )
        frozen = snapshot(payload["snapshot"])
        if frozen.metadata.get("pdf_sha256") != documents[version]["pdf_sha256"]:
            raise ValueError("Frozen matching snapshot belongs to another PDF")
        jobs.append(
            dict(
                index=index,
                pdf_version_id=version,
                matching_result_sha256=digest(indexed[index]),
                matching_snapshot_sha256=digest(payload["snapshot"]),
            )
        )
    graph: dict[str, Any] = build({p.index: p for p in positions})
    report = dict(
        schema=1,
        mode="read_only_preflight",
        matching_run_id=str(matching["id"]),
        prepared_run_id=str(prepared["id"]),
        project_id=str(matching["project_id"]),
        vor_version_id=str(matching["vor_version_id"]),
        positions_sha256=digest(config["position_inputs"]),
        expected_count=expected_count,
        selected_indices=selected,
        documents=list(documents.values()),
        jobs=jobs,
        relations=deepcopy(graph["edges"]),
        repeated_patterns=deepcopy(graph["repeated_patterns"]),
        scope_ids={key: node["scope_id"] for key, node in graph["nodes"].items()},
        relationship_counts=dict(Counter(e["kind"] for e in graph["edges"])),
        cross_scope_edges=[
            e
            for e in graph["edges"]
            if graph["nodes"][str(e["source"])]["scope_id"]
            != graph["nodes"][str(e["target"])]["scope_id"]
        ],
        model_requests=0,
        ocr_requests=0,
        database_writes=0,
        target_confirmed=(expected_count + 1) // 2,
        performance_verified=False,
        confirmed_positions_not_measured=True,
    )
    report["input_fingerprint"] = digest(report)
    return report
