"""Complete pending header observations on saved OCR, without recognizing pages again."""
from contextlib import closing
from copy import deepcopy
from typing import Any

from audit_core import PageTextIndex
from audit_snapshot import load_json, store_json, store_snapshot


def pending_header_pages(index):
    from pdf_table_verification import needs_unit_component_pass
    pages: Any = index.tables or ()
    return [p for p, tables in enumerate(pages) if any(
        any(c.get('role') == 'header' and '\nЕдиница по Tesseract:' in c.get('text', '')
            for c in t['cells']) or
        t.get('recovery') and (not t.get('header_verification') or
        (t.get('header_verification') or {}).get('deferred_header_ids') or
        needs_unit_component_pass(t)) for t in tables)]


def complete_saved_headers(blobs, data, state, index, *, checkpoint=lambda: None,
                           commit=lambda state: None):
    from pdf_table_verification import complete_recovered_headers
    return _complete_saved_tables(blobs, data, state, index, pending_header_pages(index),
        complete_recovered_headers, 'header', checkpoint=checkpoint, commit=commit)


def complete_saved_geometry(blobs, data, state, index, *, checkpoint=lambda: None,
                            commit=lambda state: None):
    from pdf_geometry_contract import validate_tables, pending_geometry_pages
    return _complete_saved_tables(blobs, data, state, index, pending_geometry_pages(index),
        validate_tables, 'geometry', checkpoint=checkpoint, commit=commit)


def complete_saved_numbers(blobs, data, state, index, *, checkpoint=lambda: None,
                           commit=lambda state: None, pages=None):
    from pdf_numeric_preparation import complete_recovered_numbers, pending_numeric_pages
    pending = pending_numeric_pages(index)
    if pages is not None:
        pending = [p for p in pending if p + 1 in pages]
    return _complete_saved_tables(blobs, data, state, index, pending,
        complete_recovered_numbers, 'numeric', checkpoint=checkpoint, commit=commit)


def complete_saved_semantics(blobs, data, state, index, *, checkpoint=lambda: None, commit=lambda state: None):
    from functools import partial
    from pdf_header_lines import complete, pending_pages
    return _complete_saved_tables(blobs,data,state,index,pending_pages(index),
        partial(complete,load_image=blobs.get),'semantic',checkpoint=checkpoint,commit=commit)


def _complete_saved_tables(blobs, data, state, index, pending, processor, stage, *, checkpoint, commit):
    if not pending:
        return state, index
    import pymupdf
    from pdf_text import PDFTextSource
    native = load_json(blobs, state['native'])
    texts, rows = list(index.texts), list(index.table_rows)
    locators, tables = list(index.locators), list(index.tables)
    quality, errors = deepcopy(index.quality), dict(index.errors)
    updated_state: dict[str, Any] = dict(state, pages=list(state['pages']),
        **{f'{stage}_parent_snapshot': state.get(f'{stage}_parent_snapshot', state['snapshot'])})
    state = updated_state
    metadata = deepcopy(index.metadata)
    with pymupdf.open(stream=bytes(data), filetype='pdf') as doc, closing(
            PDFTextSource(doc, native_state=native, full_document=True, source_bytes=data)) as source:
        if source._pdf_hash != metadata['pdf_sha256']:
            raise ValueError('Header completion source differs from prepared PDF')
        def persist(p, current, result):
            nonlocal index
            # Use the same physical-cell projection as normal page preparation.
            old_suffix = '\n' + '\n'.join(rows[p])
            new_rows, new_locators = source._store_tables(p, current, origin='ocr')
            if rows[p] and texts[p].endswith(old_suffix):
                texts[p] = texts[p][:-len(old_suffix)] + '\n' + '\n'.join(new_rows)
            locators[p] = tuple(item for item in locators[p] if not item.get('table_id')) + new_locators
            tables[p], rows[p] = tuple(current), new_rows
            quality.setdefault(p, {})[f'table_{stage}_preparation'] = result
            incomplete_error = f'incomplete_{stage}_observation'
            error_parts = [part for part in errors.get(p, '').split('; ') if part and part != incomplete_error]
            if result['remaining']:
                error_parts.append(incomplete_error)
            if error_parts:
                errors[p] = '; '.join(error_parts)
            else:
                errors.pop(p, None)
            metadata['failed_pages'] = sorted(n + 1 for n in errors)
            if result['remaining']:
                metadata['preparation_state'] = 'complete_with_errors'
            packed = load_json(blobs, state['pages'][p])
            packed.update(text=texts[p], table_rows=list(rows[p]), locators=list(locators[p]),
                tables=list(tables[p]), quality=quality[p], errors=[errors[p]] if p in errors else [])
            state['pages'][p] = store_json(blobs, packed)
            metadata[f'{stage}_completion_version'] = 'per-table-resume-v1'
            completed = set(metadata.get(f'{stage}_completed_pages', []))
            if result['remaining']:
                completed.discard(p + 1)
            else:
                completed.add(p + 1)
            metadata[f'{stage}_completed_pages'] = sorted(completed)
            metadata['incomplete_table_headers'] = {
                str(n + 1): {t['table_id']: (t.get('header_verification') or {}).get('deferred_header_ids', [])
                    for t in ts if t.get('recovery') and (not t.get('header_verification') or
                    (t.get('header_verification') or {}).get('deferred_header_ids'))}
                for n, ts in enumerate(tables) if any(t.get('recovery') and (
                    not t.get('header_verification') or
                    (t.get('header_verification') or {}).get('deferred_header_ids')) for t in ts)}
            index = PageTextIndex(texts, errors, index.pending_ocr, rows, locators,
                tables=tables, quality=quality, metadata=metadata, repeated_lines=index.repeated_lines)
            state['snapshot'] = store_snapshot(blobs, index)
            state['failed_pages'] = metadata['failed_pages']
            commit(state)

        def after_pass(p, current, step):
            remaining = {key: ids for key, ids in step['after'].items() if ids}
            persist(p, current, dict(state=f'{stage}_observation_in_progress' if remaining else
                f'{stage}_observation_complete', remaining=remaining, passes=[step],
                recognition_correctness_asserted=False))
            checkpoint()

        for p in pending:
            checkpoint()
            current = deepcopy(list(tables[p]))
            result = processor(doc[p], current,
                rotation=int((quality.get(p) or {}).get('rotation', 0)),
                store_image=blobs.put, cache_dir=source._cache_dir,
                checkpoint=lambda updated, step, p=p: after_pass(p, updated, step))
            persist(p, current, result)
            checkpoint()
    return state, index
