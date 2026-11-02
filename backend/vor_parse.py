"""VOR workbook parsing: header metadata, work rows, continuation refs.

Pandas lives only at this import boundary; callers receive validated models.
Numeric quantity formulas (column D, e.g. ``=ROUND(...)``) are snapshotted
once per load via openpyxl and evaluated by ``vor_formula``; the textual
E-column formula stays in ``VorPosition.formula`` untouched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re

import pandas as pd

from audit_models import PositionSource, SourceHeading, VorMeta, VorPosition
from pdf_binding import split_designation_refs, split_pdf_refs
from vor_formula import calculate_cell, snapshot_cells, has_formula_text




def _col_letter(index):
    """1-based column number to Excel letters (no openpyxl dependency)."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters

_SCOPE_SITE_RE = re.compile(
    r"\(\s*(?:площадка|участок|зона)\s+([^()]{1,64})\)", re.IGNORECASE
)


def _clean_text(value):
    """Strip a raw cell to text; NaN/None/placeholder markers become empty."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "-", "nat"}:
        return ""
    return text


def _is_section_row(row):
    """Detect an explicit section boundary without guessing its meaning.

    A section row carries group text in the first or second cell while every
    later cell is empty. Continuation rows never reach here as section
    candidates: their trailing ``.pdf`` reference keeps the tail non-empty.
    """
    name = _clean_text(row.iloc[0]) or _clean_text(row.iloc[1])
    if not name or name.isdigit() or len(name) <= 3:
        return False
    if _clean_text(row.iloc[0]) and _clean_text(row.iloc[1]):
        return False
    for value in row.iloc[2:]:
        if _clean_text(value):
            return False
    return True


def _section_designations(text):
    """Designated site tokens from one heading, e.g. ``(площадка КНС)``."""
    found = []
    for match in _SCOPE_SITE_RE.finditer(text or ""):
        token = re.sub(r"[^A-ZА-ЯЁ0-9]", "", match.group(1).upper())
        if token and token not in found:
            found.append(token)
    return found


def _scope_from_sections(sections):
    """Derive scope only from heading context, never from quantities.

    Returns ``(scope_key, scope_reason)``; an undeterminable site stays
    unknown instead of being guessed from a matching number.
    """
    designations = []
    for _address, text in sections:
        for token in _section_designations(text):
            if token not in designations:
                designations.append(token)
    if len(designations) == 1:
        return designations[0], "section_heading"
    if len(designations) > 1:
        return "", "ambiguous"
    return "", "unknown"


def _read_vor_sheet(file_source, filename, sheet_name=0):
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
    if filename.lower().endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="ignore")
        lines = text.splitlines()
    else:
        # Передаем BytesIO, чтобы Excel-файл можно было перечитывать многократно
        df_raw = pd.read_excel(io.BytesIO(content), header=None, sheet_name=sheet_name)
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
    doc_format = "csv" if filename.lower().endswith(".csv") else "xlsx"
    doc_sha = hashlib.sha256(content).hexdigest()
    resolved_sheet = ""
    record_numbers: dict = {}
    cells_snapshot = None
    if doc_format == "csv":
        delimiter = ";" if ";" in lines[header_idx] else ","
        records = list(
            csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        )
        header_cells = records[header_idx] if len(records) > header_idx else []
        fieldnames = [
            re.sub(r"\s+", " ", str(cell)).strip() or f"col{i + 1}"
            for i, cell in enumerate(header_cells)
        ]
        body = records[header_idx + 1:] if len(records) > header_idx else []
        rows: list = []
        record_no = 0
        data_index = 0
        for record in body:
            cells = [
                re.sub(r"\s+", " ", str(cell)).strip() if cell is not None else ""
                for cell in record
            ]
            # Pad short records; the 1-based logical record number is assigned
            # before any filtering, so blank/service rows never shift the
            # identity of a later work record.
            while len(cells) < len(fieldnames):
                cells.append("")
            record_no += 1
            if all(not cell for cell in cells):
                continue
            lowered = [cell.lower() for cell in cells]
            if lowered and lowered[0] == "1" and len(lowered) > 1 and lowered[1] == "2":
                continue
            record_numbers[data_index] = record_no
            row = {}
            for name, cell in zip(fieldnames, cells[: len(fieldnames)]):
                # Pandas inferred numerics before; preserve its display dtype
                # while keeping logical CSV records (no Excel addresses).
                if re.fullmatch(r"[+-]?\d+", cell or ""):
                    row[name] = int(cell)
                elif re.fullmatch(r"[+-]?\d*\.\d+", cell or ""):
                    row[name] = float(cell)
                else:
                    row[name] = cell
            rows.append(row)
            data_index += 1
        df = pd.DataFrame(rows, columns=fieldnames)
    else:
        df = pd.read_excel(io.BytesIO(content), skiprows=header_idx, sheet_name=sheet_name)
        import openpyxl

        book_formulas = openpyxl.load_workbook(io.BytesIO(content), data_only=False)
        try:
            book_cached = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            try:
                selected = book_formulas.sheetnames[0] if sheet_name == 0 else sheet_name
                sheet_formulas = book_formulas[selected]
                sheet_cached = book_cached[selected]
                resolved_sheet = str(selected)
                cells_snapshot = snapshot_cells(sheet_formulas, sheet_cached)
            finally:
                book_cached.close()
        finally:
            book_formulas.close()


    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
    amount_columns = [column for column in df.columns
                      if "формула" not in str(column).casefold()
                      and any(word in str(column).casefold().replace("ё", "е")
                              for word in ("объем", "количество", "ед.", "единица измерения",
                                           "единицы измерения"))]

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
        if len(col_name) > 3 and col_name.casefold() not in {"nan", "none"} and not col_name.isdigit():
            # A is a display label, not the identity of a work row. Keep a
            # meaningful work with quantity/unit even if A is blank or text.
            for column in amount_columns:
                value = str(row[column]).strip().casefold()
                if value in {"", "nan", "none", "-"}:
                    continue
                label = str(column).casefold()
                if "ед." in label or "единиц" in label:
                    return True
                if value.startswith("=") or re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value):
                    return True
            return False
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
    # Section context: walk source rows in order; an explicit section row
    # assigns context to following positions until the next boundary. A row
    # with an unclear role keeps the current context instead of guessing.
    provenance_sections: dict = {}
    pending_continuations: dict = {}
    continuation_rows_by_owner: dict = {}
    active_sections: list = []
    header_row = header_idx + 1
    source_row_by_label: dict = {}
    if doc_format == "csv":
        for data_label, _row in df.iterrows():
            record_no = int(record_numbers.get(data_label, 0) or 0)
            if record_no >= 1:
                source_row_by_label[data_label] = record_no
    else:
        for data_label, _row in df.iterrows():
            try:
                excel_row = int(data_label) + header_idx + 2
            except (TypeError, ValueError):
                continue
            if excel_row >= 1:
                source_row_by_label[data_label] = excel_row
    for data_label, row in df.iterrows():
        if bool(keep.get(data_label, False)):
            provenance_sections[data_label] = list(active_sections)
            continuation_rows_by_owner[data_label] = list(
                pending_continuations.get(data_label, [])
            )
        elif is_continuation_row(row):
            continue
        elif _is_section_row(row):
            source_row = source_row_by_label.get(data_label, 0)
            first = _clean_text(row.iloc[0])
            second = _clean_text(row.iloc[1])
            text = first or second
            if doc_format == "csv":
                address = f"record:{source_row}" if source_row >= 1 else "record:?"
            elif source_row >= 1:
                column = 1 if first else 2
                address = f"{_col_letter(column)}{source_row}"
            else:
                address = "A?" if first else "B?"
            if text:
                # A new explicit boundary starts a fresh context; nested
                # designated paths accumulate only within one boundary span.
                active_sections = [(address, text)]
        elif pd.notna(row.iloc[0]) or pd.notna(row.iloc[1]):
            active_sections = []
            pending_continuations = {}
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
                cont_row = source_row_by_label.get(idx, 0)
                if cont_row >= 1:
                    pending_continuations.setdefault(owner_idx, [])
                    if cont_row not in pending_continuations[owner_idx]:
                        pending_continuations[owner_idx].append(cont_row)
                    continuation_rows_by_owner.setdefault(owner_idx, [])
                    if cont_row not in continuation_rows_by_owner[owner_idx]:
                        continuation_rows_by_owner[owner_idx].append(cont_row)
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
            elif _is_section_row(row):
                owner_idx = None
            elif pd.notna(row.iloc[0]) or pd.notna(row.iloc[1]):
                owner_idx = None
    # Provenance for numeric quantity formulas: the once-per-load snapshot
    # ({address: formula/cached/plain}) plus the skipped header offset, so a
    # filtered row maps back to its original Excel row. Kept in attrs, never
    # as user-visible columns. CSV loads carry no formula snapshot.
    df_clean.attrs["vor_header_idx"] = header_idx
    df_clean.attrs["vor_doc_sha"] = doc_sha
    df_clean.attrs["vor_doc_format"] = doc_format
    df_clean.attrs["vor_sheet"] = resolved_sheet
    df_clean.attrs["vor_header_row"] = header_row
    df_clean.attrs["vor_record_numbers"] = dict(record_numbers)
    df_clean.attrs["vor_source_rows"] = dict(source_row_by_label)
    df_clean.attrs["vor_sections"] = {
        key: list(value) for key, value in provenance_sections.items()
    }
    df_clean.attrs["vor_continuations"] = {
        key: list(value) for key, value in continuation_rows_by_owner.items()
    }
    if cells_snapshot is not None:
        df_clean.attrs["vor_cells"] = cells_snapshot
    return df_clean, meta


def _read_vor_frame(file_source, filename):
    """Read every VOR sheet; retain physical rows before filtering/resetting."""
    if filename.lower().endswith(".csv"):
        return _read_vor_sheet(file_source, filename)
    if isinstance(file_source, str):
        content = open(file_source, "rb").read()
    elif isinstance(file_source, (bytes, bytearray)):
        content = bytes(file_source)
    elif hasattr(file_source, "getvalue"):
        content = file_source.getvalue()
    else:
        previous = file_source.tell()
        file_source.seek(0)
        content = file_source.read()
        file_source.seek(previous)
    frames, first_meta = [], None
    with pd.ExcelFile(io.BytesIO(content)) as book:
        for name in book.sheet_names:
            frame, meta = _read_vor_sheet(content, filename, name)
            if first_meta is None:
                first_meta = meta
            if frame.empty:
                continue
            frames.append(frame)
    if not frames:
        # Single-sheet fallback preserves the historical error for workbooks
        # without any work rows (e.g. a cover-only first sheet).
        frame, meta = _read_vor_sheet(content, filename, 0)
        if frame.empty:
            raise ValueError("В ВОР нет позиций для проверки.")
        return frame, meta
    if len(frames) == 1:
        return frames[0], first_meta
    combined = pd.concat(frames, ignore_index=True)
    # Per-sheet provenance (formulas, offsets, sections) keyed by sheet so
    # positions resolve cells against their own worksheet, not the first one.
    combined.attrs["vor_sheets"] = {
        str(frame.attrs.get("vor_sheet", "")): {
            key: frame.attrs.get(key)
            for key in (
                "vor_header_idx", "vor_header_row", "vor_cells",
                "vor_source_rows", "vor_sections", "vor_continuations",
            )
        }
        for frame in frames
    }
    combined.attrs["vor_sheet_names"] = [
        str(frame.attrs.get("vor_sheet", "")) for frame in frames
    ]
    combined.attrs["vor_sheet_rows"] = [len(frame) for frame in frames]
    combined.attrs["vor_sheet_labels"] = [list(frame.index) for frame in frames]
    for key in (
        "vor_header_idx", "vor_doc_sha", "vor_doc_format", "vor_sheet",
        "vor_header_row", "vor_record_numbers", "vor_source_rows",
        "vor_sections", "vor_continuations", "vor_cells",
    ):
        combined.attrs[key] = frames[0].attrs.get(key)
    return combined, first_meta


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
    # Numeric-column + source-identity provenance: df.attrs carries the
    # once-per-load snapshot ({address: formula/cached/plain}), the skipped
    # header offset, and the section/continuation/record maps, so a filtered
    # row maps back to its original source row even with section headers
    # and continuation rows removed.
    cells = df.attrs.get("vor_cells") or {}
    try:
        header_offset = int(df.attrs.get("vor_header_idx", 0) or 0)
    except (TypeError, ValueError):
        header_offset = 0
    doc_sha = str(df.attrs.get("vor_doc_sha", "") or "")
    doc_format = str(df.attrs.get("vor_doc_format", "") or "")
    if doc_format not in {"xlsx", "csv"}:
        doc_format = ""
    sheet = str(df.attrs.get("vor_sheet", "") or "")
    try:
        header_row = int(df.attrs.get("vor_header_row", 0) or 0)
    except (TypeError, ValueError):
        header_row = 0
    if header_row < 1:
        header_row = header_offset + 1
    source_rows = df.attrs.get("vor_source_rows") or {}
    sections_by_label = df.attrs.get("vor_sections") or {}
    continuations_by_label = df.attrs.get("vor_continuations") or {}
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
        if not has_formula_text(formula):
            formula = ""
        if position_type.lower() == "nan":
            position_type = ""
        label = orig_labels[index]
        calculation: dict = {}
        excel_row = 0
        if cells and vol_letter:
            try:
                excel_row = int(label) + header_offset + 2
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
        try:
            source_row = int(source_rows.get(label, 0) or 0)
        except (TypeError, ValueError):
            source_row = 0
        if doc_format == "xlsx" and source_row < 1 and excel_row >= 1:
            try:
                source_row = int(excel_row)
            except (TypeError, ValueError):
                source_row = 0
        position_source = None
        if doc_sha and doc_format in {"xlsx", "csv"} and source_row >= 1:
            raw_sections = sections_by_label.get(label, []) or []
            section_entries = []
            for entry in raw_sections:
                try:
                    entry_address, entry_text = entry
                except (TypeError, ValueError):
                    continue
                if str(entry_text or ""):
                    section_entries.append(
                        (str(entry_address or ""), str(entry_text))
                    )
            scope_key, scope_reason = _scope_from_sections(section_entries)
            tracked_cols = [
                name_col, vol_col, unit_col, ref_col, page_col, file_col,
                comment_col, formula_col, type_col,
            ]
            field_cells: dict = {}
            num_text = _clean_text(row.iloc[0])
            if doc_format == "xlsx":
                if num_text:
                    field_cells[f"{_col_letter(1)}{source_row}"] = num_text
                for col in tracked_cols:
                    if col is None:
                        continue
                    letter = col_letters.get(col, "")
                    if not letter:
                        continue
                    value = _clean_text(row.get(col, ""))
                    if not value:
                        # pandas renders a stored-but-uncached ``=...``
                        # quantity as NaN; the snapshot still identifies the
                        # source cell and its formula text.
                        snapshot = cells.get(f"{letter}{source_row}") or {}
                        value = str(snapshot.get("formula") or "")
                    if value:
                        field_cells[f"{letter}{source_row}"] = value
            else:
                if num_text:
                    field_cells[str(df.columns[0])] = num_text
                for col in tracked_cols:
                    if col is None:
                        continue
                    value = _clean_text(row.get(col, ""))
                    if value:
                        field_cells[str(col)] = value
            cont_rows = []
            for item in continuations_by_label.get(label, []) or []:
                try:
                    cont_row = int(item)
                except (TypeError, ValueError):
                    continue
                if cont_row >= 1 and cont_row not in cont_rows:
                    cont_rows.append(cont_row)
            digest = doc_sha[:16]
            if doc_format == "xlsx":
                position_id = f"{digest}:{sheet}:{source_row}"
            else:
                position_id = f"{digest}:csv:record:{source_row}"
            position_source = PositionSource(
                document_sha256=doc_sha,
                format=doc_format,
                sheet=sheet if doc_format == "xlsx" else "",
                row=source_row,
                header_row=header_row,
                position_id=position_id,
                cells=field_cells,
                continuation_rows=cont_rows,
                sections=[
                    SourceHeading(address=address, text=text)
                    for address, text in section_entries
                ],
                scope_key=scope_key,
                scope_reason=scope_reason,
            )
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
                designation_refs=split_designation_refs(reference),
                comment=comment,
                formula=formula,
                formula_refs=formula_refs,
                position_type=position_type,
                calculation=calculation,
                position_source=position_source,
                document_id="",
                bind_error=None,
                prep_error=None,
            )
        )
    return positions


def load_vor_frame_positions(file_source, filename):
    """Parse a VOR workbook into (df, positions, meta); df stays at the boundary."""
    df, meta_dict = _read_vor_frame(file_source, filename)
    sheets = df.attrs.get("vor_sheets")
    names = df.attrs.get("vor_sheet_names") or []
    counts = df.attrs.get("vor_sheet_rows") or []
    if isinstance(sheets, dict) and names and len(names) == len(counts) and len(names) > 1:
        # Multi-sheet workbook: each slice keeps its own worksheet, header
        # offset, formula snapshot, and provenance maps; indexes are global.
        labels = df.attrs.get("vor_sheet_labels") or []
        positions, start = [], 0
        for ordinal, (name, count) in enumerate(zip(names, counts)):
            bundle = sheets.get(name) or {}
            part = df.iloc[start:start + count].copy()
            if ordinal < len(labels) and len(labels[ordinal]) == len(part):
                part.index = labels[ordinal]
            for key, value in bundle.items():
                part.attrs[key] = value
            part.attrs["vor_sheet"] = name
            for position in _vor_positions(part):
                position.index = len(positions)
                positions.append(position)
            start += count
        return df, positions, VorMeta(**meta_dict)
    return df, _vor_positions(df), VorMeta(**meta_dict)


def load_vor_positions(file_source, filename):
    """Parse a VOR workbook into (positions, meta); pandas stays inside."""
    df, positions, meta = load_vor_frame_positions(file_source, filename)
    return positions, meta


def load_vor_with_metadata(file_source, filename):
    """Legacy (df, meta-dict) view; new code uses ``load_vor_positions``."""
    df, meta_dict = _read_vor_frame(file_source, filename)
    return df, VorMeta(**meta_dict)
