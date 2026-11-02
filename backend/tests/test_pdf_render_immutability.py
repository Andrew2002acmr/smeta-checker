"""Crop rendering must not rewrite the source page's inherited /Rotate key."""
from copy import deepcopy

import pymupdf
import pytest

from pdf_cell_ocr import refine_tables
from pdf_table_verification import verify_recovered_tables
from pdf_ruled_recovery import recover_page


def sample_table():
    return dict(table_id='t',recovery={'version':'test'},row_count=2,col_count=2,cells=[
        dict(cell_id='h',role='header',row=0,column=1,colspan=1,text='м2',bbox=[50,0,100,25],header_ids=[]),
        dict(cell_id='label',role='cell',row=1,column=0,colspan=1,text='Всего:',bbox=[0,25,50,50],header_ids=[]),
        dict(cell_id='n',role='cell',row=1,column=1,colspan=1,text='17',bbox=[50,25,100,50],
            quality='usable',numeric_verified=False,header_ids=['h'],
            words=[dict(text='17',confidence=.99,bbox=[60,30,80,40])])])


@pytest.mark.parametrize('refiner',[refine_tables,verify_recovered_tables])
@pytest.mark.parametrize('rotation',[None,90,270])
def test_crop_rendering_preserves_canonical_pdf_and_absent_rotation_key(monkeypatch,refiner,rotation):
    import ocr_engine
    import pdf_cell_ocr
    monkeypatch.setattr(ocr_engine,'tesseract_version',lambda:'synthetic-test')
    monkeypatch.setattr(ocr_engine,'run_tsv',lambda *args,**kwargs:'')
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda *args:[])
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image:image)
    monkeypatch.setattr(pdf_cell_ocr,'_read_variants',lambda *args,**kwargs:[])
    monkeypatch.setattr(pdf_cell_ocr,'_rapid_numeric',lambda *args:[])
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=100)
        if rotation is None:
            doc.xref_set_key(page.xref,'Rotate','null')
        else:
            page.set_rotation(rotation)
        key=doc.xref_get_key(page.xref,'Rotate')
        original=doc.tobytes(no_new_id=True)
        table=deepcopy(sample_table())
        refiner(page,[table],seconds=10,max_numbers=1)
        assert doc.tobytes(no_new_id=True)==original
        assert doc.xref_get_key(page.xref,'Rotate')==key
        assert page.rotation==(rotation or 0)
        proof=table['cells'][2].get('numeric_refinement')
        assert proof and proof['bbox']==[50,25,100,50]
        assert proof['rotation']==(-(rotation or 0))%360
        assert not table['cells'][2]['numeric_verified']


@pytest.mark.parametrize('refiner',[refine_tables,verify_recovered_tables,recover_page])
def test_private_copy_does_not_reparent_source_underline_and_popup(refiner):
    with pymupdf.open() as doc:
        page=doc.new_page(width=200,height=200)
        annotation=page.add_underline_annot(pymupdf.Rect(20,20,100,40))
        annotation.set_popup(pymupdf.Rect(100,60,180,120))
        before=doc.tobytes(no_new_id=True)
        original=doc.xref_object(annotation.xref)
        assert doc.xref_get_key(annotation.xref,'P')[0]=='xref'
        assert doc.xref_get_key(annotation.xref,'Popup')[0]=='xref'
        table=sample_table()
        table['origin']='ocr'
        refiner(page,[table],seconds=0)
        assert doc.xref_object(annotation.xref)==original
        assert doc.tobytes(no_new_id=True)==before
