from copy import deepcopy

import pymupdf

from pdf_table_verification import (
    VERSION, header_consensus, header_unit, verified_column_unit, verify_recovered_tables, unit_region,
    verify_page_numbers,
)
from pdf_cell_ocr import column_unit


def word(text, conf=96, x=0, y=0, width=20, height=20):
    return dict(text=text, conf=conf, left=x, top=y, width=width, height=height)


def test_header_consensus_rejects_conflict_and_unreadable_numeric_text():
    variants=[dict(words=[word('Площадь,'),word('м²')])]*2
    assert header_consensus(variants)=='Площадь, м²'
    assert header_consensus(variants[:1]) is None
    assert header_consensus([variants[0],dict(words=[word('Объём,'),word('м³')])]) is None
    assert header_consensus([dict(words=[word('ЩПС'),word('0/31,5',50)])]*2) is None


def test_literal_units_stay_in_their_header_and_are_not_thickness_or_material_codes():
    assert header_unit([word('Покрытие'),word('А5Вл,'),word('м²'),word('(5см)')])=='м2'
    assert header_unit([word('Основание'),word('ЩПС'),word('0/31,5,'),word('м³'),word('(20см)')])=='м3'
    assert header_unit([word('БР'),word('100.30.18,'),word('м')])=='м'
    assert header_unit([word('БР'),word('100.20.8')]) is None
    assert header_unit([word('Слой'),word('h=0,20'),word('м')]) is None
    assert header_unit([word('Основание'),word('м?')]) is None
    assert header_unit([word('Основание'),word('m7')]) is None
    assert header_unit([word('Основание'),word('м²',60)]) is None
    assert header_unit([word('Площадь'),word('м²'),word('Объем'),word('м³')]) is None


def test_superscript_must_be_geometrically_attached():
    base=word('м',x=20,y=15,width=20,height=30)
    power=word('2',x=42,y=5,width=10,height=18)
    assert header_unit([word('Площадь'),base,power])=='м2'
    assert header_unit([word('Площадь'),base,{**power,'left':100}]) is None
    assert header_unit([word('Площадь'),base,{**power,'top':30}]) is None


def table():
    header=dict(cell_id='h',role='header',row=0,column=0,colspan=1,text='Площадь, м2',
                bbox=[0,0,100,25],header_ids=[])
    cell=dict(cell_id='c',role='cell',row=1,column=0,colspan=1,text='17',bbox=[0,25,100,50],header_ids=['h'])
    return dict(table_id='t',recovery={'version':'test'},cells=[header,cell],col_count=1)


def proof():
    return dict(version=VERSION,column=0,header_id='h',bbox=[0,0,100,25],unit='м2',
                variants=[dict(words=[word('Площадь,'),word('м²')])]*2)


def test_unit_proof_is_tied_to_exact_physical_column_and_crop():
    t=table()
    p=proof()
    cell=t['cells'][1]
    t['column_units']={'0':p}
    assert verified_column_unit(t,cell,p)=='м2'
    assert column_unit(t,cell)=='м2'
    for field,value in [('column',1),('header_id','other'),('bbox',[100,0,200,25]),('unit','м3')]:
        bad={**p,field:value}
        assert verified_column_unit(t,cell,bad)==''
    cell['colspan']=2
    assert verified_column_unit(t,cell,p)==''
    cell['colspan']=1
    t['cells'][0]['column']=1
    assert verified_column_unit(t,cell,p)==''
    t['cells'][0]['column']=0
    t['cells'][0]['colspan']=2
    assert verified_column_unit(t,cell,p)==''


def test_failed_crop_proof_cannot_fall_back_to_its_own_unverified_ocr_text():
    t=table()
    t['cells'][0]['header_refinement']=proof()
    assert column_unit(t,t['cells'][1])==''


def test_recovered_raw_header_without_unit_proof_never_supplies_metres():
    t=table()
    for text in ['м?', 'Площадь, м²', 'БР100.30.18, м']:
        t['cells'][0]['text']=text
        assert column_unit(t,t['cells'][1])==''
    # Existing native/legacy behaviour is unaffected by the recovery guard.
    del t['recovery']
    assert column_unit(t,t['cells'][1])=='м'


def test_one_header_budget_reads_unit_leaf_before_material_header(monkeypatch):
    import ocr_engine
    import pdf_cell_ocr
    monkeypatch.setattr(ocr_engine,'run_tsv',lambda *a,**k:'')
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda *a:[word('Площадь,'),word('м²')])
    monkeypatch.setattr(ocr_engine,'tesseract_version',lambda:'synthetic-test')
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image:image)
    monkeypatch.setattr(pdf_cell_ocr,'refine_tables',lambda *a,**k:None)
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=100)
        t=table()
        upper=t['cells'][0]
        upper['text']='Материал м?'
        lower={**upper,'cell_id':'unit-leaf','row':1,'text':'м²','bbox':[0,25,100,50]}
        cell=t['cells'][1]
        cell.update(row=2,bbox=[0,50,100,75],header_ids=['h','unit-leaf'])
        t['cells']=[upper,lower,cell]
        verify_recovered_tables(page,[t],max_headers=1)
        assert 'header_refinement' not in upper
        assert lower['header_refinement']['header_id']=='unit-leaf'
        assert column_unit(t,cell)=='м2'


def test_zero_header_budget_does_not_approve_a_raw_unit():
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=100)
        t=table()
        t['cells'][0]['text']='м?'
        verify_recovered_tables(page,[t],max_headers=0,max_numbers=0)
        assert 'header_refinement' not in t['cells'][0]
        assert column_unit(t,t['cells'][1])==''
        assert t['header_verification']['budget_exhausted']
        assert t['header_verification']['header_limit_reached']
        assert t['header_verification']['deferred_header_ids']==['h']


def test_unit_evidence_is_independent_of_unresolved_material_code():
    t=table()
    p=proof()
    p['variants']=[dict(words=[word('ЩПЕ',60),word('м²')]),dict(words=[word('ЩПС',60),word('м²')])]
    assert header_consensus(p['variants']) is None
    assert verified_column_unit(t,t['cells'][1],p)=='м2'
    t['column_units']={'0':{**proof(),'variants':[]}}
    assert column_unit(t,t['cells'][1])==''


def test_unit_region_clamps_to_image_and_rejects_multiple_candidates():
    words=[word('м?',x=20,y=20,width=40,height=40)]
    assert unit_region(words,40,40)==[0,0,40,40]
    assert unit_region(words+[word('м2',x=100)],200,50) is None
    assert unit_region([word('100.30.15')],200,50) is None


def test_local_unit_glyph_proof_requires_two_reads_and_own_header_region():
    t=table()
    p=proof()
    p['variants']=[dict(words=[word('м?',x=20,y=20,width=40,height=40)])]*2
    glyphs=[word('м',x=20,y=15,width=20,height=30),word('2',x=42,y=5,width=10,height=18)]
    p['unit_crop']=dict(region=[0,0,40,40],image_size=[40,40],
        variants=[dict(height=h,words=glyphs) for h in (48,64)])
    assert verified_column_unit(t,t['cells'][1],p)=='м2'
    p['unit_crop']['region']=[40,0,80,40]
    assert verified_column_unit(t,t['cells'][1],p)==''
    p['unit_crop']['region']=[0,0,40,40]
    p['unit_crop']['variants'][1]['words']=[word('м3')]
    assert verified_column_unit(t,t['cells'][1],p)==''


def test_zero_budget_preserves_page_orientation_and_input_cells():
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=200)
        page.set_rotation(270)
        t=table()
        before=deepcopy(t['cells'])
        verify_recovered_tables(page,[t],seconds=0)
        assert page.rotation==270 and t['cells']==before
        assert t['header_verification']['budget_exhausted']


def test_numeric_page_evidence_requires_independent_words_within_physical_cell():
    t=table()
    cell=t['cells'][1]
    cell.update(quality='usable',numeric_verified=False,words=[dict(text='17',confidence=.99,bbox=[20,30,40,40])])
    w=dict(text='17',conf=95,bbox=[20,30,40,40],source_id='page-word-17')
    for bad in [{**w,'bbox':[90,30,110,40]}, {**w,'text':'71'}, {**w,'conf':89},
                {**w,'table_id':'old-table'}, {**w,'role':'line'}, {**w,'origin':'native'}]:
        verify_page_numbers([t],[bad])
        assert not cell['numeric_verified']
    verify_page_numbers([t],[w,{**w,'text':'71','conf':80}])
    assert not cell['numeric_verified']
    verify_page_numbers([t],[w])
    assert cell['numeric_verified']
    assert cell['numeric_page_evidence']['words'][0]['source_id']=='page-word-17'
    verify_page_numbers([t],[])
    assert cell['numeric_verified']
