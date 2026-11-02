from copy import deepcopy
from unittest.mock import patch

from pdf_row_sections import separate_row_sections,row_scope_context


def table():
    cells=[]
    def cell(identity,row,col,text,span=1,role='cell'):
        cells.append(dict(cell_id=identity,row=row,column=col,colspan=span,rowspan=1,text=text,role=role,
            bbox=[col*20,row*10,(col+span)*20,row*10+10],origin='ocr',numeric_verified=role=='cell',
            header_ids=[] if row<2 else ['title','u1' if col==1 else 'u2','right'],quality='usable'))
    cell('title',0,0,'Ведомость',3,'header')
    cell('u1',1,1,'м2',role='header')
    cell('u2',1,2,'м2',role='header')
    cell('right',2,0,'Правая ось',3,'header')
    cell('a',3,1,'10')
    cell('b',3,2,'20')
    cell('unknown',4,0,'',3)
    cell('c',5,1,'5')
    cell('d',5,2,'7')
    cell('total',6,0,'Всего:')
    cell('e',6,1,'15')
    cell('f',6,2,'27')
    return dict(table_id='t',row_count=7,col_count=3,header_rows=3,cells=cells,
        column_units={'1':dict(header_id='u1',column=1,unit='м2'),'2':dict(header_id='u2',column=2,unit='м2')})


def apply(t):
    with patch('pdf_table_verification.verified_column_unit',return_value=True):
        return separate_row_sections(t)


def test_sections_stop_at_blank_divider_and_never_become_grand_total_headers():
    t=table()
    before=deepcopy(t)
    assert apply(t)
    cs={c['cell_id']:c for c in t['cells']}
    assert t['header_rows']==2 and cs['right']['role']=='group'
    assert all('right' not in c['header_ids'] for c in t['cells'])
    assert row_scope_context(t,cs['a'])=='Раздел строк: Правая ось'
    assert cs['c']['row_scope']==dict(state='unresolved',section_id='unknown')
    assert row_scope_context(t,cs['c'])==''
    assert cs['e']['row_scope']==dict(state='total_without_section',section_id=None)
    assert row_scope_context(t,cs['e'])=='Строка итога: Всего:'
    assert t['column_units']==before['column_units']
    for old,new in zip(before['cells'],t['cells']):
        assert all(old[k]==new[k] for k in old if k not in {'role','header_ids'})
    frozen=deepcopy(t)
    assert not apply(t) and t==frozen


def test_single_title_or_unverified_units_are_not_reinterpreted():
    t=table()
    t['cells']=[c for c in t['cells'] if c['cell_id']!='unknown']
    assert not apply(t)
    t=table()
    with patch('pdf_table_verification.verified_column_unit',return_value=False):
        assert not separate_row_sections(t)
    t=table()
    t['column_units'].pop('2')
    assert not apply(t)


def test_unit_or_material_headers_below_candidate_divider_prevent_reclassification():
    t=table()
    t['cells'].append(dict(cell_id='later',row=3,column=1,colspan=1,rowspan=1,
        text='Материал',role='header',bbox=[20,30,40,40],header_ids=[]))
    assert not apply(t)


def test_read_table_and_sum_keep_only_literal_current_row_scope():
    from test_normative_agent import toolkit
    from normative_agent import EvidenceRequest
    tools=toolkit()
    t=table()
    apply(t)
    tools.index.tables=([t],[],[])
    with patch('pdf_cell_ocr.column_unit',return_value='м2'):
        reply=tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='t',row_ids=[6],column_ids=[1,2]))
        ids=[v['source_id'] for v in reply['cells']]
        computed=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit='м2'))
    source=next(s for s in tools.sources if s['id']==computed['source_id'])
    assert 'Правая ось' not in source['text'] and 'Всего:' in source['text']
    assert computed['value']=='42' and computed['scope_verified'] is False
    assert all(r['row_scope']['state']=='total_without_section' for r in reply['rows'])
