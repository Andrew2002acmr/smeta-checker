"""Bounded local PDF-region acquisition; no OCR, interpretation or API calls."""

import hashlib
import math
from uuid import UUID


def render_region(tools, request):
    version = request.document_version_id or tools.version
    context = getattr(tools, 'context', None)
    record = getattr(tools, 'record', {})
    if context is None or version not in (record.get('config', {}).get('pdf_versions') or {}).values():
        return {'error': 'drawing_requires_pinned_document'}
    box = request.bbox
    if len(box) != 4 or any(not math.isfinite(v) for v in box):
        return {'error': 'invalid_drawing_region'}
    if not (0 <= box[0] < box[2] and 0 <= box[1] < box[3]):
        return {'error': 'invalid_drawing_region'}
    document = context.repository.load_version(UUID(str(version)))
    data = context.blob_store.get(document.blob_key)
    if hashlib.sha256(data).hexdigest() != document.sha256:
        return {'error': 'drawing_document_hash_mismatch'}
    import pymupdf as fitz
    with fitz.open(stream=data, filetype='pdf') as pdf:
        if not 1 <= request.page <= len(pdf):
            return {'error': 'page_out_of_range'}
        page = pdf[request.page-1]
        bounds = page.mediabox
        if box[2] > bounds.width or box[3] > bounds.height:
            return {'error': 'drawing_region_out_of_bounds'}
        # Unrotated PDF points are the same coordinate contract as source links.
        width, height = box[2]-box[0], box[3]-box[1]
        if math.ceil(width*request.scale)*math.ceil(height*request.scale) > 2_000_000:
            return {'error': 'drawing_pixel_budget_exceeded', 'max_pixels': 2_000_000}
        page.set_rotation(0)
        image = page.get_pixmap(matrix=fitz.Matrix(request.scale, request.scale),
            clip=fitz.Rect(box), alpha=False)
        png = image.tobytes('png')
    if len(png) > 2_000_000:
        return {'error': 'drawing_byte_budget_exceeded'}
    blob = context.blob_store.put(png)
    from analysis_replay import digest
    provenance = dict(version_id=str(version), pdf_sha256=document.sha256,
        page=request.page, bbox=list(box), scale=request.scale, render_sha256=blob.sha256)
    source_id = 'drawing-region:'+digest(provenance)[:32]
    source = dict(id=source_id, kind='project', version_id=str(version), page=request.page,
        label=document.filename, origin='drawing_region_unread', numeric_verified=False,
        text='Фрагмент чертежа получен. Визуальная интерпретация не выполнена; '
            'изображение не подтверждает величины или инженерную принадлежность.',
        rects=[list(box)], drawing_region=provenance, image_key=blob.key)
    registered = tools._add(source)
    tools.events.append(dict(kind='drawing_region_acquired_v1', **provenance,
        source_id=source_id, byte_size=len(png), interpretation_state='not_performed'))
    return dict(**registered, image_key=blob.key, image_sha256=blob.sha256,
        width=image.width, height=image.height, **provenance,
        interpretation_state='not_performed', requires_visual_reader=True,
        automatic_confirmation_allowed=False)
