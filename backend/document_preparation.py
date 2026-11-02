"""Full, resumable document preparation barrier before any paid position task."""

from contextlib import closing, nullcontext
from typing import Any

from audit_core import PageTextIndex
from audit_snapshot import load_json, load_snapshot, store_json, store_snapshot
from audit_tracing import span

VERSION = 1


def assemble_document(native, packed):
    from pdf_text import _repeated_lines

    if [p["page"] for p in packed] != list(range(1, len(native["texts"]) + 1)):
        raise ValueError("Prepared document has missing or unordered pages")
    errors = {i: "; ".join(p["errors"]) for i, p in enumerate(packed) if p["errors"]}
    texts = tuple(p["text"] for p in packed)
    return PageTextIndex(
        texts,
        errors,
        (),
        tuple(tuple(p["table_rows"]) for p in packed),
        locators=tuple(p["locators"] for p in packed),
        tables=tuple(p["tables"] for p in packed),
        quality={i: p["quality"] for i, p in enumerate(packed)},
        metadata=dict(
            pdf_sha256=native["pdf_sha256"],
            extraction_key=native["extraction_key"],
            ocr_settings_key=native["ocr_settings_key"],
            preparation_version=VERSION,
            ocr=native.get("ocr", {}),
            document_version_id=native.get("document_version_id"),
            preparation_complete=True,
            preparation_state="complete_with_errors" if errors else "complete",
            failed_pages=[i + 1 for i in errors],
            page_geometry={str(i): p["geometry"] for i, p in enumerate(packed)},
            table_errors={
                str(i + 1): p["table_error"]
                for i, p in enumerate(packed)
                if p["table_error"]
            },
        ),
        repeated_lines=_repeated_lines(list(texts)),
    )


def prepare_documents(
    context, run_id, pdf_files, pdf_versions, *, checkpoint, cancelled, tracing: Any = None
):
    import pymupdf
    from pdf_text import PDFTextSource
    from analysis_replay import enabled,get,prepared_identity,store_prepared,digest

    record = context.repository.load_run(run_id, include_results=False)
    config = record.get("config") or {}
    documents: dict[str, dict[str, Any]] = dict(config.get("prepared_documents") or {})
    snapshots = {}
    reused_documents = []
    total = 0
    for name in pdf_files:
        checkpoint()
        if cancelled():
            return None
        state: dict[str, Any] = documents.get(name) or {}
        if (
            state.get("version_id") != str(pdf_versions[name].id)
            or state.get("version") != VERSION
        ):
            state = dict(
                version=VERSION, version_id=str(pdf_versions[name].id), pages=[]
            )
        if enabled(context) and not state.get('snapshot'):
            ident=prepared_identity(record,pdf_files[name],pdf_versions[name].id)
            cached=get(context,ident)
            if cached:
                state=dict(cached['state'],reused_artifact=digest(ident))
                reused_documents.append(name)
        if not state.get("page_count"):
            with pymupdf.open(stream=bytes(pdf_files[name]), filetype="pdf") as doc:
                state["page_count"] = len(doc)
        documents[name] = state
        total += int(state["page_count"])
    context.repository.update_run_config(
        run_id,
        {
            "prepared_documents": documents,
            "document_preparation_version": VERSION,
            "documents_prepared": False,
            "pdf_preparation": dict(
                done=sum(len(s.get("pages") or []) for s in documents.values()),
                total=total,
            ),
        },
    )
    for name in pdf_files:
        checkpoint()
        if cancelled():
            return None
        version = str(pdf_versions[name].id)
        state = dict(documents.get(name) or {})
        if state.get("version_id") != version or state.get("version") != VERSION:
            state = dict(version=VERSION, version_id=version, pages=[])
        if state.get("snapshot"):
            index = load_snapshot(context.blob_store, state["snapshot"])
            if (
                index.pending_ocr
                or index.metadata.get("preparation_version") != VERSION
                or not index.metadata.get("preparation_complete")
                or index.metadata.get("document_version_id") != version
            ):
                raise ValueError("Document snapshot is not fully prepared")
            if enabled(context):
                state=store_prepared(context,prepared_identity(record,pdf_files[name],version),state)
                documents[name]=state
            snapshots[name] = state["snapshot"]
            continue
        native = (
            load_json(context.blob_store, state["native"])
            if state.get("native")
            else None
        )
        with pymupdf.open(stream=bytes(pdf_files[name]), filetype="pdf") as doc:
            # Native cache writes can yield safely. Once persisted, resume from
            # native state without re-reading Markdown, including failed pages.
            with closing(
                PDFTextSource(
                    doc,
                    checkpoint=checkpoint if native is None else None,
                    native_state=native,
                    full_document=True,
                )
            ) as source:
                source._checkpoint = None
                if native is None:
                    native = source.native_state()
                    native["document_version_id"] = version
                    state["native"] = store_json(context.blob_store, native)
                state["page_count"] = source.page_count
                pages = list(state.get("pages") or [])

                def commit():
                    documents[name] = dict(state, pages=list(pages))
                    done = sum(len(s.get("pages") or []) for s in documents.values())
                    context.repository.update_run_config(
                        run_id,
                        {
                            "prepared_documents": documents,
                            "document_preparation_version": VERSION,
                            "pdf_preparation": dict(done=done, total=total),
                        },
                    )

                commit()
                checkpoint()
                for page in range(len(pages), source.page_count):
                    if cancelled():
                        return None
                    checkpoint()
                    attributes = nullcontext()
                    client = tracing.client if tracing else None
                    if client is not None:
                        from langfuse import propagate_attributes

                        attributes = propagate_attributes(
                            session_id=tracing.session_id,
                            trace_name="prepare-pdf-page",
                            tags=["document-preparation"],
                        )
                    with (
                        attributes,
                        span(
                            "prepare-pdf-page",
                            client=client,
                            input=dict(
                                pdf_name=name, version_id=version, page=page + 1
                            ),
                        ) as observation,
                    ):
                        prepared = source.prepare_page(page)
                        observation.update(
                            output=dict(
                                text_chars=len(prepared["text"]),
                                tables=len(prepared["tables"]),
                                quality=prepared["quality"],
                                errors=prepared["errors"],
                            ),
                            level="WARNING" if prepared["errors"] else "DEFAULT",
                        )
                    pages.append(store_json(context.blob_store, prepared))
                    commit()
                    checkpoint()
                packed = [load_json(context.blob_store, ref) for ref in pages]
                index = assemble_document(native, packed)
                state["snapshot"] = store_snapshot(context.blob_store, index)
                state["failed_pages"] = list(index.metadata["failed_pages"])
                if enabled(context):
                    state=store_prepared(context,prepared_identity(record,pdf_files[name],version),dict(state,pages=list(pages)))
                    pages=list(state['pages'])
                commit()
                snapshots[name] = state["snapshot"]
    # Every selected PDF has completed before the caller can dispatch positions.
    context.repository.update_run_config(
        run_id,
        {
            "documents_prepared": True,
            "prepared_documents":documents,
            "preparation_reused_documents":reused_documents,
            "normative_corpora": {
                str(pdf_versions[name].id): ref for name, ref in snapshots.items()
            },
        },
    )
    return snapshots
