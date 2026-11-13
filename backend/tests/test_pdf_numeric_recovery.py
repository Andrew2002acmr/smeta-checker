from copy import deepcopy

import pymupdf
import pytest

from pdf_cell_ocr import NUMERIC_RECOVERY_VERSION, recovered_numeric_value, refine_tables


def test_preparation_reads_missing_body_numbers_in_each_table_without_expected_values(monkeypatch):
    import ocr_engine
    import pdf_cell_ocr
    from PIL import Image
    from test_pdf_table_verification import table as make_table, proof as unit_proof
    monkeypatch.setattr(ocr_engine, 'tesseract_version', lambda: 'synthetic')
    monkeypatch.setattr(ocr_engine, 'run_tsv', lambda *a, **k: '')
    monkeypatch.setattr(ocr_engine, 'parse_tsv', lambda *a: [])
    monkeypatch.setattr(pdf_cell_ocr, 'clean_glyphs', lambda image: Image.new('L', (100, 40), color=255))
    monkeypatch.setattr(pdf_cell_ocr, '_rapid_numeric', lambda image: [dict(text='17', confidence=.99)])
    monkeypatch.setattr(pdf_cell_ocr, '_read_variants', lambda *a, **k: [variant(), {**variant(), 'height': 64}])
    with pymupdf.open() as doc:
        page = doc.new_page(width=150, height=100)
        page.insert_text((10, 40), '17')
        tables = [make_table(), make_table()]
        tables[1]['table_id'] = 'second'
        for t in tables:
            t['column_units'] = {'0': unit_proof()}
            t['cells'][1].update(text='', words=[], quality='usable', numeric_verified=False)
        refine_tables(page, tables, seconds=20, max_numbers=1, max_columns=0,
            include_body=True, resume_only=True)
        assert [t['cells'][1]['text'] for t in tables] == ['17', '17']
        assert all(t['cells'][1]['numeric_verified'] for t in tables)
        assert all(t['cells'][1]['numeric_recovery']['prior_text'] == '' for t in tables)


def test_unknown_unit_does_not_hide_cells_but_a_drawing_frame_is_not_a_quantity_table():
    from pdf_cell_ocr import numeric_candidates
    from test_pdf_table_verification import table as make_table
    t = make_table()
    t['cells'][0]['text'] = 'Количество'
    t['cells'][1].update(text='', quality='usable', words=[])
    assert numeric_candidates(t, include_body=True) == [t['cells'][1]]
    t['cells'][0]['text'] = '006/26-ТКР1-5'
    assert numeric_candidates(t, include_body=True) == []


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


def test_strong_cross_engine_consensus_keeps_and_checks_conflicting_scale_reads():
    from pdf_cell_ocr import STRONG_CONSENSUS
    cell, proof = evidence()
    proof['rapid_words'][0]['confidence'] = .993
    proof['consensus_policy'] = STRONG_CONSENSUS
    proof['variants'] = [dict(height=32, words=[dict(text='17', conf=96)]),
        dict(height=48, words=[dict(text='17', conf=95)]),
        dict(height=64, words=[dict(text='71', conf=86)])]
    original = deepcopy(proof)
    assert recovered_numeric_value(cell, proof) == '17'
    assert proof == original
    proof['variants'][2]['words'][0]['conf'] = 90
    assert recovered_numeric_value(cell, proof) is None
    proof = deepcopy(original)
    proof['rapid_words'][0]['confidence'] = .98
    assert recovered_numeric_value(cell, proof) is None
    proof = deepcopy(original)
    proof['variants'][1]['words'][0]['conf'] = 94
    assert recovered_numeric_value(cell, proof) is None


def test_rapid_padding_can_raise_readability_without_lowering_required_confidence():
    import time
    from PIL import Image
    from pdf_numeric_variants import padded_rapid
    image = Image.new('L', (100, 30), 255)
    initial = [dict(text='17', confidence=.89)]
    sizes = []
    def recognize(current):
        sizes.append(current.size)
        return [dict(text='17', confidence=.96)]
    selected, attempts = padded_rapid(image, initial, recognize, time.monotonic() + 5)
    assert selected == [dict(text='17', confidence=.96)]
    assert attempts[0]['words'] == initial
    assert sizes == [(116, 46)]
    cell, proof = evidence()
    proof['rapid_attempts'] = [dict(padding=0, words=[dict(text='71', confidence=.91)])]
    assert recovered_numeric_value(cell, proof) is None


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
        page.insert_text((60, 34), '17')
        doc.xref_set_key(page.xref,'Rotate','null')
        before=doc.tobytes(no_new_id=True)
        cell=dict(cell_id='n',text='',bbox=[50,20,100,40],row=1,column=1,role='cell',
            words=[],header_ids=['h'],quality='usable',numeric_verified=False)
        if contradiction:
            cell.update(text='17',words=[dict(text='17',confidence=.99)],
                numeric_refinement=dict(variants=[variant('71',95)]),
                raw_numeric_refinement=dict(variants=[variant('17',95)]))
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


def scalar_table(text='о', header='Количество'):
    return dict(table_id='physical', origin='ocr', col_count=1, cells=[
        dict(cell_id='h', role='header', row=0, column=0, text=header,
            bbox=[10, 0, 60, 20]),
        dict(cell_id='n', role='cell', row=1, column=0, text=text,
            bbox=[10, 20, 60, 40], header_ids=['h'], quality='uncertain',
            words=[dict(text=text, confidence=.5)], numeric_verified=False)])


@pytest.mark.parametrize('text', ['о', '0,о', '7486', '112', '12 34'])
def test_uncertain_scalar_measurement_cells_are_selected_without_recovery_flag(text):
    from pdf_cell_ocr import numeric_candidates, BODY_PREPARATION_VERSION
    from pdf_numeric_preparation import pending_numeric_tables
    table = scalar_table(text)
    cell = table['cells'][1]
    assert numeric_candidates(table, include_body=True) == [cell]
    assert pending_numeric_tables([table]) == [table]
    cell['numeric_preparation'] = dict(version=BODY_PREPARATION_VERSION, state='uncertain')
    assert pending_numeric_tables([table]) == []


@pytest.mark.parametrize('header,text', [
    ('Пикет', '112'), ('Позиция', '12'), ('Наименование', '17'),
    ('Ведомость', '17'), ('006/26-ТКР1-5', '17'), ('Количество', 'ПК12+34'),
    ('Количество', 'марка 17')])
def test_body_labels_pikets_and_nonmeasurement_grids_are_not_scalar_candidates(header, text):
    from pdf_cell_ocr import numeric_candidates
    assert numeric_candidates(scalar_table(text, header), include_body=True) == []


def test_suspect_scalar_geometry_rejection_is_preserved():
    from pdf_cell_ocr import numeric_candidates
    table = scalar_table()
    table['cells'][1]['geometry_validation'] = dict(requires_source_review=True)
    assert numeric_candidates(table, include_body=True) == []


@pytest.mark.parametrize('original', ['о', '7486', '112', '12 34'])
def test_body_reobservation_keeps_conflicting_literal_and_completes_once(monkeypatch, original):
    import ocr_engine
    import pdf_cell_ocr
    from pdf_numeric_preparation import complete_recovered_numbers
    from PIL import Image
    monkeypatch.setattr(ocr_engine, 'tesseract_version', lambda: 'synthetic')
    monkeypatch.setattr(ocr_engine, 'run_tsv', lambda *a, **k: '')
    monkeypatch.setattr(ocr_engine, 'parse_tsv', lambda *a: [])
    monkeypatch.setattr(pdf_cell_ocr, 'clean_glyphs', lambda image: Image.new('L', (100, 40), 255))
    monkeypatch.setattr(pdf_cell_ocr, '_rapid_numeric', lambda image: [dict(text='17', confidence=.99)])
    monkeypatch.setattr(pdf_cell_ocr, '_read_variants',
        lambda *a, **k: [variant(), {**variant(), 'height': 64}])
    with pymupdf.open() as doc:
        page = doc.new_page(width=150, height=100)
        table = scalar_table(original)
        cell = table['cells'][1]
        words = deepcopy(cell['words'])
        result = complete_recovered_numbers(page, [table], passes=3, seconds=10)
        assert cell['text'] == original and cell['words'] == words
        assert cell['numeric_verified'] is False
        assert cell['numeric_recovery']['prior_text'] == original
        assert cell['numeric_refinement']['prior_words'] == words
        assert len(result['passes']) == 1 and result['remaining'] == {}
        assert result['uncertain_cells'] == {'physical': ['n']}
        assert complete_recovered_numbers(page, [table])['passes'] == []


def test_unparseable_literal_cannot_agree_with_unparseable_reads():
    from pdf_cell_ocr import numeric_agreement
    assert not numeric_agreement('о', [variant('о'), {**variant('о'), 'height': 64}])


def test_recovery_clears_stale_verified_flag_before_contradictory_read(monkeypatch):
    import time
    import pdf_cell_ocr
    from PIL import Image
    cell, _ = evidence()
    cell.update(text='71', numeric_verified=True)
    monkeypatch.setattr(pdf_cell_ocr, 'clean_glyphs', lambda image: None)
    pdf_cell_ocr._recover_numeric(Image.new('L', (20, 20)), cell,
        dict(image_sha256='proof', dpi=600, rotation=0, variants=[variant()]),
        lambda *a, **k: [], time.monotonic() + 5)
    assert cell['text'] == '71' and cell['numeric_verified'] is False


def test_unparseable_original_is_not_verified_as_a_different_literal():
    cell, proof = evidence()
    cell['text'] = 'о'
    proof['rapid_words'][0]['text'] = '0'
    proof['variants'] = [variant('0'), {**variant('0'), 'height': 64}]
    assert recovered_numeric_value(cell, proof) is None


@pytest.mark.parametrize('table_origin,cell_origin,recovery,expected', [
    ('native', None, False, False),
    ('native', 'native', True, False),
    ('ocr', 'native', False, False),
    (None, None, False, False),
    ('ocr', None, False, True),
    (None, 'ocr', False, True),
    (None, None, True, True),
])
def test_numeric_preparation_only_reobserves_raster_cells(
        table_origin, cell_origin, recovery, expected):
    from pdf_cell_ocr import numeric_candidates
    from pdf_numeric_preparation import pending_numeric_tables
    table = scalar_table('112')
    if table_origin is None:
        table.pop('origin')
    else:
        table['origin'] = table_origin
    if cell_origin is not None:
        table['cells'][1]['origin'] = cell_origin
    if recovery:
        table['recovery'] = dict(version='physical-grid')
    assert bool(numeric_candidates(table, include_body=True)) is expected
    assert bool(pending_numeric_tables([table])) is expected
