import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw

from pdf_header_lines import VERSION, candidates, observe, pending_pages


def image():
    im=Image.new('L',(120,90),255)
    draw=ImageDraw.Draw(im)
    draw.rectangle((20,15,90,30),fill=0)
    draw.rectangle((30,55,60,70),fill=0)
    return im


def test_multiline_read_retains_all_lines_without_changing_literal_or_asserting_truth():
    rapid=Mock(side_effect=[[dict(text='Покрытие А5Вл',confidence=.97)],[dict(text='(5см)',confidence=.6)]])
    tess=Mock(return_value=[dict(text='uncertain',conf=40)])
    result=observe(image(),deadline=time.monotonic()+10,read_rapid=rapid,read_tesseract=tess)
    assert result['state']=='observed' and len(result['lines'])==2
    assert result['lines'][0]['rapid_words'][0]['text']=='Покрытие А5Вл'
    assert not result['recognition_verified'] and all(not line['recognition_verified'] for line in result['lines'])
    assert all(line['image_sha256'] for line in result['lines'])


def test_resumption_reuses_completed_line_without_repeating_ocr():
    first=observe(image(),deadline=time.monotonic()+10,read_rapid=lambda _:[],read_tesseract=lambda _:[])
    rapid,tess=Mock(side_effect=AssertionError('Repeated OCR')),Mock(side_effect=AssertionError('Repeated OCR'))
    result=observe(image(),deadline=0,read_rapid=rapid,read_tesseract=tess,previous_lines=first['lines'])
    assert result==first


def test_accurate_model_upgrade_keeps_previous_reads_and_is_resumable():
    first=observe(image(),deadline=time.monotonic()+10,read_rapid=lambda _:[],read_tesseract=lambda _:[])
    old=deepcopy(first)
    no_repeat=Mock(side_effect=AssertionError('Repeated base OCR'))
    accurate=Mock(return_value=[dict(text='Покрытие',conf=95)])
    result=observe(image(),deadline=time.monotonic()+10,read_rapid=no_repeat,read_tesseract=no_repeat,
        previous_lines=first['lines'],read_best=accurate,model_identity={'sha256':'pinned'})
    assert accurate.call_count==4 and first==old
    assert all(len(line['best_variants'])==2 for line in result['lines'])
    resumed=observe(image(),deadline=0,read_rapid=no_repeat,read_tesseract=no_repeat,
        previous_lines=result['lines'],read_best=no_repeat,model_identity={'sha256':'pinned'})
    assert resumed==result


def test_unit_readiness_does_not_suppress_semantic_header_reading(monkeypatch):
    from header_ocr_model import SHA256
    monkeypatch.setattr('header_ocr_model.configured', lambda: {'sha256': SHA256})
    header=dict(role='header',cell_id='h',bbox=[0,0,20,20],row=0,column=0,text='(5см)',header_ids=[])
    table=dict(recovery={'version':'grid'},cells=[header],column_units={'0':dict(unit='м2')})
    original=deepcopy(table)
    assert candidates(table)==[header]
    assert pending_pages(SimpleNamespace(tables=([table],)))==[0]
    assert table==original
    header['header_line_observation']=dict(version=VERSION,state='observed',lines=[],
        header_best_model_sha256=SHA256)
    assert not candidates(table)


def test_same_engine_agreement_does_not_hide_conflicting_material_read():
    from pdf_table_verification import independent_text_conflict
    header=dict(words=[dict(text='Основание – ЩПС',confidence=.962)])
    assert independent_text_conflict(header,'Основание - ЩПЕ 0/31,5, м3')
    assert not independent_text_conflict(header,'Основание – ЩПС 0/31,5, м3')
    assert not independent_text_conflict(dict(words=[dict(text='M800',confidence=.99)]),'М800')


def test_generic_conflict_recovery_preserves_rejected_read_and_uses_only_existing_source():
    from pdf_header_lines import restore_conflicting_read
    header=dict(text='Основание ЩПЕ',raw_recovered_text='Основание ЩПС',
        words=[dict(text='ЩПС',confidence=.99)])
    assert restore_conflicting_read(header)
    assert header['text']=='Основание ЩПС' and header['rejected_header_consensus_text']=='Основание ЩПЕ'
    assert header['quality']=='uncertain'
    assert not restore_conflicting_read(header)


def test_scaled_projection_retains_faint_lines_and_overlapping_superscript():
    from pdf_header_lines import line_boxes
    page = Image.new('L', (80, 50), 255)
    drawing = ImageDraw.Draw(page)
    drawing.rectangle((10, 12, 35, 18), fill=220)
    drawing.rectangle((36, 8, 40, 12), fill=220)
    drawing.rectangle((10, 23, 35, 29), fill=220)
    assert line_boxes(page, gap=3, threshold=250) == [
        [0, 5, 80, 22], [0, 20, 80, 33],
    ]
