from copy import deepcopy

import pymupdf

from normative_table_candidates import grouped_totals
from pdf_cell_ocr import column_unit
from pdf_table_verification import (
    SEMANTIC_VERSION, VERSION, _ordered_headers, verified_header_text, verify_recovered_tables,
)


def word(text, conf=96):
    return dict(text=text, conf=conf, left=20, top=20, width=40, height=20)


def table():
    cells=[]
    units={}
    for base,side,height in [(0,'Справа','h=0,24m'),(2,'Слева','')]:
        cells.extend([
            dict(cell_id=f's{base}',role='header',row=0,column=base,colspan=2,
                text=side,bbox=[base*25,0,(base+2)*25,10],header_ids=[]),
            dict(cell_id=f'm{base}',role='header',row=1,column=base,colspan=2,
                text='Щебеночно-песчаная смесь фр.0/31,5',bbox=[base*25,10,(base+2)*25,20],header_ids=[]),
            dict(cell_id=f'h{base}',role='header',row=2,column=base,colspan=2,
                text=height,bbox=[base*25,20,(base+2)*25,30],header_ids=[]),
        ])
        for c in (base,base+1):
            unit='м²' if c%2==0 else 'м³'
            header=dict(cell_id=f'u{c}',role='header',row=3,column=c,colspan=1,
                text=unit,bbox=[c*25,30,(c+1)*25,40],header_ids=[])
            cells.append(header)
            units[str(c)]=dict(version=VERSION,column=c,header_id=header['cell_id'],bbox=header['bbox'],
                unit='м2' if c%2==0 else 'м3',variants=[dict(words=[word(unit)])]*2)
            cells.append(dict(cell_id=f'v{c}',role='cell',row=5,column=c,colspan=1,
                text=['345.77','82.9848','85.841','20.60184'][c],bbox=[c*25,50,(c+1)*25,60],
                header_ids=[f's{base}',f'm{base}',*([f'h{base}'] if height else []),f'u{c}'],numeric_verified=True))
    cells.append(dict(cell_id='total',role='cell',row=5,column=4,colspan=1,text='Всего:',
        bbox=[100,50,120,60],header_ids=[]))
    return dict(table_id='t',recovery={'version':'test'},header_rows=4,col_count=5,cells=cells,column_units=units)


def fake_ocr(monkeypatch, readings):
    import ocr_engine
    import pdf_cell_ocr
    values=iter(readings)
    monkeypatch.setattr(ocr_engine,'run_tsv',lambda *a,**k:'')
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda *a:next(values))
    monkeypatch.setattr(ocr_engine,'tesseract_version',lambda:'synthetic-test')
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image:image)
    monkeypatch.setattr(pdf_cell_ocr,'refine_tables',lambda *a,**k:None)


def test_empty_merged_thickness_crop_restores_exact_symmetric_candidate(monkeypatch):
    t=table()
    before=deepcopy(t['column_units'])
    assert grouped_totals(t,'Щебеночно-песчаная смесь 0/31,5',unit='м2',height_cm='24')==[]
    fake_ocr(monkeypatch,[[word('h=0,24m')]]*2)
    with pymupdf.open() as doc:
        page=doc.new_page(width=150,height=100)
        verify_recovered_tables(page,[t],max_headers=1,max_numbers=0,numeric_seconds=0)
    header=next(h for h in t['cells'] if h['cell_id']=='h2')
    assert header['raw_recovered_text']==''
    assert verified_header_text(header)=='h=0,24m'
    assert header['header_refinement']['semantic_version']==SEMANTIC_VERSION
    candidates=grouped_totals(t,'Щебеночно-песчаная смесь 0/31,5',unit='м2',height_cm='24')
    assert len(candidates)==1
    assert candidates[0]['columns']==[0,2]
    assert candidates[0]['calculated_sum']=='431.611'
    assert candidates[0]['numeric_verified'] and candidates[0]['unit_verified']
    assert not candidates[0]['scope_verified']
    assert t['column_units']==before


def test_disagreeing_or_unreadable_merged_header_never_fills_missing_geometry(monkeypatch):
    for readings in [ [[word('h=0,24m')],[word('h=0,20m')]], [[word('h=0,24m',60)]]*2 ]:
        t=table()
        fake_ocr(monkeypatch,readings)
        with pymupdf.open() as doc:
            verify_recovered_tables(doc.new_page(width=150,height=100),[t],max_headers=1,max_numbers=0,numeric_seconds=0)
        header=next(h for h in t['cells'] if h['cell_id']=='h2')
        assert header['text']=='' and verified_header_text(header)==''
        assert grouped_totals(t,'Щебеночно-песчаная смесь 0/31,5',unit='м2',height_cm='24')==[]


def test_literal_unit_from_merged_header_is_not_a_column_unit(monkeypatch):
    t=table()
    t['cells']=[c for c in t['cells'] if c['cell_id'] in {'h2','v2','v3'}]
    t['cells'][1]['header_ids']=['h2']
    t['cells'][2]['header_ids']=['h2']
    t['column_units']={}
    fake_ocr(monkeypatch,[[word('м²')]]*2)
    with pymupdf.open() as doc:
        verify_recovered_tables(doc.new_page(width=150,height=100),[t],max_headers=1,max_numbers=0,numeric_seconds=0)
    assert t['cells'][0]['text']=='м²'
    assert not t['column_units']
    assert column_unit(t,t['cells'][1])==column_unit(t,t['cells'][2])==''


def test_semantic_proof_cannot_move_or_rewrite_a_header(monkeypatch):
    t=table()
    fake_ocr(monkeypatch,[[word('h=0,24m')]]*2)
    with pymupdf.open() as doc:
        verify_recovered_tables(doc.new_page(width=150,height=100),[t],max_headers=1,max_numbers=0,numeric_seconds=0)
    header=next(h for h in t['cells'] if h['cell_id']=='h2')
    assert verified_header_text(header)
    for key,value in [('bbox',[0,20,50,30]),('column',0),('row',1),('colspan',1),('cell_id','h0'),('text','h=0,20m')]:
        assert verified_header_text({**header,key:value})==''
    bad=deepcopy(header)
    bad['header_refinement']['variants'][1]['scale']=1
    assert verified_header_text(bad)==''


def test_unit_leaves_still_precede_merged_semantics_under_budget():
    order=_ordered_headers(table())
    assert [c['cell_id'] for c in order[:4]]==['u0','u1','u2','u3']
    assert order[4]['cell_id']=='h2'  # Empty geometry at the deepest merged level.
