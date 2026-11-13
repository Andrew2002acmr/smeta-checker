"""Read selected cells of a pinned raster table and retain their PDF coordinates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from pathlib import Path
from tempfile import NamedTemporaryFile

import ocr_engine
from pdf_cell_ocr import (
    NUMERIC_RECOVERY_VERSION,
    _numeric_rapid_engine,
    _rapid_numeric,
    _read_variants,
    _recover_numeric,
    clean_glyphs,
    recovered_numeric_value,
)
from pdf_ruled_recovery import VERSION as GRID_VERSION
from pdf_ruled_recovery import closed_geometries, raster_grid

VERSION = 'targeted-ruled-v9'
MAX_LABELS = 180


def _label(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е').split()).strip(' ,.:')


def _crop(page, bbox, dpi):
    import pymupdf

    clip = pymupdf.Rect(bbox) * page.rotation_matrix & page.rect
    if clip.is_empty or clip.get_area() * (dpi / 72) ** 2 > 2_000_000:
        raise ValueError('Targeted PDF crop exceeds its pixel budget')
    return ocr_engine._pil_from_pixmap(page.get_pixmap(clip=clip, dpi=dpi, alpha=False))


def _grid(page):
    import numpy as np
    import pymupdf

    from pdf_tables import physical_table_regions

    dpi = ocr_engine.bounded_render_dpi(page.rect.width, page.rect.height,
        dpi=300, max_pixels=24_000_000, max_side=4960)
    scale = dpi / 72
    visible = page.rect * page.derotation_matrix
    regions = []
    for info in page.get_image_info():
        box = pymupdf.Rect(info['bbox']) & visible
        box = box * page.rotation_matrix & page.rect
        if box.width >= page.rect.width * .2 and box.height >= page.rect.height * .08:
            box = pymupdf.Rect(box.x0-6, box.y0-18, box.x1+6, box.y1+6) & page.rect
            regions.append(box)
            if len(regions) > 8:
                raise ValueError('Too many candidate table regions')
    rasters = []
    if not regions:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        image = np.asarray(ocr_engine._pil_from_pixmap(pix))
        regions = physical_table_regions(raster_grid(image), pix.width, pix.height)
        if len(regions) > 8:
            raise ValueError('Too many candidate table regions')
        rasters = [(x0, y0, image[y0:y1, x0:x1]) for x0, y0, x1, y1 in regions]
    else:
        for region in regions:
            pix = page.get_pixmap(dpi=dpi, clip=region, alpha=False)
            rasters.append((pix.x, pix.y, np.asarray(ocr_engine._pil_from_pixmap(pix))))
    seen = []
    for x0, y0, image in rasters:
        geometries = closed_geometries(raster_grid(image))
        if not geometries:
            geometries = closed_geometries(raster_grid(image, minimum_rule_fraction=45), tolerance=15)
        for boxes, slots in geometries:
            bounds = pymupdf.Rect(min(b[0] for b in boxes)+x0, min(b[1] for b in boxes)+y0,
                                  max(b[2] for b in boxes)+x0, max(b[3] for b in boxes)+y0)
            if any((bounds & other).get_area() >= .96 * max(bounds.get_area(), other.get_area())
                   for other in seen):
                continue
            seen.append(bounds)
            yield [dict(bbox=list(pymupdf.Rect(x0+b[0],y0+b[1],x0+b[2],y0+b[3])
                * (1/scale) * page.derotation_matrix), slots=s) for b, s in zip(boxes, slots, strict=True)]


def _note_grids(page):
    """Find compact ruled notes, including those outside the main scan region."""
    import numpy as np
    import pymupdf

    dpi = ocr_engine.bounded_render_dpi(page.rect.width, page.rect.height,
        dpi=300, max_pixels=24_000_000, max_side=4960)
    clips = []
    for info in page.get_image_info():
        clip = pymupdf.Rect(info['bbox']) * page.rotation_matrix & page.rect
        if .004 <= clip.get_area() / page.rect.get_area() <= .2:
            clips.append(pymupdf.Rect(clip.x0-3, clip.y0-3, clip.x1+3, clip.y1+3) & page.rect)
    seen = []
    for clip in [*clips[:16], page.rect]:
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        image = np.asarray(ocr_engine._pil_from_pixmap(pix))
        for boxes, slots in closed_geometries(raster_grid(image, minimum_rule_fraction=45),
                                             minimum_cells=2, minimum_rows=1):
            if not 2 <= len(boxes) <= 12 or max(slot[3] for slot in slots) > 3:
                continue
            if max(b[3]-b[1] for b in boxes) > page.rect.height * dpi / 72 * .09:
                continue
            bounds = pymupdf.Rect(min(b[0] for b in boxes)+pix.x, min(b[1] for b in boxes)+pix.y,
                                 max(b[2] for b in boxes)+pix.x, max(b[3] for b in boxes)+pix.y)
            if any((bounds & old).get_area() > .8 * bounds.get_area() for old in seen):
                continue
            seen.append(bounds)
            yield [dict(bbox=list(pymupdf.Rect(b[0]+pix.x,b[1]+pix.y,b[2]+pix.x,b[3]+pix.y)
                                  * (72 / dpi) * page.derotation_matrix), slots=slot)
                   for b, slot in zip(boxes, slots, strict=True)]


def _read_labels(page, candidates):
    import numpy as np
    import pymupdf
    from PIL import ImageOps

    from pdf_header_lines import line_boxes

    crops, owners = [], []
    for cell in candidates:
        cell['text'] = ''
        box = pymupdf.Rect(cell['bbox'])
        if box.is_empty:
            continue
        dpi = min(450, math.floor(72 * math.sqrt(1_800_000 / box.get_area())))
        if dpi < 72:
            continue
        glyphs = clean_glyphs(_crop(page, cell['bbox'], dpi), preserve_gray=True)
        if glyphs is None:
            continue
        for line in line_boxes(glyphs, gap=4, threshold=250)[:8]:
            crops.append(np.asarray(ImageOps.expand(glyphs.crop(line).convert('RGB'), border=6, fill='white')))
            owners.append(cell)
    for at in range(0, len(crops), 16):
        readings = _numeric_rapid_engine().recognize_txt(crops[at:at+16])
        for cell, text in zip(owners[at:at+16], readings.txts, strict=True):
            cell['text'] = (cell['text'] + ' ' + str(text).strip()).strip()


def _structure(page):
    from pdf_tables import station_value

    tables = []
    for cells in _grid(page):
        last_column = max(cell['slots'][3] for cell in cells)
        # Some schedules split the body with a full-width section label.
        row_labels = [cell for cell in cells if cell['slots'][2] == 0
                      and cell['slots'][1] == cell['slots'][0]]
        if len(row_labels) > MAX_LABELS:
            continue
        try:
            _read_labels(page, row_labels)
        except ValueError:
            continue
        starts = [0, *(c['slots'][0] for c in row_labels
                       if c['slots'][0] and c['slots'][3] >= last_column - 1
                       and any(word in _tokens(c['text']) for word in
                               ('устройство', 'установка', 'пересечение')))]
        if len(starts) == 1:
            caption = _caption_between_grids(page, cells, row_labels, last_column)
            if caption:
                start = caption['slots'][0]
                cells = [caption, *(c for c in cells if c['slots'][0] > start)]
                row_labels = [c for c in row_labels if c['slots'][0] > start]
                starts = [start]
        stop = max(c['slots'][1] for c in cells) + 1
        for start, end in zip(starts, [*starts[1:], stop], strict=True):
            section = [c for c in cells if start <= c['slots'][0] < end]
            rows = [c for c in row_labels if start <= c['slots'][0] < end
                    and (station_value(c['text'].strip(' _|'))
                         or _is_total(c['text']))]
            if not rows:
                continue
            body = rows[0]['slots'][0]
            headers = [c for c in section if c['slots'][1] < body]
            if not headers or len(headers) > MAX_LABELS:
                continue
            try:
                _read_labels(page, [c for c in headers if 'text' not in c])
            except ValueError:
                continue
            tables.append(dict(cells=section, headers=headers, rows=rows))
    return tables


def _caption_between_grids(page, cells, labels, last_column):
    import pymupdf

    totals = [c for c in labels if _is_total(c['text'])]
    if len(totals) < 2 or any(c['slots'][3] >= last_column - 1 for c in labels):
        return None
    earlier, later = totals[0], totals[1]
    first = min((c['slots'][0] for c in cells if c['slots'][2] == 0
                 and earlier['slots'][0] < c['slots'][0] < later['slots'][0]
                 and c['slots'][1] > c['slots'][0]), default=None)
    if first is None:
        return None
    top = pymupdf.Rect()
    for cell in cells:
        if cell['slots'][0] == first:
            top |= pymupdf.Rect(cell['bbox']) * page.rotation_matrix
    previous = pymupdf.Rect(earlier['bbox']) * page.rotation_matrix
    if not 5 <= top.y0 - previous.y1 <= 50:
        return None
    rect = pymupdf.Rect(top.x0, previous.y1, top.x1, top.y0)
    caption = dict(bbox=list(rect * page.derotation_matrix), slots=[first-1, first-1, 0, last_column])
    caption['text'] = _read_crop_text(page, caption['bbox'], dpi=300, preserve_edges=True)
    words = _tokens(caption['text'])
    return caption if sum(len(word) >= 4 and word.isalpha() for word in words) >= 3 else None


def _decimal(page, bbox, dpi=600, *, original_only=False):
    def read(data, **options):
        return ocr_engine.parse_tsv(ocr_engine.run_tsv(data, whitelist='0123456789.,-+', **options))

    image = _crop(page, bbox, dpi)
    deadline = time.monotonic() + 10
    proof = dict(dpi=dpi, rotation=page.rotation, image_sha256=hashlib.sha256(ocr_engine._to_png(image)).hexdigest(),
        variants=[] if original_only else _read_variants(image, numeric=True, read=read, deadline=deadline))
    from PIL import Image, ImageOps

    if original_only:
        # Thin decimal marks can survive in the original cell at low DPI
        # while disappearing from the binarized and resized variants.
        for zoom in (225, 300, 375, 450):
            if time.monotonic() >= deadline:
                break
            original = image if zoom == dpi else _crop(page, bbox, zoom)
            padded = ImageOps.expand(original, border=16, fill='white')
            try:
                words = read(ocr_engine._to_png(padded), language='eng', psm=7, timeout=2)
            except RuntimeError:
                continue
            proof['variants'].append(dict(height=padded.height, psm=7, render_dpi=zoom, words=words))
    if not original_only and (gray := clean_glyphs(image, preserve_gray=True)) is not None:
        for height in (32, 48, 64):
            if time.monotonic() >= deadline:
                break
            width = max(1, round(gray.width * height / gray.height))
            scaled = ImageOps.expand(gray.resize((width, height), Image.Resampling.LANCZOS), border=16, fill='white')
            try:
                words = read(ocr_engine._to_png(scaled), language='eng', psm=13, timeout=2)
            except RuntimeError:
                continue
            proof['variants'].append(dict(height=height, psm=13, words=words))
    cell = dict(bbox=bbox, text='', words=[])
    if original_only:
        gray = clean_glyphs(image, preserve_gray=True)
        if gray is None:
            return None
        consensus = dict(**proof, version=NUMERIC_RECOVERY_VERSION, bbox=bbox,
                         rapid_words=_rapid_numeric(ImageOps.expand(gray, border=12, fill='white')))
    else:
        _recover_numeric(image, cell, proof, read, deadline)
        consensus = cell['numeric_recovery']
    if recovered_numeric_value(cell, consensus) is None:
        return None
    literal = re.sub(r'\s+', '', consensus['rapid_words'][0]['text'])
    return dict(text=literal, numeric_consensus=consensus)


def _cached(directory, identity, compute):
    path = _cache_file(directory, identity)
    try:
        return json.loads(path.read_text(encoding='utf-8')), True
    except (OSError, ValueError):
        result = compute()
    if result is None:
        return None, False
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, encoding='utf-8', mode='w', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(result, stream, ensure_ascii=False)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return result, False


def _cache_file(directory, identity):
    name = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return directory / f'{name}.json'


def _page_cache(cache_dir, pdf_sha256, page_number):
    identity = [VERSION, GRID_VERSION, NUMERIC_RECOVERY_VERSION, pdf_sha256, page_number]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return Path(cache_dir) / key


def _page_structure(page, pdf_sha256, cache_dir):
    if not isinstance(pdf_sha256, str) or not re.fullmatch('[a-f0-9]{64}', pdf_sha256):
        raise ValueError('Expected a pinned PDF SHA-256')
    directory = _page_cache(cache_dir, pdf_sha256, page.number+1)
    return _cached(directory, 'structure', lambda: _structure(page))


def describe(page, *, pdf_sha256, cache_dir):
    tables, hit = _page_structure(page, pdf_sha256, cache_dir)
    descriptions = []
    for table in tables:
        headers = [header for header in table['headers'] if header['text']]
        rows = [row for row in table['rows'] if row['text']]
        columns = sorted({cell['slots'][2] for cell in table['cells']
                          if cell['slots'][2] == cell['slots'][3]})
        column_headers = [list(dict.fromkeys(header['text'] for header in headers
                                           if header['slots'][2] <= column <= header['slots'][3]))
                          for column in columns]
        descriptions.append(dict(headers=headers, rows=rows,
            table_bbox=[min(cell['bbox'][0] for cell in table['cells']),
                        min(cell['bbox'][1] for cell in table['cells']),
                        max(cell['bbox'][2] for cell in table['cells']),
                        max(cell['bbox'][3] for cell in table['cells'])],
            row_count=max(cell['slots'][1] for cell in table['cells']) + 1,
            column_headers=[labels for labels in column_headers if labels]))
    return dict(page=page.number+1, structure_cache_hit=hit, tables=descriptions)


def _tokens(value):
    value = _label(value).translate(str.maketrans('abcehkmoptxy', 'авсенкмортху'))
    return re.findall(r'[а-я0-9]+', value)


def _contains(value, query):
    words = _tokens(value)
    return all(any(word == term if term.isdecimal() else word.startswith(term) for word in words)
               for term in _tokens(query))


def _is_total(text):
    # OCR may insert a space within the isolated label, e.g. «Вс его».
    return ''.join(_tokens(text)) in {'всего', 'итого'}


def scope_binding(cell, row, headers, unit, *, query=None):
    """Describe observed geometry. Engineering applicability is evaluated separately."""
    return dict(slots=cell['slots'], row={k: row[k] for k in ('text', 'bbox', 'slots')},
                headers=[{k: h[k] for k in ('text', 'bbox', 'slots')} for h in headers], unit=unit,
                **({'query': query} if query else {}))


def find_totals(page, *, pdf_sha256, cache_dir, scope, column, unit):
    """Resolve a total by heading path. Never use the VOR quantity to select it."""
    if not _tokens(scope) or not _tokens(column) or unit not in {'м', 'м2', 'м3'}:
        raise ValueError('Expected a table scope, column description and unit')
    tables, structure_hit = _page_structure(page, pdf_sha256, cache_dir)
    matches = []
    directory = _page_cache(cache_dir, pdf_sha256, page.number+1) / 'header-crops'
    column_terms = _tokens(column)
    alternate_reads = {}
    def reread(header):
        key = tuple(header['bbox'])
        if key not in alternate_reads:
            if len(alternate_reads) >= 16:
                return None
            alternate_reads[key], _ = _cached(directory, _header_cache_key(header),
                lambda: _read_crop_text(page, header['bbox'], preserve_edges=True))
        return alternate_reads[key]

    for table in tables:
        width = max(h['slots'][3] for h in table['cells'])
        for row in table['rows']:
            if not _is_total(row['text']):
                continue
            for cell in table['cells']:
                r0, r1, c0, c1 = cell['slots']
                if r0 != row['slots'][0] or r0 != r1 or c0 != c1 or c0 <= row['slots'][3]:
                    continue
                headers = [h for h in table['headers'] if h['slots'][2] <= c0 <= h['slots'][3]]
                if not headers:
                    continue
                titles = [h for h in headers if h['slots'][2] == 0 and h['slots'][3] >= width - 1]
                if not _contains(' '.join(h['text'] for h in titles), scope):
                    variants = [reread(h) for h in titles]
                    if None in variants:
                        return dict(page=page.number+1, status='search_incomplete',
                                    reason='header_search_budget_exhausted', matches=len(matches))
                    if not _contains(' '.join(variants), scope):
                        continue
                    headers = [{**h, 'text': variants[titles.index(h)]} if h in titles else h for h in headers]
                parents = ' '.join(h['text'] for h in headers[:-1]
                                   if h['slots'][2] != 0 or h['slots'][3] < width - 1)
                leaf_terms = [term for term in column_terms if not _contains(parents, term)]
                leaf = headers[-1]
                if _matches_leaf(leaf['text'], leaf_terms, unit):
                    matches.append((cell, row, headers))
                else:
                    alternate = reread(leaf)
                    if alternate is None:
                        return dict(page=page.number+1, structure_cache_hit=structure_hit,
                                    status='search_incomplete', reason='header_search_budget_exhausted',
                                    matches=len(matches))
                    if alternate and _matches_leaf(alternate, leaf_terms, unit):
                        matches.append((cell, row, [*headers[:-1], {**leaf, 'text': alternate}]))
    result = dict(page=page.number+1, structure_cache_hit=structure_hit, header_rereads=len(alternate_reads))
    if not matches:
        return dict(**result, status='not_found', matches=0, reason='no_matching_table_path',
                    tables_examined=len(tables))
    if len(matches) > 1:
        candidates = [dict(bbox=cell['bbox'], scope_binding=scope_binding(cell, row, headers, unit))
                      for cell, row, headers in matches[:12]]
        return dict(**result, status='ambiguous', matches=len(matches), candidates=candidates)
    return _read_match(page, pdf_sha256, cache_dir, result, matches[0], unit=unit,
                       query={'scope': scope, 'column': column})


def _matches_leaf(text, terms, unit):
    leaf = re.sub(r'[мm]([²³])[23]?', lambda m: 'м' + unicodedata.normalize('NFKC', m[1]), text.lower())
    units = re.findall(r'м\s*([23]?)(?![а-яa-z0-9]|\s*[23])', _label(leaf).replace('m', 'м'))
    if not units or 'м' + units[0] != unit or not _contains(text, ' '.join(terms)):
        return False
    # Component order distinguishes designations with the same set of digits.
    tokens = _tokens(text)
    for match in re.finditer(r'(?:\d+ ){1,}\d+', ' '.join(terms)):
        sequence = match[0].split()
        if not any(tokens[i:i+len(sequence)] == sequence for i in range(len(tokens))):
            return False
    return True


def find_notes(page, *, pdf_sha256, cache_dir, query):
    """Return text from physically adjacent cells of compact project notes."""
    if not _tokens(query):
        raise ValueError('Expected note keywords')
    directory = _page_cache(cache_dir, pdf_sha256, page.number+1)

    def scan():
        grids = list(_note_grids(page))
        for cells in grids[:16]:
            _read_labels(page, cells)
        return grids[:16]

    grids, hit = _cached(directory, 'compact-notes-v2', scan)
    matches = []
    for grid in grids:
        for row in sorted({c['slots'][0] for c in grid}):
            cells = [c for c in grid if c['slots'][0] == c['slots'][1] == row and c['text']]
            if len(cells) >= 2 and _contains(' '.join(c['text'] for c in cells), query):
                for cell in cells:
                    cell['verified'] = verify_note(page, cell['bbox'], cell['text'])
                matches.append(cells)
    return dict(page=page.number+1, cache_hit=hit, matches=matches)


def _header_cache_key(header):
    return ['preserve-edges', header['bbox']]


def _read_crop_text(page, bbox, *, dpi=450, psm=6, preserve_edges=False):
    from PIL import ImageOps

    try:
        crop = _crop(page, bbox, dpi)
        if preserve_edges:
            from PIL import ImageDraw

            glyphs = crop.convert('RGB')
            # Mask the rule itself. Connected-component removal can erase
            # adjacent digits that touch it.
            border = max(2, round(dpi / 100))
            ImageDraw.Draw(glyphs).rectangle((0, 0, glyphs.width-1, glyphs.height-1),
                                            outline='white', width=border)
        else:
            glyphs = clean_glyphs(crop, preserve_gray=True)
        if glyphs is None:
            return ''
        image = ImageOps.expand(glyphs.convert('RGB'), border=12, fill='white')
        words = ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(image),
            language='rus+eng', psm=psm, timeout=4))
    except (RuntimeError, OSError, ValueError):
        return ''
    return ' '.join(word['text'] for word in words)


def verify_note(page, bbox, quote):
    from codex_contract import normalize_text

    # OCR engines may disagree only on the printed line-break/spacing around
    # punctuation. Keep every letter, digit, separator and unit in the match.
    def normalize(text):
        return re.sub(r'\s*([,./])\s*', r'\1', normalize_text(text).casefold())

    expected = normalize(quote)
    return bool(expected) and any(normalize(_read_crop_text(page, bbox, dpi=dpi, psm=psm)) == expected
                                  for dpi, psm in ((450, 6), (300, 6), (600, 6), (450, 11)))


def cached_cell_texts(cache_dir, pdf_sha256, page_number, *, bbox=None):
    """Return only independently corroborated readings for citation checks."""
    for path in (_page_cache(cache_dir, pdf_sha256, page_number) / 'cells').glob('*.json'):
        try:
            reading = json.loads(path.read_text(encoding='utf-8'))
            proof = reading['numeric_consensus']
            if bbox is not None and proof['bbox'] != bbox:
                continue
            value = reading['text']
            if recovered_numeric_value(dict(bbox=proof['bbox'], text=value, words=[]), proof) is not None:
                yield re.sub(r'\s+', '', value)
        except (OSError, ValueError, TypeError, KeyError):
            continue


def verify_scope_binding(page, *, pdf_sha256, cache_dir, bbox, binding):
    """Check the physical row and all enclosing header addresses against the pinned table."""
    tables, _ = _page_structure(page, pdf_sha256, cache_dir)
    for table in tables:
        cell = next((item for item in table['cells'] if item['bbox'] == bbox), None)
        if cell is None or cell['slots'] != binding['slots']:
            continue
        r0, _, column, _ = cell['slots']
        row = next((item for item in table['rows'] if item['slots'][0] == r0), None)
        headers = [h for h in table['headers'] if h['slots'][2] <= column <= h['slots'][3]]
        if row is None or {k: row[k] for k in ('text', 'bbox', 'slots')} != binding['row']:
            continue
        actual = binding['headers']
        if len(headers) != len(actual) or any(
            h['bbox'] != ref['bbox'] or h['slots'] != ref['slots']
            for h, ref in zip(headers, actual, strict=True)
        ):
            continue
        directory = _page_cache(cache_dir, pdf_sha256, page.number+1) / 'header-crops'
        for header, ref in zip(headers, actual, strict=True):
            if header['text'] == ref['text']:
                continue
            alternate, _ = _cached(directory, _header_cache_key(header),
                                   lambda: _read_crop_text(page, header['bbox'], preserve_edges=True))
            if alternate != ref['text']:
                break
        else:
            if actual and _matches_leaf(actual[-1]['text'], [], binding['unit']):
                return True
    return False


def corroborate_scope_text(page, binding):
    """Second-engine reading of the actual header crops, separate from address validation."""
    query = binding.get('query')
    if not query or not binding['headers']:
        return None
    headers = binding['headers']
    last = headers[-1]
    titles = [h for h in headers[:-1] if h['slots'][2] == 0]
    parents = [h for h in headers[:-1] if h not in titles]
    for dpi in (300, 450):
        title = ' '.join(_read_crop_text(page, h['bbox'], dpi=dpi, preserve_edges=True) for h in titles)
        if not _contains(title, query['scope']):
            continue
        parent = ' '.join(_read_crop_text(page, h['bbox'], dpi=dpi, preserve_edges=True) for h in parents)
        leaf = _read_crop_text(page, last['bbox'], dpi=dpi, preserve_edges=True)
        terms = [term for term in _tokens(query['column']) if not _contains(parent, term)]
        if _matches_leaf(leaf, terms, binding['unit']):
            return True
    return False


def verify_cached_header(page, *, pdf_sha256, cache_dir, quote, bbox=None):
    """Corroborate an exact cached header on its own crop with Tesseract."""
    from codex_contract import normalize_text

    directory = _page_cache(cache_dir, pdf_sha256, page.number+1)
    try:
        tables = json.loads(_cache_file(directory, 'structure').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    from PIL import ImageOps

    attempts = 0
    for table in tables:
        for header in table['headers']:
            if normalize_text(header['text']) != quote or (bbox is not None and header['bbox'] != bbox):
                continue
            if attempts >= 4:
                return False
            attempts += 1
            try:
                for dpi in (450, 600):
                    image = ImageOps.expand(_crop(page, header['bbox'], dpi), border=16, fill='white')
                    words = ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(image),
                        language='rus+eng', psm=7, timeout=3))
                    if (not words or min(word['conf'] for word in words) < 80
                            or normalize_text(' '.join(word['text'] for word in words)) != quote):
                        break
                else:
                    return True
            except (RuntimeError, OSError, ValueError):
                continue
    return False


def read_cell(page, *, pdf_sha256, row, headers, cache_dir, row_bbox=None):
    """Read one physical intersection from an already verified PDF version."""
    if (not isinstance(row, str) or not 1 <= len(_label(row)) <= 160 or not isinstance(headers, list)
            or not 1 <= len(headers) <= 8 or any(not isinstance(h, str) or not 1 <= len(_label(h)) <= 160 for h in headers)):
        raise ValueError('Invalid targeted table query')
    tables, structure_hit = _page_structure(page, pdf_sha256, cache_dir)
    matches = []
    for table in tables:
        for label in table['rows']:
            if _label(label['text']) != _label(row) or (row_bbox is not None and label['bbox'] != row_bbox):
                continue
            for cell in table['cells']:
                r0, r1, c0, c1 = cell['slots']
                if r0 != label['slots'][0] or r0 != r1 or c0 != c1 or c0 <= label['slots'][3]:
                    continue
                column = [h for h in table['headers'] if h['slots'][2] <= c0 <= h['slots'][3] and h['text']]
                if all(any(_label(h['text']) == _label(wanted) for h in column) for wanted in headers):
                    matches.append((cell, label, column))
    result = dict(page=page.number+1, structure_cache_hit=structure_hit)
    if len(matches) != 1:
        if not matches:
            return dict(**result, status='not_found', matches=0, reason='no_matching_table_path',
                        tables_examined=len(tables))
        candidates = [dict(bbox=cell['bbox'], scope_binding=scope_binding(cell, label, path, None))
                      for cell, label, path in matches[:12]]
        return dict(**result, status='ambiguous', matches=len(matches), candidates=candidates)
    return _read_match(page, pdf_sha256, cache_dir, result, matches[0])


def _read_match(page, pdf_sha256, cache_dir, result, match, *, unit=None, query=None):
    cell, label, column = match
    directory = _page_cache(cache_dir, pdf_sha256, page.number+1) / 'cells'
    attempts = 0

    def read():
        nonlocal attempts
        for dpi in (600, 450):
            attempts += 1
            if reading := _decimal(page, cell['bbox'], dpi):
                return reading
        attempts += 1
        return _decimal(page, cell['bbox'], 225, original_only=True)

    reading, cell_hit = _cached(directory, cell['bbox'], read)
    if reading:
        reading = {**reading, 'text': re.sub(r'\s+', '', reading['text'])}
    return dict(**result, status='read' if reading else 'unreadable', cell_cache_hit=cell_hit,
        bbox=cell['bbox'], row=label, headers=column, reading=reading,
        slots=cell['slots'], unit=unit,
        numeric_ocr_retries=max(0, attempts - 1),
        scope_binding=scope_binding(cell, label, column, unit, query=query) if unit else None,
        reason=None if reading else 'numeric_consensus_missing',
        coordinate_system='original unrotated PDF points, top-left origin')
