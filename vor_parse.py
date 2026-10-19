"""VOR workbook parsing: header metadata, work rows, continuation refs.

Pandas lives only at this import boundary; callers receive validated models.
"""

from __future__ import annotations

import io
import re

import pandas as pd
from openpyxl.utils import get_column_letter

from audit_models import VorMeta, VorPosition
from pdf_binding import split_pdf_refs


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
        if "наименование стройки" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split("  ") if p.strip()]
            if len(parts) > 1:
                meta["object_name"] = parts[-1]
        elif "ведомость объемов работ №" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split("  ") if p.strip()]
            if len(parts) > 1:
                meta["doc_number"] = parts[-1]
        elif "основание" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split("  ") if p.strip()]
            if len(parts) > 1:
                meta["basis_code"] = parts[-1]

    header_idx = -1
    for idx, line in enumerate(lines):
        ll = line.lower()
        if "№ п.п." in ll or "наименование работ" in ll:
            header_idx = idx
            break

    if header_idx == -1:
        header_idx = 0

    # 3. Чтение итоговой таблицы
    if filename.lower().endswith(".csv"):
        delimiter = ";" if ";" in lines[header_idx] else ","
        df = pd.read_csv(
            io.StringIO("\n".join(lines)), skiprows=header_idx, sep=delimiter
        )
    else:
        df = pd.read_excel(io.BytesIO(content), skiprows=header_idx, sheet_name=sheet_name)

    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
    if len(df.columns) < 2:
        df = df.iloc[:0].copy()
        df.attrs["sources"] = []
        return df, meta

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
    df_clean.attrs["sources"] = [
        {"sheet": str(sheet_name) if isinstance(sheet_name, str) else "",
         "row": int(index) + header_idx + 2,
         "columns": list(df.columns)}
        for index in df_clean.index
    ]
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
    frames, sources, first_meta = [], [], None
    with pd.ExcelFile(io.BytesIO(content)) as book:
        for name in book.sheet_names:
            frame, meta = _read_vor_sheet(content, filename, name)
            if first_meta is None:
                first_meta = meta
            if frame.empty:
                continue
            # Validate each nonempty work sheet, rather than silently losing rows.
            _vor_positions(frame)
            sources.extend(frame.attrs.get("sources", []))
            frame.attrs = {}
            frames.append(frame)
    if not frames:
        raise ValueError("В ВОР нет позиций для проверки.")
    combined = pd.concat(frames, ignore_index=True)
    combined.attrs["sources"] = sources
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
    positions = []
    for index, (_, row) in enumerate(df.reset_index(drop=True).iterrows()):
        sources = df.attrs.get("sources", [])
        origin = sources[index] if index < len(sources) else {}
        columns = origin.get("columns", list(df.columns))
        def address(column):
            if column is None or column not in columns or not origin.get("row"):
                return ""
            return f"{get_column_letter(columns.index(column) + 1)}{origin['row']}"
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
        if comment.lower() == "nan":
            comment = ""
        if formula.lower() == "nan":
            formula = ""
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
                sheet=origin.get("sheet", ""),
                source_row=origin.get("row"),
                source_cells={key: address(col) for key, col in {
                    "quantity": vol_col, "calculation": formula_col,
                    "reference": ref_col, "pages": page_col, "file": file_col,
                    "name": name_col, "unit": unit_col,
                }.items()},
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
