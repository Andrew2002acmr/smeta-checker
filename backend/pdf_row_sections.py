"""Keep physical body dividers separate from column identity and unit proof."""
from copy import deepcopy
import re

VERSION='physical-row-sections-v1'


def separate_row_sections(table):
    """Reclassify only repeated full-width dividers below verified unit leaves."""
    if table.get('row_section_repair',{}).get('version')==VERSION:
        return False
    from pdf_table_verification import verified_column_unit
    from normative_parameters import number
    cells=table.get('cells',[])
    by_id={c['cell_id']:c for c in cells}
    proofs=[p for p in (table.get('column_units') or {}).values()
        if any(c.get('role')=='cell' and verified_column_unit(table,c,p) for c in cells)]
    if len({p['column'] for p in proofs})<2:
        return False
    unit_bottom=max(by_id[p['header_id']]['row']+by_id[p['header_id']].get('rowspan',1) for p in proofs)
    numeric_rows={c['row'] for c in cells if c['row']>=unit_bottom and c.get('colspan',1)==1
        and number(c.get('text','')) is not None}
    possible=sorted((c for c in cells if c['row']>=unit_bottom and c['column']==0
        and c.get('colspan',1)==table.get('col_count') and c.get('rowspan',1)==1
        and number(c.get('text','')) is None),key=lambda c:c['row'])
    dividers=[c for i,c in enumerate(possible) if any(c['row']<r<(
        possible[i+1]['row'] if i+1<len(possible) else table['row_count']) for r in numeric_rows)]
    if len(dividers)<2 or not any(c.get('text','').strip() for c in dividers):
        return False
    # Do not reinterpret a title above the actual unit-bearing columns.
    first=dividers[0]['row']
    if any(c.get('role')=='header' and c['row']>first and c.get('colspan',1)<table['col_count'] for c in cells):
        return False
    ids={c['cell_id'] for c in dividers}
    previous=table.get('header_rows',0)
    table['header_rows']=min(previous,first)
    sections=[]
    for i,c in enumerate(dividers):
        c['role']='group'
        sections.append(dict(cell_id=c['cell_id'],row=c['row'],start_row=c['row']+1,
            end_row=dividers[i+1]['row'] if i+1<len(dividers) else table['row_count'],
            text=c['text'],bbox=deepcopy(c['bbox']),state='literal' if c['text'].strip() else 'unresolved'))
    total_rows={c['row'] for c in cells if c['column']==0 and re.fullmatch(r'\s*Всего\s*[:.]?\s*',c['text'],re.I)
        and c['row']==max(numeric_rows,default=-1)}
    for c in cells:
        c['header_ids']=[key for key in c.get('header_ids',[]) if key not in ids]
        if c['cell_id'] in ids or c['row']<first:
            continue
        if c['row'] in total_rows:
            c['row_scope']=dict(state='total_without_section',section_id=None)
            continue
        section=next((s for s in sections if s['start_row']<=c['row']<s['end_row']),None)
        if section:
            c['row_scope']=dict(state=section['state'],section_id=section['cell_id'])
    table['row_sections']=sections
    table['row_section_repair']=dict(version=VERSION,previous_header_rows=previous,
        current_header_rows=table['header_rows'],divider_ids=[c['cell_id'] for c in dividers],
        unscoped_total_rows=sorted(total_rows),reason='full_width_body_dividers_below_verified_units')
    return True


def row_scope_context(table, cell):
    """Expose literal row labels separately; blank dividers provide no invented scope."""
    scope=cell.get('row_scope') or {}
    if scope.get('state')=='literal':
        section=next((s for s in table.get('row_sections',[]) if s['cell_id']==scope.get('section_id')),None)
        if section and section['text'].strip():
            return 'Раздел строк: '+section['text'].strip()
    if scope.get('state')=='total_without_section':
        label=next((c['text'] for c in table['cells'] if c['row']==cell['row'] and c['column']==0),'')
        return 'Строка итога: '+label if label else ''
    return ''
