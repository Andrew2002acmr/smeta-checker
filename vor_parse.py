"""VOR workbook parsing: header metadata, work rows, continuation refs.

Pandas lives only at this import boundary; callers receive validated models.
Numeric quantity formulas (column D, e.g. ``=ROUND(...)``) are snapshotted
once per load via openpyxl and evaluated by ``vor_formula``; the textual
E-column formula stays in ``VorPosition.formula`` untouched.
"""

from __future__ import annotations

import io
import re

import pandas as pd

from audit_models import VorMeta, VorPosition
from pdf_binding import split_pdf_refs
from vor_formula import calculate_cell, snapshot_cells


def _col_letter(index):
    """1-based column number to Excel letters (no openpyxl dependency)."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _read_vor_frame(file_source, filename):
    """Read raw bytes into (df, meta_dict); pure pandas/io/re boundary."""
    meta = {
        "object_name": "Не указано",
        "doc_number": filename,
        "basis_code": "Проектная документация",
    }

    # 1. Безопасное чтение файла в байты (защита от блокировок и смещения потока)
    if isinstance(file_source, str):
        with open(file_source, "rb") as f:
            content = f.read()
    elif isinstance(file_source, (bytes, bytearray)):
        content = bytes(file_source)
    elif hasattr(file_source, "getvalue"):
        content = file_source.getvalue()
    else:
        file_source.seek(0)
        content = file_source.read()
        file_source.seek(0)

    # 2. Извлечение строк для чтения шапки
    if filename.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="ignore")
        lines = text.splitlines()
    else:
        # Передаем BytesIO, чтобы Excel-файл можно было перечитывать многократно
        df_raw = pd.read_excel(io.BytesIO(content), header=None)
        lines = [
            "\t".join([str(v) for v in row if pd.notna(v)])
            for _, row in df_raw.iterrows()
        ]

    # Чтение метаданных из шапки
    for line in lines[:20]:
        line_clean = line.replace('"', "").replace(";", "  ")
        cells = [part.strip() for part in line.split("\t") if part.strip()]
        value = cells[-1] if len(cells) > 1 else ""
        if "наименование объекта капитального строительства" in line_clean.lower():
            if value:
                meta["object_name"] = value
        elif "наименование стройки" in line_clean.lower():
            if meta["object_name"] == "Не указано" and value:
                meta["object_name"] = value
        elif "ведомость объемов работ №" in line_clean.lower():
            if value:
                meta["doc_number"] = value
        elif "основание" in line_clean.lower():
            if value:
                meta["basis_code"] = value
    header_idx = -1
    for idx, line in enumerate(lines):
        ll = line.lower()
        if "№ п.п." in ll or "наименование работ" in ll:
            header_idx = idx
            break

    if header_idx == -1:
        header_idx = 0

    # 3. Чтение итоговой таблицы
    cells_snapshot = None
    if filename.endswith(".csv"):
        delimiter = ";" if ";" in lines[header_idx] else ","
        df = pd.read_csv(
            io.StringIO("\n".join(lines)), skiprows=header_idx, sep=delimiter
        )
    else:
        df = pd.read_excel(io.BytesIO(content), skiprows=header_idx)
        import openpyxl

        book_formulas = openpyxl.load_workbook(io.BytesIO(content), data_only=False)
        try:
            book_cached = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            try:
                sheet_formulas = book_formulas[book_formulas.sheetnames[0]]
                sheet_cached = book_cached[book_cached.sheetnames[0]]
                cells_snapshot = snapshot_cells(sheet_formulas, sheet_cached)
            finally:
                book_cached.close()
        finally:
            book_formulas.close()


    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]

    def is_work_row(row):
        col_pp = re.sub(r"\.0+$", "", str(row.iloc[0]).strip())
        col_name = str(row.iloc[1]).strip()
        col_type = str(row.get("Тип позиции", "")).strip().lower()

        # 1. Отсекаем техническую строку номеров колонок 1; 2; 3...
        if col_pp == "1" and col_name == "2":
            return False
        # 2. Основные позиции работ
        if col_type in ["работа", "материал", "оборудование"]:
            return True
        if re.match(r"^\d+$", col_pp) and len(col_name) > 3 and not col_name.isdigit():
            return True
        return False

    def is_continuation_row(row):
        # Строка-продолжение: нет номера и имени работы, но есть ссылка на PDF.
        col_pp = str(row.iloc[0]).strip()
        col_name = str(row.iloc[1]).strip()
        if col_pp.lower() in {"", "nan", "-", "none"} and col_name.lower() in {
            "",
            "nan",
            "-",
            "none",
        }:
            row_str = " ".join([str(val) for val in row.values if pd.notna(val)])
            return ".pdf" in row_str.lower()
        return False

    keep = df.apply(is_work_row, axis=1)
    df_clean = df[keep].copy()
    # Присоединяем ссылки строк-продолжений к предыдущей позиции-владельцу:
    # сами строки-продолжения никогда не идут в модель как позиции с nan-именем.
    file_col_guess = next(
        (c for c in df.columns if "наименование файла" in str(c).lower()), None
    )
    page_col_guess = next(
        (
            c
            for c in df.columns
            if any(
                m in str(c).lower().replace("ё", "е")
                for m in ("номер страниц", "номера страниц", "ссылка на страницу")
            )
        ),
        None,
    )
    if file_col_guess is not None or page_col_guess is not None:
        for col in (file_col_guess, page_col_guess):
            if col is not None:
                df_clean[col] = df_clean[col].astype(object)
        owner_idx = None
        for idx, row in df.iterrows():
            if bool(keep.get(idx, False)) and idx in df_clean.index:
                owner_idx = idx
            elif owner_idx is not None and is_continuation_row(row):
                for col in (file_col_guess, page_col_guess):
                    if col is None:
                        continue
                    extra = str(row.get(col, "")).strip()
                    if not extra or extra.lower() == "nan":
                        continue
                    current = str(df_clean.at[owner_idx, col])
                    if current.lower() in {"nan", "", "-", "none"}:
                        df_clean.at[owner_idx, col] = extra
                    elif extra not in current.splitlines():
                        df_clean.at[owner_idx, col] = current + "\n" + extra
            elif pd.notna(row.iloc[0]) or pd.notna(row.iloc[1]):
                owner_idx = None
    # Provenance for numeric quantity formulas: the once-per-load snapshot
    # ({address: formula/cached/plain}) plus the skipped header offset, so a
    # filtered row maps back to its original Excel row. Kept in attrs, never
    # as user-visible columns. CSV loads carry no formula snapshot.
    df_clean.attrs["vor_header_idx"] = header_idx
    if cells_snapshot is not None:
        df_clean.attrs["vor_cells"] = cells_snapshot
    return df_clean, meta


def _vor_positions(df):
    """Validate work rows into VorPosition; pandas objects never escape."""

    def find_column(*markers):
        return next(
            (
                c
                for c in df.columns
                if any(
                    marker in str(c).lower().replace("ё", "е") for marker in markers
                )
            ),
            None,
        )

    name_col = find_column("наименование работ", "наименование")
    vol_col = find_column("объем", "количество")
    unit_col = find_column("ед.", "единица измерения", "единицы измерения")
    ref_col = find_column("ссылка")
    page_col = find_column("номер страниц", "номера страниц", "ссылка на страницу")
    file_col = next(
        (c for c in df.columns if "наименование файла" in str(c).lower()),
        None,
    )
    comment_col = next(
        (
            c
            for c in df.columns
            if "дополнительная" in str(c).lower() or "комментарий" in str(c).lower()
        ),
        None,
    )
    formula_col = next((c for c in df.columns if "формула" in str(c).lower()), None)
    type_col = next(
        (c for c in df.columns if "тип позиции" in str(c).lower()),
        None,
    )
    missing = [
        label
        for label, col in (
            ("наименование работ", name_col),
            ("объем / количество", vol_col),
            ("единица измерения", unit_col),
        )
        if col is None
    ]
    if missing:
        raise ValueError(
            "не найдены обязательные колонки: "
            + ", ".join(missing)
            + ". Проверьте шапку ВОР."
        )
    # Numeric-column provenance: df.attrs carries the once-per-load snapshot
    # ({address: formula/cached/plain}) and the skipped header offset, so a
    # filtered row maps back to its original Excel row even with section
    # headers and continuation rows removed.
    cells = df.attrs.get("vor_cells") or {}
    try:
        header_offset = int(df.attrs.get("vor_header_idx", 0) or 0)
    except (TypeError, ValueError):
        header_offset = 0
    col_letters = {col: _col_letter(idx + 1) for idx, col in enumerate(df.columns)}
    vol_letter = col_letters.get(vol_col, "")
    formula_letter = col_letters.get(formula_col, "") if formula_col else ""
    orig_labels = list(df.index)
    df_reset = df.reset_index(drop=True)
    positions = []
    for index in range(len(df_reset)):
        row = df_reset.iloc[index]

        def cell(col):
            return "" if col is None else row.get(col, "")

        number = re.sub(r"\.0+$", "", str(row.iloc[0]).strip())
        work_name = str(row[name_col]).strip()
        volume = str(row[vol_col]).strip()
        unit = str(row[unit_col]).strip()
        reference = str(cell(ref_col)).strip()
        page_ref = str(cell(page_col)).strip()
        file_ref = str(cell(file_col)).strip()
        comment = str(cell(comment_col)).strip()
        formula = str(cell(formula_col)).strip()
        position_type = str(cell(type_col)).strip()
        if comment.lower() == "nan":
            comment = ""
        if formula.lower() == "nan":
            formula = ""
        if position_type.lower() == "nan":
            position_type = ""
        calculation: dict = {}
        excel_row = 0
        if cells and vol_letter:
            try:
                excel_row = int(orig_labels[index]) + header_offset + 2
            except (TypeError, ValueError):
                excel_row = 0
            address = f"{vol_letter}{excel_row}"
            if excel_row >= 1 and cells.get(address, {}).get("formula"):
                calculation = calculate_cell(address, cells)
        formula_cell = (
            f"{formula_letter}{excel_row}"
            if formula_letter and excel_row >= 1 else "")
        formula_refs: dict = {}
        if formula_cell:
            formula_refs = {"display_cell": formula_cell}
            display = (cells.get(formula_cell) or {}) if cells else {}
            if display.get("formula"):
                formula_refs["display_formula"] = display["formula"]
        # Generated workbooks saved without an Excel engine leave the cached
        # quantity empty (pandas reads NaN or the raw "=..." text): fill the
        # claimed volume only from a successful safe calculation. A present
        # cached value always stays the caller's claim; staleness is exposed
        # via calculation["cached_mismatch"], never by overwriting volume.
        # On failure the raw value is left untouched (previous behaviour).
        if volume.lower() in {"", "nan", "none", "-"} or volume.startswith("="):
            if calculation.get("status") == "calculated" and calculation.get("value"):
                volume = str(calculation["value"])
        positions.append(
            VorPosition(
                index=index,
                number="" if number.lower() == "nan" else number,
                work_name=work_name,
                volume=volume,
                unit=unit,
                reference="" if reference.lower() == "nan" else reference,
                page_ref="" if page_ref.lower() == "nan" else page_ref,
                file_ref="" if file_ref.lower() == "nan" else file_ref,
                explicit_refs=split_pdf_refs(file_ref),
                comment=comment,
                formula=formula,
                formula_refs=formula_refs,
                position_type=position_type,
                calculation=calculation,
                document_id="",
                bind_error=None,
                prep_error=None,
            )
        )
    return positions


def load_vor_frame_positions(file_source, filename):
    """Parse a VOR workbook into (df, positions, meta); df stays at the boundary."""
    df, meta_dict = _read_vor_frame(file_source, filename)
    return df, _vor_positions(df), VorMeta(**meta_dict)


def load_vor_positions(file_source, filename):
    """Parse a VOR workbook into (positions, meta); pandas stays inside."""
    df, positions, meta = load_vor_frame_positions(file_source, filename)
    return positions, meta


def load_vor_with_metadata(file_source, filename):
    """Legacy (df, meta-dict) view; new code uses ``load_vor_positions``."""
    df, meta_dict = _read_vor_frame(file_source, filename)
    return df, VorMeta(**meta_dict)
