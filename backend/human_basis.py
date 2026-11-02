"""Validate and persist a user's choice against immutable source documents."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from workspace_service import inputs_for_run, source_evidence


def position_key(position):
    source = position.position_source
    payload = {
        name: getattr(position, name)
        for name in (
            "work_name",
            "volume",
            "unit",
            "comment",
            "formula",
            "reference",
            "position_type",
        )
    }
    payload.update(
        sheet=source.sheet if source else "",
        row=source.row if source else position.index,
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def locate_position(context, record, index):
    position = next(
        (p for p in inputs_for_run(context, record) if p.index == index), None
    )
    if position is None:
        raise KeyError("Позиция ВОР не найдена")
    return position


def list_basis(context, record, position):
    pins = [UUID(v) for v in (record["config"].get("pdf_versions") or {}).values()]
    rows = context.repository.list_human_basis(
        record["project_id"], record["vor_version_id"], position_key(position), pins
    )
    versions = context.repository.result_versions(record["id"], position.index, limit=1)
    latest = versions[0]["id"] if versions else None
    return [{**row, "reused": row["run_id"] != str(record["id"]), "outdated": bool(row.get("result_version_id") and row["run_id"] == str(record["id"]) and row["result_version_id"] != latest)} for row in rows]


def preview_basis(context, record, payload, *, recognize=False):
    import pymupdf
    from pdf_selection import resolve_selection
    pins = set((record["config"].get("pdf_versions") or {}).values())
    if payload.version_id not in pins:
        raise ValueError("Выберите версию ПД, включённую в эту проверку")
    ref = context.repository.load_version(UUID(payload.version_id))
    if ref.kind != "pdf" or context.repository.version_project_id(ref.id) != record["project_id"]:
        raise ValueError("Документ не относится к проекту")
    with pymupdf.open(stream=context.blob_store.get(ref.blob_key), filetype="pdf") as pdf:
        if payload.page > len(pdf):
            raise ValueError("Страницы нет в PDF")
        if payload.mode == "multi":
            from basis_fragments import preview_fragments, recognize_fragments
            resolved = (recognize_fragments if recognize else preview_fragments)(context, record, pdf, payload)
        elif recognize:
            raise ValueError("Выберите режим «Несколько областей» для OCR")
        else:
            resolved = resolve_selection(pdf[payload.page - 1], payload, payload.version_id)
    return dict(filename=ref.filename, **resolved)


def save_basis(context, record, position, payload, actor, *, evidence_index=None):
    if evidence_index is not None:
        sources = source_evidence(context, record, position.index, position)
        if not 0 <= evidence_index < len(sources):
            raise ValueError("Основание не найдено. Обновите позицию.")
        evidence = sources[evidence_index]
        if not evidence["confirmed"]:
            raise ValueError(
                "Это ссылка для навигации. Выделите область ПД кнопкой «Указать основание»."
            )
        version_id, page_number = evidence["version_id"], evidence["page"]
        quote, region, rect = evidence["quote"], evidence["region"], None
        origin = "confirmed"
    else:
        version_id, page_number = payload.version_id, payload.page
        rect = payload.rect
        origin = "specified"
    pins = set((record["config"].get("pdf_versions") or {}).values())
    if version_id not in pins:
        raise ValueError("Выберите версию ПД, включённую в эту проверку")
    ref = context.repository.load_version(UUID(version_id))
    if (
        ref.kind != "pdf"
        or context.repository.version_project_id(ref.id) != record["project_id"]
    ):
        raise ValueError("Документ не относится к проекту")
    if origin == "specified":
        preview = preview_basis(context, record, payload)
        if payload.selection_digest and payload.selection_digest != preview["selection_digest"]:
            raise ValueError("Выделение изменилось. Обновите предпросмотр перед сохранением.")
        quote, region, rect = preview["quote"], preview["region"], preview["rect"]
    fragments = preview.get("fragments", []) if origin == "specified" else []
    if fragments:
        import pymupdf
        from basis_fragments import crop_png
        with pymupdf.open(stream=context.blob_store.get(ref.blob_key), filetype="pdf") as pdf:
            for fragment in fragments:
                if not fragment["image_key"]:
                    fragment["image_key"] = context.blob_store.put(crop_png(pdf[fragment["page"] - 1], fragment["rect"])).key
    versions = context.repository.result_versions(record["id"], position.index)
    captured = next((r["result"] for r in record["indexed_results"] if r["position_index"] == position.index), None)
    result_version_id = next((v["id"] for v in versions if v["result"] == captured), None)
    data = dict(
        result_version_id=result_version_id,
        fragments=fragments,
        ocr_key=payload.ocr_key if origin == "specified" else None,
        filename=ref.filename,
        page=page_number,
        quote=quote,
        rect=rect,
        region=region,
        origin=origin,
        comment=payload.comment.strip(),
    )
    # Duplicate clicks are idempotent; the original reviewer/time stay intact.
    identity = [str(record["id"]), position.index, version_id, data]
    fingerprint = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    context.repository.save_human_basis(
        project_id=record["project_id"],
        run_id=record["id"],
        position_index=position.index,
        vor_version_id=record["vor_version_id"],
        pdf_version_id=ref.id,
        position_key=position_key(position),
        fingerprint=fingerprint,
        payload=data,
        actor=actor,
    )
    return list_basis(context, record, position)
