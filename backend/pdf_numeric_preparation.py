"""Bounded, resumable recognition of physical numeric cells before agent work."""
from pdf_cell_ocr import BODY_PREPARATION_VERSION, numeric_candidates, refine_tables, has_measurement_context
from pdf_paired_ocr import pending_cells as pending_paired_cells,verified_reading


def pending_ids(table):
    return [c['cell_id']for c in numeric_candidates(table,include_body=True,resume_only=True)]+[
        c['cell_id']for c,_ in pending_paired_cells(table)]


def pending_numeric_tables(tables):
    return [table for table in tables if (table.get('recovery')and
        numeric_candidates(table, include_body=True, resume_only=True))or pending_paired_cells(table)]


def pending_numeric_pages(index):
    return [page for page, tables in enumerate(index.tables or ()) if pending_numeric_tables(tables)]


def complete_recovered_numbers(page, tables, *, rotation=0, cache_dir=None, store_image=None,
                               passes=4, seconds=45, max_numbers=256, checkpoint=None):
    history = []
    import time
    from pdf_paired_ocr import complete as complete_pairs
    for attempt in range(passes):
        pending = pending_numeric_tables(tables)
        if not pending:
            break
        before = {table['table_id']: pending_ids(table)for table in pending}
        started=time.monotonic()
        refine_tables(page, pending, rotation=rotation, cache_dir=cache_dir, store_image=store_image,
            seconds=seconds, max_numbers=max_numbers, max_columns=0, include_body=True, resume_only=True)
        complete_pairs(page,pending,rotation=rotation,cache_dir=cache_dir,store_image=store_image,
            seconds=max(0,seconds-(time.monotonic()-started)),max_cells=max_numbers)
        after = {table['table_id']: pending_ids(table)for table in pending}
        history.append(dict(pass_number=attempt + 1, before=before, after=after))
        if checkpoint is not None:
            checkpoint(tables, history[-1])
        if before == after:
            break
    remaining = {table['table_id']:pending_ids(table)for table in pending_numeric_tables(tables)}
    uncertain = {table['table_id']: [cell['cell_id'] for cell in table['cells'] if
        (cell.get('numeric_preparation') or {}).get('version') == BODY_PREPARATION_VERSION and
        not cell.get('numeric_verified')] for table in tables if table.get('recovery')}
    return dict(state='numeric_observation_complete' if not remaining else 'incomplete_numeric_observation',
        remaining=remaining, uncertain_cells=uncertain, passes=history,
        uncertain_paired_cells={table['table_id']:[c['cell_id']for c in table['cells']
            if c.get('paired_ocr')and not verified_reading(c,c['paired_ocr']['kind'])]
            for table in tables if any(c.get('paired_ocr')for c in table['cells'])},
        other_grids=[dict(table_id=t['table_id'], reason='no_measurement_header_or_verified_unit',
            separate_semantic_analysis_required=True, absence_of_project_fact_not_proven=True)
            for t in tables if t.get('recovery') and not has_measurement_context(t)],
        recognition_correctness_asserted=False)
