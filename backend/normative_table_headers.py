"""Reuse independently verified header crops on the exact prepared table only."""
from copy import deepcopy
import hashlib
import re
from typing import Any

from analysis_replay import digest, get, put
from pdf_table_verification import refresh_header_ancestry, verified_header_text

STAGE='verified_table_headers_v1'
SCOPE_VERSION='independent_visual_row_labels_v1'


def _literal_cell(cell):
    return {k:deepcopy(cell[k]) for k in ('cell_id','row','column','colspan','text','bbox') if k in cell}


def table_scope_context(table, cell, source_id, version, page):
    """Project a prepared visual reading only when all physical cells still match.

    This supplies literal evidence; it asserts neither construction applicability
    nor the allocation of a total to any individual technological layer.
    """
    proof=table.get('visual_scope_transcription') or {}
    actual={k:v for k,v in table.items() if k!='visual_scope_transcription'}
    if (proof.get('version')!=SCOPE_VERSION or proof.get('table_sha256')!=digest(actual)
            or proof.get('version_id')!=str(version) or proof.get('page')!=page
            or cell['cell_id'] not in proof.get('total_cell_ids',[])):
        return None
    by_id={c['cell_id']:c for c in table['cells']}
    if by_id.get(cell['cell_id'])!=cell:
        return None
    labels=proof.get('row_labels',[])
    if any(label!=_literal_cell(by_id.get(label.get('cell_id'),{})) for label in labels):
        return None
    identity=proof.get('document_identity') or {}
    if (identity.get('original_sha256')!=proof.get('document_sha256')
            or identity.get('extraction_sha256')!=proof.get('extraction_sha256')):
        return None
    headers=[_literal_cell(by_id[h]) for h in cell.get('header_ids',[]) if h in by_id]
    if len(headers)!=len(cell.get('header_ids',[])):
        return None
    result=dict(version=SCOPE_VERSION,caption=proof.get('caption',''),
        source_binding=dict(source_id=source_id,version_id=str(version),page=page,
            table_id=table['table_id'],cell_id=cell['cell_id'],row=cell['row'],column=cell['column'],
            header_ids=deepcopy(cell.get('header_ids',[]))),
        body_first_row=proof.get('body_first_row'),total_row=proof.get('total_row'),
        row_labels=deepcopy(labels),header_cells=headers,total_cell=_literal_cell(cell),
        provenance=dict(table_sha256=proof['table_sha256'],actual_cells_sha256=digest(table['cells']),
            header_cells_sha256=digest(headers),row_labels_sha256=digest(labels),
            document_sha256=proof.get('document_sha256'),render_sha256=proof.get('render_sha256'),
            extraction_sha256=proof.get('extraction_sha256'),
            review_sha256=deepcopy(proof.get('review_sha256',[])),
            origin=cell.get('origin'),quality=cell.get('quality')))
    source=dict(id=source_id,kind='project',version_id=str(version),page=page,
        origin=cell.get('origin'),table_scope_context=result)
    return validated_table_scope_context(source)


def validated_table_scope_context(source):
    """Check a compact literal projection, without inferring engineering scope."""
    context=source.get('table_scope_context') or {}
    if not isinstance(context,dict):
        return None
    binding=context.get('source_binding') or {}
    provenance=context.get('provenance') or {}
    labels=context.get('row_labels') or []
    headers=context.get('header_cells') or []
    total=context.get('total_cell') or {}
    if (not all(isinstance(v,dict) for v in (binding,provenance,total))
            or not all(isinstance(v,list) for v in (labels,headers))
            or not all(isinstance(v,dict) for v in [*labels,*headers])):
        return None
    first,last=context.get('body_first_row'),context.get('total_row')
    if (context.get('version')!=SCOPE_VERSION or source.get('kind')!='project'
            or any(binding.get(k)!=source.get(s) for k,s in
                (('source_id','id'),('version_id','version_id'),('page','page')))
            or not binding.get('version_id') or type(binding.get('page')) is not int
            or provenance.get('quality')!='usable' or provenance.get('origin')!=source.get('origin')
            or provenance.get('origin') not in {'ocr','native'}
            or type(first) is not int or type(last) is not int or first<0 or last<=first
            or len(labels)!=last-first
            or [c.get('row') for c in labels]!=list(range(first,last))
            or len({c.get('cell_id') for c in labels})!=len(labels)
            or not headers or [c.get('cell_id') for c in headers]!=binding.get('header_ids')
            or any(total.get(k)!=binding.get(k) for k in ('cell_id','row','column'))
            or last!=binding.get('row')
            or provenance.get('header_cells_sha256')!=digest(headers)
            or provenance.get('row_labels_sha256')!=digest(labels)):
        return None
    for cell in [*labels,*headers,total]:
        bbox=cell.get('bbox')
        if (not isinstance(cell.get('text'),str) or not cell['text'].strip()
                or not str(cell.get('cell_id','')).startswith(str(binding.get('table_id'))+':')
                or type(cell.get('row')) is not int or type(cell.get('column')) is not int
                or not isinstance(bbox,list) or len(bbox)!=4
                or any(type(v) not in (int,float) for v in bbox)
                or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]):
            return None
    if (any(c['row']>=first or not c['column']<=total['column']<c['column']+c.get('colspan',1)
            for c in headers) or any(c['column']!=0 for c in labels)):
        return None
    hashes=[provenance.get(k) for k in ('table_sha256','actual_cells_sha256',
        'header_cells_sha256','row_labels_sha256','document_sha256','extraction_sha256','render_sha256')]
    reviews=provenance.get('review_sha256') or []
    if (not isinstance(reviews,list) or not all(isinstance(v,str) for v in reviews)
            or len(reviews)<2 or len(set(reviews))!=len(reviews)
            or any(not re.fullmatch('[0-9a-f]{64}',str(value)) for value in [*hashes,*reviews])):
        return None
    from normative_source_addresses import cell_source_id
    expected={cell_source_id(binding['version_id'],binding['page'],binding.get('table_id'),binding['cell_id'],
        envelope_version=version)for version in (0,1)}
    if binding['source_id'] not in expected:
        return None
    return deepcopy(context)


def identity(project, version, page, table):
    return dict(schema=1,stage=STAGE,project_id=str(project),version_id=str(version),
        page=page,table_id=table['table_id'],table_sha256=digest(table))


def _replace(table, headers):
    if not headers or len({h['cell_id'] for h in headers})!=len(headers):
        raise ValueError('Header corrections must be nonempty and unique')
    result=deepcopy(table)
    for header in headers:
        matches=[i for i,c in enumerate(table['cells']) if c['cell_id']==header['cell_id']]
        if len(matches)!=1:
            raise ValueError('Header is not unique in the prepared table')
        at=matches[0]
        original=table['cells'][at]
        allowed={'text','header_refinement','raw_recovered_text','visual_transcription'}
        if (original.get('role')!='header' or header.get('raw_recovered_text')!=original['text']
                or {k:v for k,v in header.items() if k not in allowed}
                !={k:v for k,v in original.items() if k not in allowed}
                or not verified_header_text(header)
                or (header.get('header_refinement') or {}).get('image_sha256')
                !=(original.get('header_refinement') or {}).get('image_sha256')):
            raise ValueError('Header correction does not match its physical crop')
        result['cells'][at]=deepcopy(header)
    refresh_header_ancestry(result)
    from pdf_row_sections import separate_row_sections
    separate_row_sections(result)
    return result


def save(context, record, version, page, table, headers, *, provenance='independent_physical_header_ocr'):
    if version not in (record['config'].get('pdf_versions') or {}).values():
        raise ValueError('Header correction requires a pinned project PDF')
    if provenance not in {'independent_physical_header_ocr','independent_visual_header_transcription'}:
        raise ValueError('Unknown header reading provenance')
    if provenance=='independent_visual_header_transcription':
        from pdf_table_verification import visual_header_text
        if any(not visual_header_text(h) or h['visual_transcription']['document_version_id']!=str(version)
                or h['visual_transcription']['page']!=page for h in headers):
            raise ValueError('Visual header reading does not match pinned page/version')
    _replace(table,headers)
    ident=identity(record['project_id'],version,page,table)
    payload=dict(headers=deepcopy(headers),provenance=provenance)
    if put(context,ident,payload)!=payload:
        raise ValueError('Different header evidence already exists; preserve history')
    return digest(ident)


def apply(context, record, version, page, tables, events):
    if version not in (record['config'].get('pdf_versions') or {}).values():
        return tables
    result=[]
    for table in tables:
        ident=identity(record['project_id'],version,page,table)
        payload=get(context,ident)
        if payload is None:
            restored = restore_visual_observation(context, record, version, page, table, events)
            result.append(restored if restored is not None else table)
            continue
        if payload.get('provenance') not in {'independent_physical_header_ocr','independent_visual_header_transcription'}:
            raise ValueError('Invalid header correction provenance')
        if payload['provenance']=='independent_visual_header_transcription' and any(
                h.get('visual_transcription',{}).get('document_version_id')!=str(version)
                or h.get('visual_transcription',{}).get('page')!=page for h in payload['headers']):
            raise ValueError('Visual header reading belongs to another page/version')
        result.append(_replace(table,payload['headers']))
        event=dict(kind=STAGE,key=digest(ident),version_id=version,page=page,
            table_id=table['table_id'],header_ids=[h['cell_id'] for h in payload['headers']])
        if event not in events:
            events.append(event)
    return result


def restore_visual_observation(context, record, version, page, table, events):
    """Reuse a reviewed physical header after unrelated table OCR changes.

    An unchanged raw header, geometry, original PDF bytes and archived crop
    are required. A different new crop or conflicting reading is never replaced.
    The old image/proof is restored intact; no proof hash is manufactured.
    """
    from uuid import UUID
    from pdf_table_verification import visual_header_text
    lookup = getattr(context.repository, 'visual_header_artifacts', None)
    if not callable(lookup):
        return None
    candidates: Any = lookup(record['project_id'], str(version), page, table['table_id'])
    if not candidates:
        return None
    document = context.repository.load_version(UUID(str(version)))
    restored = []
    for candidate in candidates:
        ident, payload = candidate['identity'], candidate['payload']
        if (ident.get('project_id') != str(record['project_id'])
                or ident.get('version_id') != str(version) or ident.get('page') != page
                or ident.get('table_id') != table['table_id']
                or payload.get('provenance') != 'independent_visual_header_transcription'):
            continue
        prepared = deepcopy(table)
        by_id = {cell['cell_id']: cell for cell in prepared['cells']}
        headers = deepcopy(payload.get('headers') or [])
        if not headers:
            continue
        valid = True
        for header in headers:
            proof = header.get('visual_transcription') or {}
            refinement = header.get('header_refinement') or {}
            cell = by_id.get(header.get('cell_id'))
            if (not visual_header_text(header) or cell is None
                    or proof.get('document_version_id') != str(version)
                    or proof.get('pdf_sha256') != document.sha256 or proof.get('page') != page
                    or any(cell.get(k) != header.get(k) for k in ('cell_id', 'bbox', 'row', 'column', 'colspan'))
                    or cell.get('text') != header.get('raw_recovered_text')
                    or not refinement.get('image_sha256')
                    or (cell.get('header_refinement') and
                        cell['header_refinement'].get('image_sha256') != refinement['image_sha256'])):
                valid = False
                break
            if not verified_observation_crop(context.blob_store, document, page, refinement):
                valid = False
                break
            cell['header_refinement'] = deepcopy(refinement)
            # OCR quality describes the new extraction, not the immutable
            # visual observation. Do not upgrade it from an older snapshot.
            if 'quality' in cell:
                header['quality'] = cell['quality']
            else:
                header.pop('quality', None)
        if not valid:
            continue
        try:
            projected = _replace(prepared, headers)
        except ValueError:
            continue
        restored.append((projected, candidate['key']))
    if not restored or len({digest(value) for value, _ in restored}) != 1:
        return None
    projected, key = restored[0]
    events.append(dict(kind='visual_header_observation_reused_v1', artifact_key=key,
        version_id=str(version), original_pdf_sha256=document.sha256, page=page,
        table_id=table['table_id'], old_table_sha256=table_scope_digest(candidates, key),
        new_raw_table_sha256=digest(table),
        basis='same_original_pdf_geometry_raw_header_and_verified_archived_crop',
        model_verdict_reused=False, ocr_requests=0))
    return projected


def table_scope_digest(candidates, key):
    return next(c['identity']['table_sha256'] for c in candidates if c['key'] == key)


def verified_observation_crop(blobs, document, page, refinement):
    """Verify an archived crop, or reproduce identical pixels without OCR."""
    expected = refinement.get('image_sha256', '')
    if not re.fullmatch('[0-9a-f]{64}', expected):
        return False
    key = refinement.get('image_key') or f'{expected[:2]}/{expected[2:4]}/{expected}'
    try:
        return hashlib.sha256(blobs.get(key)).hexdigest() == expected
    except (FileNotFoundError, KeyError):
        pass
    if refinement.get('dpi') != 600 or refinement.get('rotation') not in {0, 90, 180, 270}:
        return False
    import pymupdf
    import ocr_engine
    try:
        raw = blobs.get(document.blob_key)
        if hashlib.sha256(raw).hexdigest() != document.sha256:
            return False
        with pymupdf.open(stream=raw, filetype='pdf') as pdf, pymupdf.open() as private:
            if not 1 <= page <= len(pdf):
                return False
            private.insert_pdf(pdf, from_page=page-1, to_page=page-1, annots=False, links=False)
            render = private[0]
            render.set_rotation(0)
            box = pymupdf.Rect(refinement['bbox'])
            if (box.is_empty or box.is_infinite or box != (box & render.rect)
                    or box.width * box.height * (600 / 72) ** 2 > 4_000_000):
                return False
            pix = render.get_pixmap(dpi=600, clip=box, alpha=False)
            crop = ocr_engine._to_png(ocr_engine._rotate(ocr_engine._pil_from_pixmap(pix), refinement['rotation']))
        if hashlib.sha256(crop).hexdigest() != expected:
            return False
        saved = blobs.put(crop)
        return saved.sha256 == expected
    except (OSError, ValueError, KeyError):
        return False
