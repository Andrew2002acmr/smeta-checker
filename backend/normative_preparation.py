"""CPU-only refresh of a document corpus; matching snapshots stay immutable."""
from uuid import UUID
from contextlib import closing


def repair_table_index(document, prepared, *, pages=None, store_image=None, progress=None,
                       refine_existing=False):
    """Rebuild physical tables into a detached index; no database/model changes.

    Selection is document-based, never based on an expected VOR quantity. The
    original snapshot, page text and matching results remain available intact.
    """
    from copy import deepcopy
    import hashlib
    import json
    import re
    from audit_core import PageTextIndex
    from pdf_ruled_recovery import recover_page, VERSION as recovery_version
    from pdf_table_verification import verify_recovered_tables, VERSION as verification_version

    if len(document) != len(prepared.texts):
        raise ValueError('Document and prepared page counts differ')
    tables = deepcopy(list(prepared.tables or [() for _ in prepared.texts]))
    texts = list(prepared.texts)
    rows = deepcopy(list(prepared.table_rows or [() for _ in prepared.texts]))
    locators = deepcopy(list(prepared.locators or [() for _ in prepared.texts]))
    metadata = deepcopy(prepared.metadata)
    if pages is None:
        pages = [i+1 for i, page_tables in enumerate(tables)
            if any(t.get('origin') == 'ocr' for t in page_tables)
            and re.search(r'ведомост|всего|итого', prepared.texts[i]+' '+
                ' '.join(c['text'] for t in page_tables for c in t['cells']), re.I)]
    if any(type(p) is not int or not 1 <= p <= len(document) for p in pages):
        raise ValueError('Invalid physical page number')
    report = dict(version=recovery_version, verification=verification_version,
        selected_pages=sorted(set(pages)), pages=[], model_calls=0)
    for physical in report['selected_pages']:
        index = physical-1
        if progress:
            progress(dict(page=physical, stage='geometry'))
        rotation = int((prepared.quality.get(index) or {}).get('rotation', 0))
        previous_tables = deepcopy(tables[index]) if refine_existing else None
        recovered = recover_page(document[index], tables[index], rotation=rotation)
        details = dict(page=physical, changed=recovered['changed'], diagnostics=recovered['diagnostics'])
        if recovered['changed'] or (refine_existing and any(t.get('recovery') for t in recovered['tables'])):
            new_tables = recovered['tables']
            independent_words = [dict(text=c['text'], bbox=c['bbox'], conf=c.get('confidence', 0),
                source_id=c.get('source_id', ''), crop=c.get('crop', '')) for c in locators[index]
                if c.get('origin') == 'ocr' and not c.get('table_id') and c.get('role') in {'word', 'cell'}
                and len(c.get('bbox') or []) == 4]
            verify_recovered_tables(document[index], new_tables, rotation=rotation,
                store_image=store_image, seconds=120, max_headers=64, max_numbers=32,
                independent_words=independent_words)
            details['changed'] = recovered['changed'] or new_tables != previous_tables
            tables[index] = new_tables
            flattened, located = [], []
            for table in new_tables:
                by_id = {c['cell_id']:c for c in table['cells']}
                for row in range(table['row_count']):
                    flattened.append(' | '.join(next((c['text'] for c in table['cells']
                        if c['row']==row and c['column']==column), '') for column in range(table['col_count'])))
                for cell in table['cells']:
                    locator = dict(cell, document_id='pdf-'+metadata['pdf_sha256'][:12], page=physical,
                        table_id=table['table_id'], row_id=f"{table['table_id']}:r{cell['row']+1}",
                        column_id=f"{table['table_id']}:c{cell['column']+1}", rotation=0,
                        group_id=next((f"{table['table_id']}:r{by_id[key]['row']+1}"
                            for key in cell.get('header_ids', []) if by_id[key]['role']=='group'), ''))
                    identity = [metadata['pdf_sha256'], recovery_version, physical, table['table_id'], cell]
                    locator['source_id'] = 'repaired-'+hashlib.sha256(json.dumps(identity,
                        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                    located.append(locator)
            # Non-table page text locators belong to the same immutable PDF.
            located.extend(deepcopy(c) for c in locators[index] if not c.get('table_id'))
            # prepare_page appends its table extraction to the page text. Replace
            # that exact suffix so search cannot keep using the old merged grid.
            old_tail = '\n'.join(rows[index])
            new_tail = '\n'.join(flattened)
            if old_tail and texts[index].endswith('\n'+old_tail):
                texts[index] = texts[index][:-len(old_tail)] + new_tail
            elif texts[index] == old_tail:
                texts[index] = new_tail
            else:
                texts[index] += '\nВосстановленные таблицы страницы:\n'+new_tail
            rows[index], locators[index] = flattened, located
            details.update(tables=len(new_tables), cells=sum(len(t['cells']) for t in new_tables),
                verified_numbers=sum(bool(c.get('numeric_verified')) for t in new_tables for c in t['cells']),
                verified_unit_columns=sum(len(t.get('column_units') or {}) for t in new_tables))
        report['pages'].append(details)
        if progress:
            progress(dict(details, stage='complete'))
    if not any(p['changed'] for p in report['pages']):
        return deepcopy(prepared), report
    metadata['table_recovery'] = dict(version=recovery_version, verification=verification_version,
        parent_corpus_id=prepared.to_snapshot()['corpus_id'],
        pages=[p['page'] for p in report['pages'] if p['changed']])
    updated = PageTextIndex(texts, prepared.errors, prepared.pending_ocr, rows,
        locators=locators, quality=deepcopy(prepared.quality), metadata=metadata,
        repeated_lines=prepared.repeated_lines, tables=tables)
    return updated, report


def refresh_document(context, run_id, index, *, max_pages=3):
    import pymupdf
    from audit_snapshot import load_snapshot, store_snapshot
    from pdf_text import PDFTextSource
    from normative_retrieval import DocumentSearch
    import normative_service
    record = context.repository.load_run(run_id)
    position = normative_service.locate_position(context, record, index)
    job = context.repository.load_position_job(run_id, index)
    name = job.payload['pdf_name']
    version = (record['config'].get('pdf_versions') or {})[name]
    ref = context.repository.load_version(UUID(version))
    old = load_snapshot(context.blob_store, job.payload['snapshot'])
    # This is a document preparation pass: prioritise quantity schedules,
    # irrespective of the wording of the first VOR item used to open it.
    hits = DocumentSearch(old.texts).search('ведомость площадь объем количество', tables=True, limit=max_pages)
    targets = [h['page']-1 for h in hits]
    with pymupdf.open(stream=context.blob_store.get(ref.blob_key),filetype='pdf') as doc:
        with closing(PDFTextSource(doc)) as source:
            prepared = source.for_position(targets, position.work_name, '', position.reference,
                position.comment, max_ocr_pages=max_pages)
            frozen = store_snapshot(context.blob_store, prepared)
    corpora = dict(record['config'].get('normative_corpora') or {})
    corpora[version] = frozen
    context.repository.update_run_config(run_id, {'normative_corpora':corpora})
    return dict(version_id=version, pages=[p+1 for p in targets], corpus_id=frozen['corpus_id'])


def refine_prepared(context, run_id, index, *, max_pages=3):
    """Add physical-cell OCR proof to a NEW shared snapshot; no matching edits."""
    from copy import deepcopy
    import pymupdf
    from audit_snapshot import load_snapshot, store_snapshot
    from normative_retrieval import DocumentSearch
    from pdf_cell_ocr import refine_tables, VERSION
    if not 1 <= max_pages <= 5:
        raise ValueError('Invalid bounded refinement page count')
    record = context.repository.load_run(run_id)
    job = context.repository.load_position_job(run_id, index)
    version = (record['config'].get('pdf_versions') or {})[job.payload['pdf_name']]
    corpora = dict(record['config'].get('normative_corpora') or {})
    prepared = deepcopy(load_snapshot(context.blob_store,corpora.get(version) or job.payload['snapshot']))
    ref = context.repository.load_version(UUID(version))
    hits = DocumentSearch(prepared.texts).search('ведомость площадь объем количество',tables=True,limit=max_pages)
    pages = []
    with pymupdf.open(stream=context.blob_store.get(ref.blob_key),filetype='pdf') as doc:
        for hit in hits:
            p = hit['page']-1
            tables = [t for t in (prepared.tables[p] if prepared.tables else ()) if t.get('origin') == 'ocr']
            if not tables:
                continue
            rotation = (prepared.quality.get(p) or {}).get('rotation',0)
            refine_tables(doc[p],tables,rotation=int(rotation),store_image=context.blob_store.put)
            pages.append(p+1)
            cells = {(t['table_id'],c['cell_id']):c for t in tables for c in t['cells']}
            for locator in prepared.locators[p] if prepared.locators else ():
                cell = cells.get((locator.get('table_id'),locator.get('cell_id')))
                if cell:
                    for key in ('numeric_verified','numeric_refinement','refinement_error'):
                        if key in cell:
                            locator[key] = cell[key]
    prepared.metadata['cell_refinement'] = dict(version=VERSION,pages=pages)
    frozen = store_snapshot(context.blob_store,prepared)
    corpora[version] = frozen
    context.repository.update_run_config(run_id,{'normative_corpora':corpora})
    return dict(version_id=version,pages=pages,corpus_id=frozen['corpus_id'])
