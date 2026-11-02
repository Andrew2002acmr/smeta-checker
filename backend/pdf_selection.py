"""Exact physical selection: table rows, glyph centres and disjoint text ranges."""

from __future__ import annotations

import hashlib
import json


def _display_rect(page, rect):
    import pymupdf

    return pymupdf.Rect(
        rect[0] * page.rect.width,
        rect[1] * page.rect.height,
        rect[2] * page.rect.width,
        rect[3] * page.rect.height,
    )


def _normalized(page, rect):
    return [
        rect.x0 / page.rect.width,
        rect.y0 / page.rect.height,
        rect.x1 / page.rect.width,
        rect.y1 / page.rect.height,
    ]


def _glyph_selection(page, areas):
    """A neighbouring glyph's ascender intersecting the frame is insufficient."""
    import pymupdf

    texts, boxes = [], []
    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            characters = []
            run_box = None
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    box = pymupdf.Rect(char["bbox"])
                    x, y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
                    selected = any(
                        area.x0 <= x < area.x1 and area.y0 <= y < area.y1
                        for area in areas
                    )
                    if selected:
                        characters.append(char["c"])
                        run_box = (run_box | box) if run_box is not None else box
                    elif run_box is not None:
                        boxes.append(run_box)
                        run_box = None
                        characters.append(" ")
            if run_box is not None:
                boxes.append(run_box)
            text = "".join(characters).strip()
            if text:
                texts.append(text)
    return "\n".join(texts), boxes


def _table_row(page, display):
    import pymupdf
    from pdf_tables import native_tables

    if len(page.get_drawings()) > 5000:
        raise ValueError("Сложная страница. Выберите режим «Область» или «Текст».")
    point = (
        pymupdf.Point((display.x0 + display.x1) / 2, (display.y0 + display.y1) / 2)
        * page.derotation_matrix
    )
    candidates = []
    for table in native_tables(page, lambda p, box: p.get_textbox(box)):
        if table.get("quality") != "usable":
            continue
        for cell in table["cells"]:
            box = pymupdf.Rect(cell["bbox"])
            if cell.get("role") == "cell" and box.contains(point):
                candidates.append((box.get_area(), table, cell["row"]))
    if not candidates:
        raise ValueError(
            "В этом месте строка таблицы не определена. Выберите режим «Область» или «Текст»."
        )
    if len({(item[1]["table_id"], item[2]) for item in candidates}) > 1:
        raise ValueError("Клик попал на границу строк. Нажмите внутри нужной строки.")
    _, table, row = min(candidates, key=lambda item: item[0])
    cells = sorted(
        (c for c in table["cells"] if c["row"] == row and c.get("role") == "cell"),
        key=lambda cell: cell["column"],
    )
    boxes = [pymupdf.Rect(cell["bbox"]) for cell in cells]
    quote = "\n".join(
        " ".join(cell["text"].split()) for cell in cells if cell["text"].strip()
    )
    return quote, boxes


def resolve_selection(page, payload, version_id):
    import pymupdf

    display = _display_rect(page, payload.rect)
    if payload.mode == "row":
        quote, native_boxes = _table_row(page, display)
        boxes = [box * page.rotation_matrix for box in native_boxes]
    else:
        if payload.mode == "text" and not payload.rects:
            raise ValueError(
                "Выделите текст мышью: нужны точные фрагменты выделения, а не общая рамка."
            )
        ranges = payload.rects if payload.mode == "text" else [payload.rect]
        areas = [_display_rect(page, rect) * page.derotation_matrix for rect in ranges]
        quote, native_boxes = _glyph_selection(page, areas)
        boxes = [box * page.rotation_matrix for box in native_boxes]
    if len(quote) > 12000:
        raise ValueError("Выбрано слишком много текста. Выделите меньший фрагмент.")
    if not boxes or not quote.strip():
        if payload.mode == "text":
            raise ValueError(
                "Текст в выделенных фрагментах не найден. Для скана используйте «Область»."
            )
        boxes = [display]
    union = pymupdf.Rect(boxes[0])
    for box in boxes[1:]:
        union |= box
    rect = _normalized(page, union)
    region = dict(
        width=page.rect.width,
        height=page.rect.height,
        highlights=[dict(bbox=list(box), kind="human", text=quote) for box in boxes],
        reason="Точные границы выбранной строки"
        if payload.mode == "row"
        else "Точные фрагменты выделения",
    )
    digest = hashlib.sha256(
        json.dumps(
            [version_id, payload.page, quote, rect, region, payload.mode],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return dict(
        page=payload.page,
        quote=quote,
        rect=rect,
        region=region,
        selection_digest=digest,
        mode=payload.mode,
    )
