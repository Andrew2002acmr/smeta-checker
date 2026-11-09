"""Resolve exact table addresses from registered project source handles."""


def resolve(tools, request):
    sources = {source['id']: source for source in tools.sources}
    source = sources.get(request.source_id)
    if source is None:
        return {'error': 'unknown_table_source'}
    if source.get('kind') != 'project':
        return {'error': 'table_source_requires_project_evidence',
                'read_request': dict(tool='read_source', source_id=request.source_id)}

    operands = [source]
    if source.get('origin') == 'calculated_from_cells':
        saved = next((row for row in getattr(tools, 'quantity_sums', [])
                      if row['source_id'] == request.source_id), None)
        if not saved:
            return {'error': 'table_source_operands_unavailable'}
        operands = [sources.get(sid) for sid in saved['operand_source_ids']]
    addresses = set()
    for operand in operands:
        anchor = (operand or {}).get('source_anchor') or {}
        if (not operand or operand.get('kind') != 'project'
                or not anchor.get('table_id') or not anchor.get('cell_id')
                or not anchor.get('version_id') or type(anchor.get('page')) is not int
                or anchor['version_id'] != operand.get('version_id') or anchor['page'] != operand.get('page')
                or anchor.get('state') in {'invalid_geometry', 'source_geometry_requires_review'}):
            return {'error': 'table_source_has_no_physical_cell'}
        addresses.add((anchor['version_id'], anchor['page'], anchor['table_id']))
    if len(addresses) != 1:
        return {'error': 'table_source_spans_multiple_tables'}
    version, page, table = addresses.pop()
    if (source.get('version_id') != version or source.get('page') != page
            or request.document_version_id and request.document_version_id != version
            or 'page' in request.model_fields_set and request.page != page
            or request.table_id and request.table_id != table):
        return {'error': 'table_source_address_conflict'}
    target = tools if version == tools.version else getattr(tools, 'additional_documents', {}).get(version)
    if target is None or target.index is None or not 1 <= page <= len(target.index.texts):
        return {'error': 'unknown_table_source_document'}
    prepared = next((item for item in target._tables(page) if item['table_id'] == table), None)
    wanted = {operand['source_anchor']['cell_id'] for operand in operands}
    cells = [cell for cell in (prepared or {}).get('cells', []) if cell['cell_id'] in wanted]
    if {cell['cell_id'] for cell in cells} != wanted:
        return {'error': 'table_source_cells_unavailable'}
    return request.model_copy(update=dict(
        document_version_id=version, page=page, table_id=table,
        row_ids=request.row_ids or sorted({cell['row'] for cell in cells}),
        column_ids=request.column_ids or sorted({cell['column'] for cell in cells}),
    ))
