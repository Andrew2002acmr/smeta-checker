"""Composite evidence and bounded, local crop OCR. No whole-page OCR or LLM calls."""
from __future__ import annotations
import hashlib
import json
import threading
import time

from pdf_selection import _display_rect, _glyph_selection

_OCR_SLOTS = threading.BoundedSemaphore(2)

def geometry(payload):
    return [[f.page, f.rect] for f in payload.fragments]

def crop_png(page, rect):
    import pymupdf
    area = _display_rect(page, rect)
    # Render the selected crop only, with a bounded pixel allocation.
    scale = min(300 / 72, 3000 / max(area.width, area.height), (4000000 / (area.width * area.height)) ** 0.5)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=area, colorspace=pymupdf.csRGB, alpha=False)
    if pix.width < 8 or pix.height < 8:
        raise ValueError("Область слишком мала для распознавания. Выделите ячейку целиком.")
    return pix.tobytes("png")

def _receipt(context, record, payload):
    if not payload.ocr_key:
        return None
    try:
        data = context.blob_store.get(payload.ocr_key)
        if len(data) > 500000:
            raise ValueError("Некорректный результат OCR")
        receipt = json.loads(data)
        if (receipt.get("kind") != "basis-crop-ocr-v1" or receipt.get("run_id") != str(record["id"]) or receipt.get("version_id") != payload.version_id or receipt.get("geometry") != geometry(payload)):
            raise ValueError("Области изменились после OCR. Запустите распознавание заново.")
        return receipt
    except (FileNotFoundError, json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("Результат OCR недоступен. Запустите распознавание заново.") from exc

def preview_fragments(context, record, pdf, payload):
    if not payload.fragments:
        raise ValueError("Добавьте хотя бы одну область")
    receipt = _receipt(context, record, payload)
    items = []
    for index, fragment in enumerate(payload.fragments):
        if fragment.page > len(pdf):
            raise ValueError("Страницы нет в PDF")
        page = pdf[fragment.page - 1]
        box = _display_rect(page, fragment.rect)
        native, _ = _glyph_selection(page, [box * page.derotation_matrix])
        ocr = receipt["items"][index] if receipt else None
        ocr_text = ocr["text"] if ocr else None
        text = fragment.corrected_text if fragment.corrected_text is not None else ocr_text if ocr_text is not None else native
        region = dict(width=page.rect.width, height=page.rect.height, highlights=[dict(bbox=list(box), kind="human", text=text)], reason="Область составного основания")
        items.append(dict(**fragment.model_dump(), extracted_text=native, ocr_text=ocr_text, text=text, region=region, image_key=ocr["image_key"] if ocr else None))
    quote = "\n\n".join(item["text"] for item in items if item["text"].strip())
    if len(quote) > 12000:
        raise ValueError("Слишком много текста. Уменьшите выделение.")
    digest = hashlib.sha256(json.dumps([payload.version_id, items, payload.ocr_key], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return dict(page=items[0]["page"], rect=items[0]["rect"], region=items[0]["region"], quote=quote, mode="multi", fragments=items, ocr_key=payload.ocr_key, selection_digest=digest)

def recognize_fragments(context, record, pdf, payload):
    import ocr_engine
    if payload.mode != "multi" or not payload.fragments:
        raise ValueError("Добавьте области для распознавания")
    if not _OCR_SLOTS.acquire(blocking=False):
        raise RuntimeError("Распознавание занято. Повторите через несколько секунд.")
    try:
        deadline = time.monotonic() + 70
        items = []
        for fragment in payload.fragments:
            if fragment.page > len(pdf):
                raise ValueError("Страницы нет в PDF")
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise RuntimeError("Превышено время OCR. Выберите меньше областей и повторите.")
            png = crop_png(pdf[fragment.page - 1], fragment.rect)
            tsv = ocr_engine.run_tsv(png, language="rus+eng", psm=6, timeout=max(1, min(10, int(remaining))))
            # Keep the engine's literal line breaks and confidence, including uncertain words.
            import csv
            import io
            lines = {}
            for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
                if row.get("level") == "5" and row.get("text", "").strip():
                    key = (row.get("block_num"), row.get("par_num"), row.get("line_num"))
                    lines.setdefault(key, []).append(row["text"].strip())
            text = "\n".join(" ".join(words) for words in lines.values())
            if len(text) > 4000:
                raise ValueError("Слишком много текста в области. Выделите меньший фрагмент.")
            image = context.blob_store.put(png)
            items.append(dict(text=text, image_key=image.key, words=ocr_engine.parse_tsv(tsv)))
        receipt = dict(kind="basis-crop-ocr-v1", run_id=str(record["id"]), version_id=payload.version_id, geometry=geometry(payload), engine="tesseract", engine_version=ocr_engine.tesseract_version(), dpi=300, items=items)
        ref = context.blob_store.put(json.dumps(receipt, ensure_ascii=False).encode())
        # OCR does not overwrite previously typed corrections.
        updated = payload.model_copy(update={"ocr_key": ref.key})
        return preview_fragments(context, record, pdf, updated)
    finally:
        _OCR_SLOTS.release()
