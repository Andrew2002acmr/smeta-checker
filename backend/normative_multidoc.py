"""Pinned supporting PDFs with independent physical-page namespaces.

Matching retains the first explicit PDF as primary; normative validation can
search/read every explicitly linked, fully prepared document. No new OCR.
"""

from copy import copy


def attach(tools, payload):
    from audit_snapshot import load_snapshot

    tools.additional_documents = {}
    pins = tools.record["config"].get("pdf_versions") or {}
    documents = []
    for name, entry in (payload.get("supporting_documents") or {}).items():
        version = entry.get("version_id")
        if not version or pins.get(name) != version or version == tools.version:
            raise ValueError("Supporting PDF does not match its document pin")
        index = load_snapshot(tools.blob_store, entry["snapshot"])
        if (
            index.pending_ocr
            or not index.metadata.get("preparation_complete")
            or index.metadata.get("document_version_id") != version
        ):
            raise ValueError("Supporting PDF must be fully prepared and pinned")
        child = copy(tools)
        child.index, child.filename, child.version = index, name, version
        child.snapshot_refs = [entry["snapshot"]]
        child.additional_documents = {}
        child.is_supporting_document = True
        child.project_search = None
        child.project_search_results = {}
        child.table_search_done = False
        child.coverage = dict(
            state="prepared",
            pages=len(index.texts),
            pending_ocr_pages=[],
            error_pages=[int(p) + 1 for p in index.errors],
            document_version_id=version,
        )
        tools.additional_documents[version] = child
        documents.append(
            dict(document_version_id=version, filename=name, pages=len(index.texts))
        )
    if documents:
        tools.coverage["supporting_documents"] = documents
        tools.coverage["primary_document_version_id"] = tools.version


def route(tools, request):
    others = getattr(tools, "additional_documents", {})
    version = getattr(request, "document_version_id", "")
    if version:
        if version == tools.version:
            return None
        target = others.get(version)
        return (
            target._execute_single(request)
            if target
            else {"error": "unknown_document_version"}
        )
    if not others:
        return None
    if request.tool == "sum_cells":
        versions = {
            s.get("version_id") for s in tools.sources if s["id"] in request.operand_ids
        }
        if len(versions) > 1:
            return {"error": "cross_document_sum_requires_separate_scope_validation"}
        target = others.get(next(iter(versions), None))
        return target._execute_single(request) if target else None
    if request.tool not in {"search_project", "search_tables"}:
        return None
    replies = []
    for target in [tools, *others.values()]:
        result = target._execute_single(request)
        replies.append(
            dict(document_version_id=target.version, filename=target.filename, **result)
        )
    tools.table_search_done |= request.tool == "search_tables"
    return dict(
        pages=[p for r in replies for p in r.get("pages", [])],
        documents=replies,
        coverage=tools.coverage,
        repeated_query=all(r.get("repeated_query", False) for r in replies),
    )
