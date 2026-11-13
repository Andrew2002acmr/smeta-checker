"""Serve the pinned audit inputs over the local Codex MCP stdio transport."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic


def schema(description, *, optional=(), **properties):
    return {"description": description, "annotations": {"readOnlyHint": True, "openWorldHint": False}, "inputSchema": {
        "type": "object", "properties": properties,
        "required": [name for name in properties if name not in optional],
        "additionalProperties": False,
    }}


DOCUMENT = {"type": "string", "description": "Exact document_id from the manifest"}
PAGE = {"type": "integer", "minimum": 1, "description": "Physical page, starting at 1"}
TEXT = {"type": "string"}
PDF_DIGEST = re.compile(r'[a-f0-9]{64}')
REGION = {"type": "array", "minItems": 4, "maxItems": 4,
          "items": {"type": "number", "minimum": 0, "maximum": 1}}
TARGETED_TOOLS = {"table_structure", "read_table_cell", "read_text_region", "ocr_region", "read_native_cell",
                  "find_project_total", "find_project_notes"}
TOOLS = {
    "manifest": schema("List pinned original documents and expected VOR row identities"),
    "calculate": schema("Evaluate decimal arithmetic and optionally subtract a VOR value; returns exact decimal strings",
                        expression=TEXT, vor_value={"type": ["string", "null"]}),
    "calculate_from_project_cell": schema("Recalculate using a verified PDF numeric cell. Use BASE once in the expression.",
                                          evidence_id=TEXT, expression=TEXT, vor_value=TEXT),
    "normative_catalog": schema("Find pinned normative PDFs by designation or name without reading every document",
                                query=TEXT),
    "workbook_rows": schema("Read Excel rows and formulas, or CSV records with sheet set to an empty string",
                            document_id=DOCUMENT, sheet=TEXT, first_row=PAGE, last_row=PAGE,
                            optional=("columns",), columns={"type": "array", "items": TEXT,
                                "minItems": 1, "maxItems": 24}),
    "search_pdf": schema("Search literal text across an original PDF",
                         document_id=DOCUMENT, query=TEXT, optional=("first_page", "last_page"),
                         first_page=PAGE, last_page=PAGE),
    "search_pdf_terms": schema("Search several literal phrases in one pass through a pinned PDF range",
                                document_id=DOCUMENT, first_page=PAGE, last_page=PAGE,
                                queries={"type": "array", "items": TEXT, "minItems": 1, "maxItems": 8}),
    "page_inventory": schema("Locate PDF appendices and image-only pages by physical page number; scan at most 100 pages without OCR",
                             document_id=DOCUMENT, first_page=PAGE, last_page=PAGE),
    "scan_table_titles": schema("Read the upper part of at most 12 consecutive PDF pages to locate raster table titles; observations are search hints",
                                document_id=DOCUMENT, first_page=PAGE, last_page=PAGE),
    "read_page": schema("Read text from an original PDF page", document_id=DOCUMENT, page=PAGE),
    "read_text_region": schema("Read literal text within a PDF region and return a linkable observation when unique",
                               document_id=DOCUMENT, page=PAGE, query=TEXT),
    "ocr_region": schema("Recognize a short drawing label from a tight crop and return an address only when two OCR readings agree",
                         document_id=DOCUMENT, page=PAGE, bbox=REGION),
    "read_tables": schema("Read text-layer PDF tables, as arrays of rows; use read_native_cell for a link to a cell",
                          document_id=DOCUMENT, page=PAGE),
    "read_native_cell": schema("Read and locate a cell from read_tables; table, row and column are 1-based",
                               document_id=DOCUMENT, page=PAGE, table=PAGE, row=PAGE, column=PAGE),
    "table_structure": schema("List OCR row labels and grouped column_headers for navigation and read_table_cell. "
                              "Header evidence IDs are provisional; their full literal text must be cited and is reread by the server.",
                              document_id=DOCUMENT, page=PAGE),
    "find_project_total": schema("Find and verify a total. Put site words from the table title in scope, "
                                 "and the local structure and leaf header keywords in column. Never supply the VOR value.",
                                 document_id=DOCUMENT, page=PAGE, scope=TEXT, column=TEXT,
                                 unit={"type": "string", "enum": ["м", "м2", "м3"]}),
    "find_project_notes": schema("Search compact ruled notes on a relevant PDF sheet. Returns adjacent cells with their own links.",
                                 document_id=DOCUMENT, page=PAGE, query=TEXT),
    "read_table_cell": schema("Read a scanned numeric cell by its exact row label and column headers from table_structure",
                             document_id=DOCUMENT, page=PAGE, row=TEXT, optional=("row_bbox",),
                             row_bbox={"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                             headers={"type": "array", "items": TEXT, "minItems": 1, "maxItems": 8}),
    "view_page": schema("Render an original PDF page as an image", document_id=DOCUMENT, page=PAGE),
    "view_region": schema("Render a normalized rectangle [left, top, right, bottom] at higher resolution from an original PDF page",
                          document_id=DOCUMENT, page=PAGE, bbox=REGION),
    "ocr_page": schema("Recognize text on an original PDF page", document_id=DOCUMENT, page=PAGE),
}


def text(value):
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str
    )}


class Documents:
    def __init__(self, root: Path, cache_root: Path | None = None) -> None:
        self.root = root
        self.cache_root = cache_root if cache_root is not None else root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.documents = {item["id"]: item for item in self.manifest["documents"]}
        self.workbooks = {}

    def call(self, tool, args):
        if tool == "manifest":
            return text(self.manifest)
        if tool == "normative_catalog":
            query = args['query'].strip().casefold()
            if not 1 <= len(query) <= 200:
                raise ValueError('Invalid normative catalog query')
            terms = re.findall(r'[a-zа-яё]+|\d+', query)
            if not terms:
                return text({'matches': []})
            matches = []
            for document in self.documents.values():
                if document['kind'] != 'normative':
                    continue
                searchable = f"{document['filename']} {document.get('title') or ''}"
                keywords = re.findall(r'[a-zа-яё]+|\d+', searchable.casefold())
                if all(term in keywords for term in terms):
                    matches.append(document)
            return text({'matches': [{key: document[key] for key in ('id', 'filename', 'title', 'page_count') if key in document}
                                     for document in matches]})
        if tool == "calculate_from_project_cell":
            from codex_contract import _decimal

            key = args['evidence_id']
            expression = args['expression']
            if not re.fullmatch(r'[a-f0-9]{64}', key) or len(re.findall(r'\bBASE\b', expression)) != 1:
                raise ValueError('Expected one BASE operand and a local evidence ID')
            observation = json.loads((self.root / 'codex-evidence' / f'{key}.json').read_bytes())
            if (hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest() != key
                    or observation['role'] not in {'cell', 'native_cell'}
                    or self.documents[observation['document_id']]['kind'] != 'project'
                    or (value := _decimal(observation['quote'])) is None):
                raise ValueError('Expected an addressed numeric project cell')
            expression = re.sub(r'\bBASE\b', str(value), expression)
            result = json.loads(self.call('calculate', {'expression': expression,
                                                        'vor_value': args['vor_value']})['text'])
            return text({**result, 'expression': expression, 'source': observation, 'evidence_id': key})
        if tool == "calculate":
            from decimal import DecimalException, localcontext
            from codex_contract import _arithmetic, _decimal

            expression = args['expression']
            vor = args['vor_value']
            if not isinstance(expression, str) or not 1 <= len(expression) <= 400:
                raise ValueError('Invalid calculation expression')
            if vor is not None and (not isinstance(vor, str) or _decimal(vor) is None):
                raise ValueError('Invalid VOR value')
            try:
                with localcontext() as context:
                    context.prec = 50
                    result = _arithmetic(expression)
                    deviation = None if vor is None else str(result - _decimal(vor))
            except (SyntaxError, DecimalException, OverflowError, RecursionError) as error:
                raise ValueError('Invalid decimal arithmetic') from error
            return text({"result": str(result), "deviation": deviation})
        document = self.documents[args["document_id"]]
        path = (self.root / document["path"]).resolve(strict=True)
        if not path.is_relative_to(self.root / "inputs"):
            raise ValueError("Invalid document path")
        if tool == "workbook_rows" and document["kind"] == "vor":
            return text(self.rows(path, args, opened=self.workbooks))
        if tool in TARGETED_TOOLS and document["kind"] in {"project", "normative"}:
            return self.targeted_pdf(path, document['sha256'], tool, args)
        if tool not in {"search_pdf", "search_pdf_terms", "page_inventory", "scan_table_titles", "read_page",
                        "read_tables", "view_page", "view_region", "ocr_page"}:
            raise ValueError("Unsupported operation")
        return self.pdf(path, tool, args, cache_dir=self.cache_root / 'pdf-navigation-cache',
                        digest=document['sha256'])

    def targeted_pdf(self, path, digest, tool, args):
        import pymupdf

        with pymupdf.open(path) as pdf:
            index = args['page']
            if type(index) is not int or not 1 <= index <= len(pdf):
                raise ValueError('Invalid PDF page')
            options = dict(pdf_sha256=digest, cache_dir=self.cache_root / 'targeted-table-cache')
            page = pdf[index-1]

            def display_rectangle(bbox):
                display = pymupdf.Rect(bbox) * page.rotation_matrix
                return [value / size for value, size in zip(display,
                    (page.rect.width, page.rect.height) * 2, strict=True)]

            def address(item, quote, role):
                bbox = item['bbox']
                entry = dict(document_id=args['document_id'], page=index, quote=quote,
                    bbox=bbox, highlight=display_rectangle(bbox), role=role,
                    row=item.get('row'), headers=item.get('headers'))
                if item.get('scope_binding') is not None:
                    entry['scope_binding'] = item['scope_binding']
                key = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
                directory = self.root / 'codex-evidence'
                directory.mkdir(exist_ok=True)
                # Each call has its own temporary file; concurrent workers may return the same observation.
                with NamedTemporaryFile(dir=directory, delete=False) as stream:
                    stream.write(json.dumps(entry, ensure_ascii=False).encode())
                    temporary = Path(stream.name)
                temporary.replace(directory / f'{key}.json')
                item['evidence_id'] = key

            if tool == 'ocr_region':
                rect = self.region(page.rect, args['bbox'])
                if rect.width > page.rect.width * .6 or rect.height > page.rect.height * .2:
                    raise ValueError('Select a tight rectangle around one drawing label')
                quote = self.recognize_region(page, rect, corroborate=False)
                if quote is None or quote != self.recognize_region(page, rect, corroborate=True):
                    return text({'page': index, 'status': 'unreadable'})
                item = {'page': index, 'status': 'read', 'quote': quote,
                        'bbox': list(rect * page.derotation_matrix), 'view_bbox': args['bbox']}
                address(item, quote, 'drawing_label')
                return text(item)
            if tool == 'read_text_region':
                quote = ' '.join(args['query'].split())
                if not 4 <= len(quote) <= 400:
                    raise ValueError('Invalid PDF quote')
                matches = page.search_for(quote)
                if len(matches) != 1:
                    return text({'page': index, 'status': 'ambiguous' if matches else 'not_found',
                                 'matches': len(matches)})
                item = {'page': index, 'status': 'read', 'bbox': list(matches[0]), 'quote': quote}
                address(item, quote, 'text')
                return text(item)
            if tool == 'read_native_cell':
                reading = self.native_cell(page, args['table'], args['row'], args['column'])
                if reading is None:
                    return text({'page': index, 'status': 'not_found'})
                item = {'page': index, 'status': 'read', **reading}
                address(item, item['quote'], 'native_cell')
                return text(item)
            from pdf_targeted_cells import describe, find_notes, find_totals, read_cell

            if tool == 'find_project_notes':
                result = find_notes(page, query=args['query'], **options)
                for row in result['matches']:
                    for item in row:
                        if item['verified']:
                            address(item, item['text'], 'table_note')
                        item['view_bbox'] = display_rectangle(item['bbox'])
                return text(result)

            if tool == 'table_structure':
                result = describe(page, **options)
                for table in result['tables']:
                    table['view_bbox'] = display_rectangle(table['table_bbox'])
                    for item in table['headers'] + table['rows']:
                        item['view_bbox'] = display_rectangle(item['bbox'])
                    for header in table['headers']:
                        address(header, header['text'], 'header')
                    table['headers'] = [{key: item[key] for key in ('text', 'view_bbox', 'evidence_id')}
                                        for item in table['headers']]
                    table['rows'] = [{key: item[key] for key in ('text', 'bbox', 'view_bbox')}
                                     for item in table['rows']]
                return text(result)
            if tool == 'find_project_total':
                result = find_totals(page, scope=args['scope'], column=args['column'], unit=args['unit'], **options)
            else:
                result = read_cell(page, row=args['row'], headers=args['headers'],
                                   row_bbox=args.get('row_bbox'), **options)
            if 'bbox' in result:
                result['view_bbox'] = display_rectangle(result['bbox'])
            for candidate in result.get('candidates', []):
                candidate['view_bbox'] = display_rectangle(candidate['bbox'])
            if result['status'] == 'read':
                for header in result['headers']:
                    header['view_bbox'] = display_rectangle(header['bbox'])
                address(result, result['reading']['text'], 'cell')
                result['reading'] = {'text': result['reading']['text']}
                result['headers'] = [{key: item[key] for key in ('text', 'view_bbox')} for item in result['headers']]
            return text(result)

    @staticmethod
    def native_tables(page):
        from pymupdf.table import find_tables

        rotation = page.rotation
        try:
            if rotation:
                page.set_rotation(0)
            return [(table.extract(), [row.cells for row in table.rows])
                    for table in find_tables(page, strategy='lines_strict', use_layout=False).tables]
        finally:
            if rotation:
                page.set_rotation(rotation)

    @staticmethod
    def native_cell(page, table, row, column):
        if any(type(index) is not int or index < 1 for index in (table, row, column)):
            raise ValueError('Invalid table address')
        tables = Documents.native_tables(page)
        if table > len(tables):
            return None
        values, boxes = tables[table - 1]
        if row > len(values) or column > len(values[row - 1]):
            return None
        bbox = boxes[row - 1][column - 1]
        value = values[row - 1][column - 1]
        quote = ' '.join(value.split()) if value else ''
        if bbox is None or not 1 <= len(quote) <= 500:
            return None
        return {'bbox': list(bbox), 'quote': quote,
                'row': {'table': table, 'row': row, 'column': column}}

    @staticmethod
    def region(rect, bbox):
        import pymupdf

        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError('Invalid PDF region')
        return pymupdf.Rect(rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                            rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height)

    @staticmethod
    def recognize_region(page, rect, *, corroborate):
        import pymupdf
        from PIL import Image, ImageOps

        from ocr_engine import _to_png, parse_tsv, run_tsv

        if rect.is_empty or not page.rect.contains(rect):
            return None
        resolution, mode = (500, 6) if corroborate else (350, 7)
        scale = min(resolution / 72, 2000 / max(rect.width, rect.height))
        raster = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
        bordered = ImageOps.expand(Image.frombytes('RGB', (raster.width, raster.height), raster.samples),
                                   border=14, fill='white')
        try:
            words = parse_tsv(run_tsv(_to_png(bordered), language='rus+eng', psm=mode, timeout=5))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            return None
        if not words or min(word['conf'] for word in words) < 80:
            return None
        quote = ' '.join(word['text'] for word in words)
        return quote if 4 <= len(quote) <= 250 else None

    @staticmethod
    def rows(path, args, *, opened=None):
        first, last = args["first_row"], args["last_row"]
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= first + 199:
            raise ValueError("Invalid row interval")
        from openpyxl import load_workbook
        from openpyxl.utils.cell import column_index_from_string

        columns = args.get('columns')
        selected = None
        if columns is not None:
            if not isinstance(columns, list) or not 1 <= len(columns) <= 24 or any(
                not isinstance(column, str) or not re.fullmatch(r'[A-Z]{1,3}', column) for column in columns
            ):
                raise ValueError('Invalid workbook columns')
            selected = {column: column_index_from_string(column) - 1 for column in columns}

        if path.suffix == '.csv':
            from codex_sources import csv_records

            if args['sheet'] or selected:
                raise ValueError('For CSV, pass an empty sheet and no Excel columns')
            header, records = csv_records(path)
            return {'header': header, 'records': [{'row': index, 'cells': values}
                    for index, values in enumerate(records[first - 1:last], first)]}

        book = opened.get(path) if opened is not None else None
        if book is None:
            book = load_workbook(path, read_only=True, data_only=False)
            if opened is not None:
                opened[path] = book
        try:
            sheet = book[args["sheet"]]
            if selected:
                min_index = min(selected.values())
                rows = sheet.iter_rows(min_row=first, max_row=last, min_col=min_index + 1,
                                       max_col=max(selected.values()) + 1, values_only=True)
                return [{"row": row, "cells": {column: cells[index - min_index] for column, index in selected.items()}}
                        for row, cells in enumerate(rows, first)]
            rows = sheet.iter_rows(min_row=first, max_row=last, values_only=True)
            return [{"row": row, "cells": cells} for row, cells in enumerate(rows, first)]
        finally:
            if opened is None:
                book.close()

    @staticmethod
    def pdf(path, tool, args, *, cache_dir=None, digest=None):
        import pymupdf

        with pymupdf.open(path) as pdf:
            if tool == "page_inventory":
                first, last = args["first_page"], args["last_page"]
                if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(pdf) or last - first >= 100:
                    raise ValueError("Invalid PDF inventory interval")
                return text({"page_count": len(pdf), "landscape_text_checked": False, "pages": [
                    Documents.page_summary(pdf[index - 1]) for index in range(first, last + 1)
                ]})
            if tool == "scan_table_titles":
                first, last = args["first_page"], args["last_page"]
                if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(pdf) or last - first >= 12:
                    raise ValueError("Invalid PDF page interval")
                with ThreadPoolExecutor(max_workers=3) as pool:
                    return text({"pages": list(pool.map(
                        lambda index: Documents.scan_title(path, index), range(first, last + 1)))})
            if tool in {"search_pdf", "search_pdf_terms"}:
                queries = args['queries'] if tool == "search_pdf_terms" else [args['query']]
                identity = f'text-v2-{pymupdf.VersionBind}-{digest}' if digest and PDF_DIGEST.fullmatch(digest) else None
                result = Documents.search(pdf, identity, queries, args.get('first_page', 1),
                                          args.get('last_page', len(pdf)), cache_dir)
                if tool == "search_pdf":
                    for hit in result['matches']:
                        del hit['queries']
                return text(result)
            index = args["page"]
            if type(index) is not int or not 1 <= index <= len(pdf):
                raise ValueError("Invalid PDF page")
            page = pdf[index - 1]
            if tool == "read_page":
                from codex_sources import join_wrapped_designations

                return text(join_wrapped_designations(page.get_text(sort=True))[:60_000])
            if tool == "read_tables":
                return text([values for values, _ in Documents.native_tables(page)])
            if tool in {"view_page", "view_region"}:
                rect = page.rect
                if tool == "view_region":
                    rect = Documents.region(rect, args['bbox'])
                scale = min(6.0 if tool == "view_region" else 2.0, 2400 / max(rect.width, rect.height))
                image = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect).tobytes("png")
                return {"type": "image", "mimeType": "image/png",
                        "data": base64.b64encode(image).decode("ascii")}
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "rus+eng"],
                input=page.get_pixmap(dpi=200).tobytes("png"), capture_output=True,
                timeout=60, check=True,
            )
            return text(result.stdout.decode("utf-8", errors="replace")[:60_000])

    @staticmethod
    def search(pdf, name, queries, first, last, cache_dir):
        if not isinstance(queries, list) or not 1 <= len(queries) <= 8 or any(
            not isinstance(query, str) or not 1 <= len(query.strip()) <= 400 for query in queries
        ):
            raise ValueError("Invalid search text")
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(pdf):
            raise ValueError("Invalid PDF search interval")
        phrases = {' '.join(query.casefold().split()): query for query in queries}
        deadline = monotonic() + 45
        matches = []
        for index in range(first, last + 1):
            if index > first and (monotonic() >= deadline or len(matches) >= 100):
                return {"matches": matches, "truncated": True, "next_page": index}
            page_matches = []
            for line in Documents.searchable_lines(pdf[index - 1], cache_dir, name):
                normalized = ' '.join(line.casefold().split())
                found = [query for phrase, query in phrases.items() if phrase in normalized]
                if found:
                    page_matches.append({"page": index, "text": line, "queries": found})
            if len(matches) + len(page_matches) > 100 and index > first and matches:
                return {"matches": matches, "truncated": True, "next_page": index}
            if len(page_matches) > 100:
                return {"matches": page_matches[:100], "truncated": True,
                        "read_page_for_remaining_hits": index, "next_page": index + 1 if index < last else None}
            matches.extend(page_matches)
        return {"matches": matches, "truncated": False, "next_page": None}

    @staticmethod
    def searchable_lines(page, cache_dir, name):
        from codex_sources import join_wrapped_designations

        if cache_dir is None or name is None:
            return join_wrapped_designations(page.get_text()).splitlines()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f'{name}-{page.number + 1}.json'
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            lines = join_wrapped_designations(page.get_text()).splitlines()
        with NamedTemporaryFile(dir=cache_dir, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(json.dumps(lines, ensure_ascii=False).encode('utf-8'))
                stream.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return lines

    @staticmethod
    def page_summary(page):
        # Landscape drawings can contain huge vector streams. Their text can
        # be inspected separately once the sheet is selected.
        landscape = page.rect.width > page.rect.height
        lines = [] if landscape else [' '.join(line.split()) for line in page.get_text().splitlines()]
        lines = [line for line in lines if sum(character.isalpha() for character in line) >= 4]
        return {"page": page.number + 1, "text_layer": None if landscape else bool(lines),
                "heading_hint": ' | '.join(lines[:5])[:240],
                "image_resources": len(page.get_images()),
                "landscape": landscape}

    @staticmethod
    def scan_title(path, index):
        import pymupdf

        with pymupdf.open(path) as pdf:
            page = pdf[index - 1]
            rect = page.rect
            clip = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height * .27)
            scale = min(3.5, 3600 / rect.width)
            crop = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
            try:
                result = subprocess.run(['tesseract', 'stdin', 'stdout', '-l', 'rus+eng', '--psm', '11'],
                    input=crop.tobytes('png'), capture_output=True, timeout=12, check=True,
                    env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
            except (OSError, subprocess.SubprocessError):
                return {"page": index, "status": "unreadable", "heading_hint": ""}
        title = ' '.join(result.stdout.decode('utf-8', errors='replace').split())
        return {"page": index, "status": "read" if title else "unreadable", "heading_hint": title[:700]}


def main() -> None:
    root = Path(__file__).resolve().parent
    documents = Documents(root, Path(sys.argv[1]) if len(sys.argv) == 2 else None)
    for line in sys.stdin.buffer:
        request = json.loads(line)
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        method = request.get("method")
        params = request.get("params") or {}
        if method == "initialize":
            response["result"] = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "serverInfo": {"name": "pinned-audit-documents", "version": "2"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            response["result"] = {"tools": [{"name": name, **spec} for name, spec in TOOLS.items()]}
        elif method == "tools/call":
            started = monotonic()
            tool = params.get("name")
            success = True
            try:
                content = documents.call(tool, params.get("arguments") or {})
            except (AttributeError, KeyError, ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError):
                success = False
                content = text("Unable to read this pinned input with the supplied arguments.")
            response["result"] = {"content": [content], "isError": not success}
            event = {"event": "document_tool.completed", "at": datetime.now(UTC).isoformat(),
                     "tool": tool if tool in TOOLS else "unknown", "success": success,
                     "document_kind": documents.documents.get(
                         (params.get("arguments") or {}).get("document_id"), {}).get("kind"),
                     "duration_ms": round((monotonic() - started) * 1000)}
            page = (params.get("arguments") or {}).get("page")
            if type(page) is int and page >= 1:
                event["page"] = page
            if success and tool in {'find_project_total', 'read_table_cell'}:
                outcome = json.loads(content['text'])
                event['outcome'] = outcome['status']
                event['cache_hit'] = bool(outcome.get('cell_cache_hit'))
                for field in ('reason', 'header_rereads', 'numeric_ocr_retries'):
                    if outcome.get(field) is not None:
                        event[field] = outcome[field]
            with (root / "tool-events.jsonl").open("ab") as stream:
                stream.write(json.dumps(event).encode("utf-8") + b"\n")
        elif method == "ping":
            response["result"] = {}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
