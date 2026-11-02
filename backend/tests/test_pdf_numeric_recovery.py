from copy import deepcopy

import pymupdf
import pytest

from pdf_cell_ocr import NUMERIC_RECOVERY_VERSION, recovered_numeric_value, refine_tables


def variant(text='17',conf=95):
    return dict(height=48,words=[dict(text=text,conf=conf)])


def evidence():
    cell=dict(text='',bbox=[10,20,60,40],words=[])
    proof=dict(version=NUMERIC_RECOVERY_VERSION,bbox=list(cell['bbox']),
        rapid_words=[dict(text='17',confidence=.99)],variants=[variant(),{**variant(),'height':64}])
    return cell,proof


def test_empty_cell_requires_fresh_rapid_and_two_single_number_tesseract_reads():
    cell,proof=evidence()
    assert recovered_numeric_value(cell,proof)=='17'
    proof['rapid_words']=[]
    assert recovered_numeric_value(cell,proof) is None
    cell,proof=evidence()
    proof['variants']=proof['variants'][:1]
    assert recovered_numeric_value(cell,proof) is None
    cell,proof=evidence()
    proof['variants']=[variant(conf=89)]*2
    assert recovered_numeric_value(cell,proof) is None


@pytest.mark.parametrize('change',[
    lambda c,p:p['rapid_words'][0].update(confidence=.89),
    lambda c,p:p['rapid_words'][0].update(text='71'),
    lambda c,p:p['variants'].append(variant('71',80)),
    lambda c,p:p.update(prior_variants=[variant('71',80)]),
    lambda c,p:p.update(variants=[variant(),variant()]),
    lambda c,p:c.update(text='71'),
    lambda c,p:c.update(words=[dict(text='71',confidence=.95)]),
    lambda c,p:p.update(bbox=[60,20,110,40]),
    lambda c,p:p.update(rapid_words=[dict(text='1',confidence=.99),dict(text='7',confidence=.99)]),
    lambda c,p:p.update(variants=[dict(words=[dict(text='1',conf=98),dict(text='7',conf=98)])]*2),
])
def test_disagreement_cross_cell_evidence_and_joined_numbers_do_not_confirm(change):
    cell,proof=evidence()
    change(cell,proof)
    assert recovered_numeric_value(cell,proof) is None


@pytest.mark.parametrize('contradiction',[False,True])
def test_total_crop_can_fill_blank_preserving_raw_words_and_pdf(monkeypatch,contradiction):
    import ocr_engine
    import pdf_cell_ocr
    from PIL import Image
    monkeypatch.setattr(ocr_engine,'tesseract_version',lambda:'synthetic')
    monkeypatch.setattr(ocr_engine,'run_tsv',lambda *a,**k:'')
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda *a:[])
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image:Image.new('L',(100,40),color=255))
    monkeypatch.setattr(pdf_cell_ocr,'_rapid_numeric',lambda image:[dict(text='17',confidence=.99)])
    monkeypatch.setattr(pdf_cell_ocr,'_read_variants',lambda *a,**k:[variant(),{**variant(),'height':64}])
    with pymupdf.open() as doc:
        page=doc.new_page(width=150,height=100)
        doc.xref_set_key(page.xref,'Rotate','null')
        before=doc.tobytes(no_new_id=True)
        cell=dict(cell_id='n',text='',bbox=[50,20,100,40],row=1,column=1,role='cell',
            words=[],header_ids=['h'],quality='usable',numeric_verified=False)
        if contradiction:
            cell.update(text='17',words=[dict(text='17',confidence=.99)],
                numeric_refinement=dict(variants=[variant('71',95)]))
        verified={**deepcopy(cell),'cell_id':'old','column':2,'bbox':[100,20,150,40],
            'text':'19','numeric_verified':True}
        old=deepcopy(verified)
        table=dict(recovery={'version':'physical-grid'},col_count=3,cells=[
            dict(cell_id='h',role='header',row=0,column=1,colspan=1,text='м2',bbox=[50,0,100,20]),
            dict(cell_id='total',role='cell',row=1,column=0,text='Всего:',bbox=[0,20,50,40]),cell,verified])
        refine_tables(page,[table],seconds=10,max_columns=0)
        assert cell['text']=='17' and cell['numeric_verified']==(not contradiction)
        assert cell['numeric_recovery']['prior_text']==('17' if contradiction else '')
        assert cell['words']==cell['numeric_recovery']['prior_words']
        assert cell['numeric_recovery']['bbox']==cell['bbox']
        assert verified==old and doc.tobytes(no_new_id=True)==before
