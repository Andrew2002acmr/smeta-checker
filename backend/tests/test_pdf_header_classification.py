from copy import deepcopy

import pymupdf
from PIL import Image,ImageDraw

from pdf_tables import finish_table,physical_header_rows
from pdf_table_verification import (
    VERSION,_ordered_headers,plain_m_clearance,plain_m_proof,reclassify_recovered_headers,
    unit_region,verified_column_unit,verify_recovered_tables,
)


def word(text,conf=95,x=0,y=0,width=20,height=20):
    return dict(text=text,conf=conf,left=x,top=y,width=width,height=height)


def lawn_table():
    raw=[(0,0,4,1,'Газон'),(1,0,1,2,'ПК+'),(1,1,1,2,'Расстояние, м'),(1,2,2,1,'Слой'),
        (2,2,1,1,'Площадь, м²'),(2,3,1,1,'Объём, м³'),
        (3,0,1,1,'ПКО+00,00 -ПКО+22,43'),(3,1,1,1,''),(3,2,1,1,''),(3,3,1,1,'6,222'),
        (4,0,1,1,'ПК 0+22,43 - ПК 1+76,37'),(4,1,1,1,'153,94'),(4,2,1,1,'319,91'),(4,3,1,1,'63,982')]
    cells=[dict(row=r,column=c,colspan=cs,rowspan=rs,text=text,bbox=[c*20,r*20,(c+cs)*20,(r+rs)*20],
        words=[],numeric_verified=False) for r,c,cs,rs,text in raw]
    table=finish_table(cells,table_id='lawn',page=1,bbox=[0,0,80,100],origin='ocr',header_rows=4)
    table['recovery']={'version':'test'}
    return table


def test_station_and_one_readable_amount_start_body_even_with_empty_ocr_cells():
    table=lawn_table()
    assert physical_header_rows(table['cells'])==3
    table['cells'][6]['text']='Толщина 0,2 м'
    assert physical_header_rows(table['cells'])==4


def test_numeric_superscript_ocr_in_a_header_does_not_outrank_literal_station_data():
    table=lawn_table()
    table['cells'][4]['text']='2'
    table['cells'][5]['text']='3'
    assert physical_header_rows(table['cells'])==3
    assert table['cells'][5] in _ordered_headers(table)
    assert physical_header_rows([dict(row=0,text='Площадь, м2'),dict(row=1,text='5'),dict(row=1,text='10')])==1


def test_existing_grid_reclassifies_body_without_changing_geometry_or_valid_unit_proof():
    table=lawn_table()
    header=table['cells'][5]
    proof=dict(version=VERSION,column=3,header_id=header['cell_id'],bbox=header['bbox'],unit='м3',
        variants=[dict(words=[word('Объём,'),word('м³')])]*2)
    table['column_units']={'3':proof}
    boxes=[c['bbox'][:] for c in table['cells']]
    reclassify_recovered_headers(table)
    assert table['header_rows']==3
    assert boxes==[c['bbox'] for c in table['cells']]
    assert all(c['role']=='cell' for c in table['cells'] if c['row']==3)
    assert verified_column_unit(table,table['cells'][9],proof)=='м3'
    assert table['column_units']['3']==proof
    assert table['cells'][9]['cell_id'] not in table['cells'][-1]['header_ids']


def test_numeric_body_cannot_override_or_validate_as_a_unit_header():
    table=lawn_table()
    data=table['cells'][9]
    assert data not in _ordered_headers(table)
    proof=dict(version=VERSION,column=3,header_id=data['cell_id'],bbox=data['bbox'],unit='м3',
        variants=[dict(words=[word('м³')])]*2)
    assert verified_column_unit(table,table['cells'][-1],proof)==''
    table['column_units']={'3':proof}
    reclassify_recovered_headers(table)
    assert table['column_units']=={}


def plain_proof(image,words):
    return dict(variants=[dict(words=words)]*2),dict(image_size=list(image.size),
        region=unit_region(words,*image.size),plain_m_clearance=plain_m_clearance(image,words),
        variants=[dict(height=48,words=[word('М',92)]),dict(height=64,words=[word('М',85)])])


def test_plain_metres_need_two_reads_and_no_unread_superscript_ink():
    clean=Image.new('L',(150,100),255)
    draw=ImageDraw.Draw(clean)
    draw.line([(30,80),(35,50),(47,70),(60,50),(65,80)],fill=0,width=4)
    words=[word('Ширина,'),word('м',78,x=50,y=70,width=35,height=30)]
    proof,crop=plain_proof(clean,words)
    assert plain_m_proof(proof,crop)
    # OCR missed this actual superscript, but its ink still forbids plain m.
    draw.line([(75,30),(82,25),(87,30),(75,42),(88,42)],fill=0,width=3)
    proof,crop=plain_proof(clean,words)
    assert len(crop['plain_m_clearance']['components'])==2
    assert not plain_m_proof(proof,crop)
    words[-1]['text']='м?'
    assert plain_m_clearance(clean,words) is None


def test_plain_metres_do_not_come_from_a_thickness_value_or_weak_reads():
    clean=Image.new('L',(150,100),255)
    ImageDraw.Draw(clean).rectangle([30,50,65,80],fill=0)
    words=[word('Ширина,'),word('м',78,x=50,y=70,width=35,height=30)]
    proof,crop=plain_proof(clean,words)
    crop['variants'][0]['words'][0]['conf']=79
    assert not plain_m_proof(proof,crop)
    words[0]['text']='h=0,20'
    assert plain_m_clearance(clean,words) is None


def test_unit_proposal_may_have_ocr_quote_noise_but_crop_includes_literal_superscript():
    words=[word('м”',76,x=886,y=201,width=62,height=55),word('3',39,x=967,y=155,width=35,height=48)]
    assert unit_region(words,1400,280)==[862,131,986,240]
    assert plain_m_clearance(Image.new('L',(1400,280),255),words) is None


def test_larger_unit_reads_keep_failed_attempts_and_reject_conflicting_unit():
    table=lawn_table()
    header=table['cells'][5]
    words=[word('м”',76,x=886,y=201,width=62,height=55),word('3',39,x=967,y=155,width=35,height=48)]
    attempts=[dict(height=48,words=[word('3',88),word('м',83)]),
        dict(height=64,words=[word('mM?',72)]),dict(height=96,words=[word('м3',94)]),
        dict(height=128,words=[word('м3',95)])]
    proof=dict(version=VERSION,column=3,header_id=header['cell_id'],bbox=header['bbox'],unit='м3',
        variants=[dict(words=words)]*2,unit_crop=dict(region=unit_region(words,1400,280),image_size=[1400,280],
            variants=attempts[2:],attempts=attempts))
    assert verified_column_unit(table,table['cells'][-1],proof)=='м3'
    attempts[0]['words']=[word('м2',96)]
    assert verified_column_unit(table,table['cells'][-1],proof)==''
    del proof['unit_crop']['attempts']
    assert verified_column_unit(table,table['cells'][-1],proof)==''


def test_incremental_verification_preserves_valid_proof_and_reserves_numeric_budget(monkeypatch):
    import ocr_engine
    import pdf_cell_ocr
    called=[]
    monkeypatch.setattr(ocr_engine,'run_tsv',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No new OCR')))
    monkeypatch.setattr(pdf_cell_ocr,'refine_tables',lambda *a,**k:called.append(k))
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=100)
        header=dict(cell_id='h',role='header',row=0,column=0,colspan=1,text='м²',bbox=[0,0,80,20],header_ids=[])
        cell=dict(cell_id='v',role='cell',row=1,column=0,colspan=1,text='17',bbox=[0,20,80,40],header_ids=['h'])
        proof=dict(version=VERSION,column=0,header_id='h',bbox=header['bbox'],unit='м2',
            variants=[dict(words=[word('м²')])]*2)
        table=dict(cells=[header,cell],recovery={'version':'test'},column_units={'0':proof},
            header_verification={'headers_attempted':5})
        original=deepcopy(proof)
        verify_recovered_tables(page,[table],seconds=0,numeric_seconds=45,max_headers=1)
        assert table['column_units']['0']==original
        assert table['header_verification']['headers_attempted']==5
        assert table['header_verification']['headers_skipped_verified']==1
        assert called[0]['seconds']==45
