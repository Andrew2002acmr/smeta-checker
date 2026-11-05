"""Validate physical table boxes before OCR proofs and source links are produced."""
import math
from typing import Any

VERSION = 'pdf-visible-table-geometry-v1'


def validate_tables(page, tables: list[dict[str, Any]], **_):
    import pymupdf
    bounds = pymupdf.Rect(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1)
    if hasattr(page, 'derotation_matrix'):
        bounds = bounds * page.derotation_matrix
    elif getattr(page, 'rotation', 0):
        raise ValueError('Rotated source has no coordinate transform')
    changed = 0
    unlocatable = 0
    for table in tables:
        rejected_headers = set()
        for item in [table, *table['cells']]:
            box = item.get('bbox') or []
            if len(box) != 4 or not all(math.isfinite(v) for v in box):
                continue
            original = pymupdf.Rect(box)
            visible = original & bounds
            if original.is_empty or visible.is_empty:
                state, replacement = 'unlocatable', []
            elif all(abs(original[n] - visible[n]) <= .01 for n in range(4)):
                continue
            else:
                state, replacement = 'clipped', [visible[n] for n in range(4)]
            item.setdefault('raw_bbox_before_validation', list(box))
            item['bbox'] = replacement
            item['geometry_validation'] = dict(version=VERSION, state=state, page_bounds=[bounds[n] for n in range(4)],
                original_bbox=list(box), requires_source_review=True)
            item['quality'] = 'uncertain'
            if 'cell_id' in item:
                if item.get('numeric_verified'):
                    item['numeric_verified_before_geometry_validation'] = True
                item['numeric_verified'] = False
                if item.get('role') == 'header':
                    rejected_headers.add(item['cell_id'])
            changed += 1
            unlocatable += state == 'unlocatable'
        for column, proof in list((table.get('column_units') or {}).items()):
            if proof.get('header_id') in rejected_headers:
                table.setdefault('unit_proofs_before_geometry_validation', {})[column] = proof
                table['column_units'].pop(column)
    return dict(version=VERSION, changed_boxes=changed, unlocatable_boxes=unlocatable,
        remaining={}, recognition_correctness_asserted=False)


def pending_geometry_pages(index: Any):
    import pymupdf
    pending = []
    prepared_tables: Any = index.tables
    for p, tables in enumerate(prepared_tables or []):
        geometry = (index.metadata.get('page_geometry') or {}).get(str(p))
        if not geometry:
            continue
        bounds = pymupdf.Rect(0, 0, geometry['width'], geometry['height']) * ~pymupdf.Matrix(geometry['matrix'])
        allowed = pymupdf.Rect(bounds.x0 - .01, bounds.y0 - .01, bounds.x1 + .01, bounds.y1 + .01)
        for table in tables:
            if any(len(item.get('bbox') or []) == 4 and (
                    pymupdf.Rect(item['bbox']).is_empty or not allowed.contains(pymupdf.Rect(item['bbox'])))
                    for item in [table, *table['cells']]):
                pending.append(p)
                break
    return pending
