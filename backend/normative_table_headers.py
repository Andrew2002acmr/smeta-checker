"""Reuse independently verified header crops on the exact prepared table only."""
from copy import deepcopy

from analysis_replay import digest, get, put
from pdf_table_verification import refresh_header_ancestry, verified_header_text

STAGE='verified_table_headers_v1'


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
        allowed={'text','header_refinement','raw_recovered_text'}
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


def save(context, record, version, page, table, headers):
    if version not in (record['config'].get('pdf_versions') or {}).values():
        raise ValueError('Header correction requires a pinned project PDF')
    _replace(table,headers)
    ident=identity(record['project_id'],version,page,table)
    payload=dict(headers=deepcopy(headers),provenance='independent_physical_header_ocr')
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
            result.append(table)
            continue
        if payload.get('provenance')!='independent_physical_header_ocr':
            raise ValueError('Invalid header correction provenance')
        result.append(_replace(table,payload['headers']))
        event=dict(kind=STAGE,key=digest(ident),version_id=version,page=page,
            table_id=table['table_id'],header_ids=[h['cell_id'] for h in payload['headers']])
        if event not in events:
            events.append(event)
    return result
