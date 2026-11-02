"""Recover physical raster grids using frozen OCR words, never VOR quantities.

Returned cells use original PDF points. No OCR/network calls or numeric verdicts
are made here. A malformed table is replaced only when its words are covered by
recovered regions; unrecovered source tables remain available as uncertain proof.
"""
from copy import deepcopy
import hashlib
import json
import math
import time

VERSION = 'embedded-ruled-recovery-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode('utf-8')).hexdigest()


def collect_words(tables, matrix):
    import pymupdf
    found = {}
    for table in tables:
        words = [*table.get('unassigned_words', []),
            *(word for cell in table['cells'] for word in cell.get('words', []))]
        for word in words:
            key = (word['text'], tuple(round(v, 3) for v in word['bbox']))
            if key not in found:
                found[key] = dict(deepcopy(word), original_bbox=list(word['bbox']))
                found[key]['source_table_ids'] = []
            if table['table_id'] not in found[key]['source_table_ids']:
                found[key]['source_table_ids'].append(table['table_id'])
    for word in found.values():
        word['bbox'] = list(pymupdf.Rect(word['bbox']) * matrix)
    return list(found.values())


def raster_grid(image):
    import cv2
    import numpy as np
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 35, 2)
    height, width = gray.shape
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, width//25), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, height//25))))
    return cv2.morphologyEx(cv2.bitwise_or(horizontal, vertical), cv2.MORPH_CLOSE,
        np.ones((5, 5), dtype=np.uint8))


def geometry_from_boxes(boxes, *, tolerance=6):
    """Accept closed nonoverlapping cells, allowing an exterior stepped edge.

    A tall left header can leave an empty upper-right corner. Such exterior
    gaps are not data cells. Interior holes and overlapping slots are rejected.
    """
    if not 4 <= len(boxes) <= 4000:
        return None

    def edges(values):
        groups = []
        for value in sorted(values):
            if groups and value-groups[-1][-1] <= tolerance:
                groups[-1].append(value)
            else:
                groups.append([value])
        return [sum(group)/len(group) for group in groups]

    xs = edges([box[i] for box in boxes for i in (0, 2)])
    ys = edges([box[i] for box in boxes for i in (1, 3)])
    if min(len(xs), len(ys)) < 3 or (len(xs)-1)*(len(ys)-1) > 12000:
        return None
    snapped, logical, occupied = [], [], set()
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        c0, c1 = [min(range(len(xs)), key=lambda j: abs(xs[j]-box[i])) for i in (0, 2)]
        r0, r1 = [min(range(len(ys)), key=lambda j: abs(ys[j]-box[i])) for i in (1, 3)]
        slots = {(r, c) for r in range(r0, r1) for c in range(c0, c1)}
        if not slots or slots & occupied:
            return None
        occupied.update(slots)
        snapped.append([xs[c0], ys[r0], xs[c1], ys[r1]])
        logical.append([r0, r1-1, c0, c1-1])
    height, width = len(ys)-1, len(xs)-1
    holes = {(r, c) for r in range(height) for c in range(width)}-occupied
    exterior = {slot for slot in holes if slot[0] in {0, height-1} or slot[1] in {0, width-1}}
    pending = list(exterior)
    while pending:
        r, c = pending.pop()
        for neighbour in ((r-1, c), (r+1, c), (r, c-1), (r, c+1)):
            if neighbour in holes and neighbour not in exterior:
                exterior.add(neighbour)
                pending.append(neighbour)
    if holes != exterior:
        return None
    return snapped, logical


def closed_geometries(grid):
    """Recover each independent ruled component, including in a shared scan."""
    import cv2
    contours, hierarchy = cv2.findContours(grid, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    by_parent = {}
    for index, contour in enumerate(contours):
        parent = int(hierarchy[0][index][3])
        if parent < 0:
            continue
        x, y, width, height = cv2.boundingRect(contour)
        if width > 10 and height > 10 and width*height > 150:
            by_parent.setdefault(parent, []).append([x, y, x+width, y+height])
    candidates = []
    for boxes in by_parent.values():
        geometry = geometry_from_boxes(boxes)
        if geometry is not None:
            bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)]
            candidates.append((bounds, geometry))
    accepted = []
    for bounds, geometry in sorted(candidates, key=lambda c: -(c[0][2]-c[0][0])*(c[0][3]-c[0][1])):
        if any(min(bounds[2], old[2]) > max(bounds[0], old[0])
                and min(bounds[3], old[3]) > max(bounds[1], old[1]) for old, _ in accepted):
            continue
        accepted.append((bounds, geometry))
    return [geometry for _, geometry in sorted(accepted, key=lambda c: (c[0][1], c[0][0]))]


def closed_geometry(grid):
    """Compatibility helper for callers expecting one physical grid."""
    geometries = closed_geometries(grid)
    return geometries[0] if geometries else None


def rebuild(boxes, logical, words, *, page, source_table_ids=()):
    """Full word containment prevents assigning crossing text by its value."""
    from pdf_tables import finish_table, physical_header_rows
    geometry_hash = _digest([[round(v, 4) for v in box] for box in boxes] + logical)
    words_hash = _digest(sorted((dict(text=w['text'], bbox=w['bbox'], confidence=w.get('confidence'))
        for w in words), key=lambda w: (w['bbox'], w['text'])))
    table_id = 'ruled-'+_digest([geometry_hash, words_hash])[:16]
    cells = [dict(row=int(r0), column=int(c0), rowspan=int(r1-r0+1), colspan=int(c1-c0+1),
        bbox=list(map(float, box)), text='', words=[], numeric_verified=False)
        for box, (r0, r1, c0, c1) in zip(boxes, logical, strict=False)]
    if not cells:
        raise ValueError('Cannot rebuild an empty physical grid')
    unassigned = []
    for original in words:
        word = deepcopy(original)
        x0, y0, x1, y1 = word['bbox']
        matches = [cell for cell in cells if cell['bbox'][0]-1 <= x0 and cell['bbox'][1]-1 <= y0
            and cell['bbox'][2]+1 >= x1 and cell['bbox'][3]+1 >= y1]
        if len(matches) != 1:
            unassigned.append(word)
            continue
        cell = matches[0]
        cell['words'].append(word)
        if word.get('confidence', 0) < (.9 if any(ch.isdigit() for ch in word['text']) else .6):
            cell['quality'] = 'uncertain'
    for cell in cells:
        cell['words'].sort(key=lambda word: (word['bbox'][1], word['bbox'][0]))
        cell['text'] = '\n'.join(word['text'] for word in cell['words'])
    header_rows = physical_header_rows(cells)
    result = finish_table(cells, table_id=table_id, page=page,
        bbox=[min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
        origin='ocr', header_rows=header_rows or 1,
        reasons=[] if header_rows is not None else ['recovered_header_unresolved'])
    result['unassigned_words'] = unassigned
    result['recovery'] = dict(version=VERSION, geometry_sha256=geometry_hash,
        source_words_sha256=words_hash, source_table_ids=list(source_table_ids),
        coordinate_system='original PDF points', independent_numeric_verification=False)
    return result


def merge_recovered(originals, recovered, regions):
    """Keep source tables if any significant portion has not been recovered."""
    output = list(recovered)
    retained = []
    for table in originals:
        if any(t['table_id'] == table['table_id'] for t in recovered):
            continue
        if table.get('origin') != 'ocr':
            output.append(deepcopy(table))
            continue
        words = [*table.get('unassigned_words', []),
            *(word for cell in table['cells'] for word in cell.get('words', []))]
        points = [((w['bbox'][0]+w['bbox'][2])/2, (w['bbox'][1]+w['bbox'][3])/2) for w in words]
        covered = sum(any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in regions) for x, y in points)
        if not points or covered/len(points) < .95:
            fallback = deepcopy(table)
            fallback['quality'] = 'uncertain'
            fallback['reasons'] = sorted(set(fallback.get('reasons', [])) | {'unrecovered_source_region'})
            for cell in fallback['cells']:
                cell['quality'] = 'uncertain'
                cell['numeric_verified'] = False
                cell['reasons'] = sorted(set(cell.get('reasons', [])) | {'unrecovered_source_region'})
            output.append(fallback)
            retained.append(table['table_id'])
    return output, retained


def recover_page(page, frozen_tables, *, rotation=0, seconds=20, max_images=8, max_pixels=12_000_000):
    """Return {tables, changed, diagnostics, version}; never mutate the PDF/cache."""
    import numpy as np
    import pymupdf
    result = dict(tables=deepcopy(frozen_tables), changed=False, diagnostics=[], version=VERSION)
    if rotation % 90 or not any(t.get('origin') == 'ocr' for t in frozen_tables):
        return result
    if all(t.get('origin') != 'ocr' or t.get('recovery', {}).get('version') == VERSION
            or t.get('recovery_attempt_version') == VERSION for t in frozen_tables):
        return result
    deadline = time.monotonic()+seconds
    with pymupdf.open() as private:
        private.insert_pdf(page.parent, from_page=page.number, to_page=page.number,annots=False,links=False)
        upright = private[0]
        upright.set_rotation((page.rotation-int(rotation)) % 360)
        matrix = upright.rotation_matrix
        inverse = ~matrix
        words = collect_words(frozen_tables, matrix)
        upright.remove_rotation()
        recovered, regions = [], []
        image_rects = []
        for image in sorted(upright.get_image_info(), key=lambda item: (item['bbox'][1], item['bbox'][0])):
            rect = pymupdf.Rect(image['bbox']) & upright.rect
            if rect.width < upright.rect.width*.2 or rect.height < upright.rect.height*.08:
                continue
            if any(abs(rect.x0-old.x0)+abs(rect.y0-old.y0)+abs(rect.x1-old.x1)+abs(rect.y1-old.y1) < 1 for old in image_rects):
                continue
            image_rects.append(rect)
        if len(image_rects) > max_images:
            result['diagnostics'].append(dict(reason='geometry_image_budget', images=len(image_rects), limit=max_images))
        for rect in image_rects[:max_images]:
            if time.monotonic() >= deadline:
                result['diagnostics'].append(dict(reason='geometry_time_budget'))
                break
            dpi = min(300, max(72, int(72*math.sqrt(max_pixels/(rect.width*rect.height)))))
            pix = upright.get_pixmap(dpi=dpi, clip=rect, alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            geometries = closed_geometries(raster_grid(image))
            observation = dict(image_bbox=list(pymupdf.Rect(rect)*inverse), dpi=dpi,
                pixels=[pix.width, pix.height], recovered=bool(geometries), grids=[])
            result['diagnostics'].append(observation)
            for boxes, logical in geometries:
                # Pixmap origins are integer device coordinates; using clip.width
                # would subtly shift cells when clipping rounds outward.
                scale = 72/dpi
                pdf_boxes = [[(pix.x+b[0])*scale, (pix.y+b[1])*scale,
                    (pix.x+b[2])*scale, (pix.y+b[3])*scale] for b in boxes]
                bounds = pymupdf.Rect(min(b[0] for b in pdf_boxes), min(b[1] for b in pdf_boxes),
                    max(b[2] for b in pdf_boxes), max(b[3] for b in pdf_boxes))
                inside = [word for word in words if bounds.contains(pymupdf.Point(
                    (word['bbox'][0]+word['bbox'][2])/2, (word['bbox'][1]+word['bbox'][3])/2))]
                if not inside:
                    observation['grids'].append(dict(reason='no_existing_ocr_words'))
                    continue
                table = rebuild(pdf_boxes, logical, inside, page=page.number+1,
                    source_table_ids=sorted({identity for word in inside for identity in word.get('source_table_ids', [])}))
                for item in [table, *table['cells'], *table['unassigned_words'],
                        *(word for cell in table['cells'] for word in cell['words'])]:
                    item['bbox'] = list(pymupdf.Rect(item['bbox'])*inverse)
                table['recovery'].update(original_rotation=page.rotation, ocr_rotation=rotation,
                    original_to_upright=list(matrix), image_bbox=observation['image_bbox'], dpi=dpi)
                recovered.append(table)
                regions.append(table['bbox'])
                observation['grids'].append(dict(table_id=table['table_id'], cells=len(table['cells']),
                    unassigned_words=len(table['unassigned_words'])))
            observation['recovered'] = any('table_id' in grid for grid in observation['grids'])
        if recovered:
            result['tables'], retained = merge_recovered(frozen_tables, recovered, regions)
            result['changed'] = True
            if retained:
                result['diagnostics'].append(dict(reason='unrecovered_source_tables_retained', table_ids=retained))
                if not any(d.get('reason') in {'geometry_time_budget', 'geometry_image_budget'} for d in result['diagnostics']):
                    for table in result['tables']:
                        if table['table_id'] in retained:
                            table['recovery_attempt_version'] = VERSION
    return result
