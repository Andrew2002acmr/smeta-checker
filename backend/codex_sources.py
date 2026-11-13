"""Verify literal evidence against the documents pinned to one Codex run."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from codex_contract import Citation, DocumentKind, normalize_text


@dataclass(frozen=True)
class PinnedDocument:
    path: Path
    kind: DocumentKind


class PinnedCitationVerifier:
    """Verify addresses and quotes without asking Codex to judge its own citations.

    The optional OCR callback must read the pinned page locally, independently
    of any text in the agent response. It may return an empty string if the
    source cannot be read reliably.
    """

    def __init__(
        self,
        documents: Mapping[str, PinnedDocument],
        *,
        page_ocr_text: Callable[[PinnedDocument, int], str] | None = None,
        page_cell_texts: Callable[[PinnedDocument, int], Iterable[str]] | None = None,
        verify_page_header: Callable[[PinnedDocument, int, str], bool] | None = None,
    ):
        self.documents = dict(documents)
        self.page_ocr_text = page_ocr_text
        self.page_cell_texts = page_cell_texts
        self.verify_page_header = verify_page_header
        self._pages: dict[tuple[str, int], tuple[str, ...]] = {}
        self._ocr_pages: dict[tuple[str, int], str] = {}
        self._sheets: dict[tuple[str, str], dict[int, tuple[str, ...]]] = {}

    def __call__(self, citation: Citation) -> bool:
        document = self.documents.get(citation.document_id)
        quote = normalize_text(citation.quote)
        if document is None or document.kind != citation.kind or len(quote) < 4:
            return False
        if citation.kind == "vor":
            if citation.sheet is None or citation.row is None:
                return False
            return self._workbook_quote(citation.document_id, document.path, citation.sheet, citation.row, quote)
        if citation.page is None:
            return False
        return self._pdf_quote(citation.document_id, document, citation.page, quote)

    def _pdf_quote(self, document_id: str, document: PinnedDocument, page: int, quote: str) -> bool:
        import pymupdf

        key = (document_id, page)
        if key not in self._pages:
            with pymupdf.open(document.path) as pdf:
                if not 1 <= page <= pdf.page_count:
                    return False
                source = pdf[page - 1]
                extracts = [source.get_text('text', sort=sort) for sort in (False, True)]
                self._pages[key] = tuple(dict.fromkeys(normalize_text(text) for extract in extracts
                    for text in (extract, join_wrapped_designations(extract))))
        if any(_contains_quote(text, quote) for text in self._pages[key]):
            return True
        if self.page_cell_texts is not None and any(
            _contains_quote(normalize_text(text), quote) for text in self.page_cell_texts(document, page)
        ):
            return True
        if self.verify_page_header is not None and self.verify_page_header(document, page, quote):
            return True
        if self.page_ocr_text is None:
            return False
        if key not in self._ocr_pages:
            self._ocr_pages[key] = normalize_text(self.page_ocr_text(document, page))
        return _contains_quote(self._ocr_pages[key], quote)

    def _workbook_quote(self, document_id: str, path: Path, sheet: str, row: int, quote: str) -> bool:
        key = (document_id, sheet)
        if key not in self._sheets:
            self._sheets[key] = _csv_rows(path, sheet) if path.suffix.lower() == ".csv" else _excel_rows(path, sheet)
        return any(_contains_quote(text, quote) for text in self._sheets[key].get(row, ()))


def join_wrapped_designations(text: str) -> str:
    """Join a numeric identifier split after a hyphen at a physical line break."""
    return re.sub(r'(?<!\w)(\d+(?:-\d+)+)-[ \t]*\r?\n[ \t]*(?=\d)', r'\1-', text)


def _contains_quote(text: str, quote: str) -> bool:
    # Prevent a partial word or a suffix of a different number from becoming
    # evidence. Escape the quote since it comes from the generated result.
    left = r"(?<!\w)(?<!\d[.,])" if quote[0].isdigit() else r"(?<!\w)" if quote[0].isalnum() else ""
    right = r"(?!\w)(?![.,]\d)" if quote[-1].isdigit() else r"(?!\w)" if quote[-1].isalnum() else ""
    return re.search(left + re.escape(quote) + right, text) is not None


def csv_records(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read logical records using the same header convention as the VOR parser."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="ignore")
    lines = text.splitlines()
    if not lines:
        return [], []
    header_index = next(
        (
            index for index, line in enumerate(lines)
            if "№ п.п." in line.lower() or "наименование работ" in line.lower()
        ),
        0,
    )
    delimiter = ";" if ";" in lines[header_index] else ","
    records = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    return records[header_index], records[header_index + 1:]


def _csv_rows(path: Path, sheet: str) -> dict[int, tuple[str, ...]]:
    if sheet:
        return {}
    _, records = csv_records(path)
    return {
        index: (_row_text(record),)
        for index, record in enumerate(records, start=1)
    }


def _row_text(values) -> str:
    return normalize_text(" ".join(str(value) for value in values if value is not None))


def _excel_rows(path: Path, sheet: str) -> dict[int, tuple[str, ...]]:
    from openpyxl import load_workbook

    rows: dict[int, tuple[str, ...]] = {}
    # Excel keeps both the formula and its optional cached value. Either is
    # authentic evidence when the quote points to the original physical row.
    for data_only in (False, True):
        with path.open("rb") as stream:
            workbook = load_workbook(stream, read_only=True, data_only=data_only)
            try:
                if sheet not in workbook.sheetnames:
                    return {}
                for index, values in enumerate(workbook[sheet].values, start=1):
                    text = _row_text(values)
                    if text:
                        rows[index] = (*rows.get(index, ()), text)
            finally:
                workbook.close()
    return rows
