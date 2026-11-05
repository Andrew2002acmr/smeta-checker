from copy import deepcopy

import pymupdf

from pdf_cell_ocr import clean_glyphs, column_axis, column_unit, literal_unit, numeric_agreement, refine_tables, subcolumn_box, VERSION


def word(text,confidence=96,**box):
    return dict(text=text,conf=confidence,left=10,top=10,width=20,height=20,**box)


def test_numbers_require_two_high_confidence_reads_and_independent_value_agreement():
    variants=[dict(words=[word('37,125',94)]),dict(words=[word('37,125',96)])]
    assert numeric_agreement('37.125',variants)
    assert not numeric_agreement('38.125',variants)
    assert not numeric_agreement('37.125',variants[:1])
    assert not numeric_agreement('37.125',[dict(words=[word('37,125',88)])]*3)
    assert not numeric_agreement('37.125',variants+[dict(words=[word('37125',80)])])


def test_split_unit_requires_a_letter_and_attached_superscript():
    base=dict(text='М',conf=78,left=10,top=20,width=30,height=40)
    power=dict(text='2',conf=97,left=45,top=5,width=15,height=24)
    assert literal_unit([power,base])=='м2'
    assert literal_unit([base,{**power,'text':'3'}])=='м3'
    assert literal_unit([power]) is None
    assert literal_unit([base,{**power,'top':45}]) is None
    assert literal_unit([{**base,'conf':60},power]) is None
    assert literal_unit([word('м²',77)])=='м2'


def test_raised_digit_can_match_lowercase_x_height_but_must_be_adjacent():
    # Real saved crop: exponent 20 px vs lowercase m 22 px. The digit is
    # completely above the base, despite failing the former 0.9 size ratio.
    base=dict(text='м',conf=90,left=23,top=43,width=27,height=22)
    power=dict(text='2',conf=96,left=57,top=23,width=19,height=20)
    assert literal_unit([power,base])=='м2'
    assert literal_unit([base,{**power,'text':'3'}])=='м3'
    assert literal_unit([base,{**power,'top':38}]) is None
    assert literal_unit([base,{**power,'left':130}]) is None
    assert literal_unit([base,{**power,'height':30}]) is None
    assert literal_unit([base,{**power,'conf':89}]) is None


def grid(rotated=False):
    def box(x0,y0,x1,y1):
        return [y0,x0,y1,x1] if rotated else [x0,y0,x1,y1]
    header=dict(cell_id='h',role='header',row=0,column=0,colspan=2,text='${}$',bbox=box(0,0,100,20))
    cells=[header]+[dict(cell_id=f'c{r}{col}',role='cell',row=r,column=col,colspan=1,text='12',
        bbox=box(col*50,r*20,(col+1)*50,(r+1)*20),header_ids=['h']) for r in (1,2) for col in (0,1)]
    return dict(table_id='t',row_count=3,col_count=2,cells=cells)


def test_physical_column_bounds_split_merged_header_for_both_pdf_orientations():
    for rotated in (False,True):
        table=grid(rotated)
        axis=column_axis(table)
        assert axis==int(rotated)
        left=subcolumn_box(table,table['cells'][0],0,axis)
        right=subcolumn_box(table,table['cells'][0],1,axis)
        assert left[axis+2]==right[axis]==50
        assert left[axis]==0 and right[axis+2]==100


def test_merged_header_unit_is_not_inherited_by_both_subcolumns():
    table=grid()
    table['cells'][0]['text']='Единица по Tesseract: м2'
    assert column_unit(table,table['cells'][1])==''
    def proof(unit):
        return dict(version=VERSION,unit=unit,header_id='h',variants=[dict(words=[word(unit)])]*2)
    table['column_units']={'0':proof('м2'),'1':proof('м3')}
    assert column_unit(table,table['cells'][1])=='м2'
    assert column_unit(table,table['cells'][2])=='м3'
    table['column_units']['1']['variants']=[]
    assert column_unit(table,table['cells'][2])==''


def test_raw_raster_unit_cannot_inherit_native_text_trust_or_neighbour_crop():
    table = grid()
    table['origin'] = 'ocr'
    header = table['cells'][0]
    header.update(colspan=1, text='м2')
    assert column_unit(table, table['cells'][1]) == ''
    table['origin'] = 'native'
    assert column_unit(table, table['cells'][1]) == 'м2'
    table['origin'] = 'ocr'
    header['colspan'] = 2
    proof = dict(version=VERSION, unit='м2', header_id='h',
        variants=[dict(words=[word('м2')])] * 2,
        bbox=subcolumn_box(table, header, 0, column_axis(table)))
    table['column_units'] = {'0': proof}
    assert column_unit(table, table['cells'][1]) == 'м2'
    proof['bbox'] = subcolumn_box(table, header, 1, column_axis(table))
    assert column_unit(table, table['cells'][1]) == ''


def test_border_cleanup_preserves_interior_glyphs_and_decimal_point():
    import cv2
    import numpy as np
    from PIL import Image
    image=np.full((80,220),255,dtype=np.uint8)
    cv2.rectangle(image,(0,0),(219,79),0,3)
    cv2.putText(image,'12.34',(20,52),cv2.FONT_HERSHEY_SIMPLEX,1.2,0,2)
    clean=clean_glyphs(Image.fromarray(image))
    assert clean is not None
    assert clean.width<180 and clean.height<65
    # Remaining connected components include the decimal point.
    count=cv2.connectedComponents(255-np.array(clean))[0]-1
    assert count==5


def test_refinement_budget_restores_page_rotation_without_running_ocr():
    with pymupdf.open() as pdf:
        page=pdf.new_page(width=100,height=200)
        page.set_rotation(270)
        table=grid()
        original=deepcopy(table['cells'])
        refine_tables(page,[table],seconds=0)
        assert page.rotation==270
        assert table['cells']==original
        assert table['cell_refinement']['budget_exhausted']


def test_refinement_proof_survives_immutable_snapshot_roundtrip():
    from audit_core import PageTextIndex
    from audit_snapshot import encode_snapshot,decode_snapshot
    table=grid()
    old=PageTextIndex(['текст'],tables=[[table]])
    before=old.to_snapshot()['corpus_id']
    updated=deepcopy(old)
    updated.tables[0][0]['column_units']={'0':dict(version=VERSION,unit='м2',image_sha256='sha',image_key='blob')}
    restored=decode_snapshot(encode_snapshot(updated))
    assert restored.tables[0][0]['column_units']['0']['image_key']=='blob'
    assert old.to_snapshot()['corpus_id']==before
    assert restored.to_snapshot()['corpus_id']!=before
