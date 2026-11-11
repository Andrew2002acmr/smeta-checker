from contextlib import closing
from copy import deepcopy

import pymupdf
import pytest
from PIL import Image, ImageDraw

from audit_core import _evaluate_quantity, _evaluate_unit
from audit_models import ClaimState
from document_preparation import assemble_document
from pdf_empty_cells import empty_paper, printed_dash
from pdf_header_lines import candidates as semantic_candidates
from pdf_header_lines import pending_pages as pending_semantic_pages
from pdf_header_preparation import pending_header_pages
from pdf_numeric_preparation import pending_ids, pending_numeric_pages, pending_numeric_tables
from pdf_paired_ocr import pending_cells as pending_paired_cells
from pdf_ruled_recovery import rebuild
from pdf_table_checks import check_table
from pdf_tables import RULED_OCR_VERSION, is_primary_ruled_table, primary_observation_complete
from pdf_text import PDFTextSource
from test_pdf_ruled_recovery import sample_grid


def physical_table(values=None, *, offset=(0, 0)):
    boxes, logical, words = sample_grid()
    dx, dy = offset
    boxes = [[x0 + dx, y0 + dy, x1 + dx, y1 + dy] for x0, y0, x1, y1 in boxes]
    for word in words:
        x0, y0, x1, y1 = word['bbox']
        word['bbox'] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
    if values is not None:
        words = [dict(word, text=text) for word, text in zip(words, values, strict=True) if text]
    return rebuild(boxes, logical, words, page=1)


def primary_table(values=None, *, offset=(0, 0)):
    table = physical_table(values, offset=offset)
    table['recognition'] = dict(version=RULED_OCR_VERSION, engine='rapidocr',
        rec_model='PP-OCRv5/cyrillic/mobile', state='complete')
    for cell in table['cells']:
        cell['recognition'] = dict(version=RULED_OCR_VERSION, state='recognized', confidence=.99,
            readings=[dict(text=cell['text'], confidence=.99, crop_bbox=list(cell['bbox']))])
    return table


def physical_literal(cell, kind):
    image = Image.new('L', (100, 30), 255)
    if kind == 'literal_dash':
        ImageDraw.Draw(image).rectangle((40, 14, 60, 15), fill=0)
        marker = printed_dash(image)
        text = '–'
    else:
        marker = empty_paper(image)
        text = ''
    assert marker is not None
    cell.update(text=text, words=[], literal_marker=dict(marker, bbox=list(cell['bbox'])))
    cell['recognition'] = dict(version=RULED_OCR_VERSION, state=kind, confidence=None, readings=[])


def assembled(tables, errors=()):
    text = '\n'.join(cell['text'] for table in tables for cell in table['cells'])
    native = dict(texts=[''], pdf_sha256='synthetic-source', extraction_key='synthetic-native',
        ocr_settings_key='synthetic-settings')
    packed = dict(page=1, text=text, table_rows=[], locators=[], tables=tables,
        quality={'state': 'uncertain'}, errors=list(errors),
        geometry=dict(width=640, height=200, rotation=0), table_error=None)
    return assemble_document(native, [packed])


def test_complete_primary_observation_is_not_independent_numeric_or_unit_proof():
    table = primary_table()
    before = deepcopy(table)
    assert is_primary_ruled_table(table)
    assert primary_observation_complete(table)
    assert all(cell['numeric_verified'] is False for cell in table['cells'])
    assert all(cell['recognition']['confidence'] == .99 for cell in table['cells'])
    arithmetic = check_table(table)
    assert arithmetic['state'] == 'not_checkable'
    assert arithmetic['ocr_verified'] is False
    assert 'unknown_or_unproved_literal_unit' in {check.get('reason') for check in arithmetic['checks']}
    assert table == before


@pytest.mark.parametrize('origin,version,expected', [
    ('ocr', RULED_OCR_VERSION, True),
    ('native', RULED_OCR_VERSION, False),
    ('ocr', 'rapidocr-ruled-obsolete', False),
    ('ocr', None, False),
])
def test_primary_identity_is_only_origin_and_current_version(origin, version, expected):
    table = dict(origin=origin, recognition=dict(version=version, state='incomplete'), cells=[])
    assert is_primary_ruled_table(table) is expected
    assert primary_observation_complete(table) is False


@pytest.mark.parametrize('damage', [
    'missing_recognition', 'missing_interior_cell', 'unreadable', 'not_attempted',
    'stale_cell_version', 'reading_outside_cell', 'invalid_cell_bbox', 'missing_reading',
    'mismatched_text', 'numeric_verified', 'recognized_conflict', 'table_bbox',
])
def test_outer_complete_flag_cannot_hide_missing_or_inconsistent_physical_observation(damage):
    table = primary_table()
    cell = table['cells'][4]
    if damage == 'missing_recognition':
        del cell['recognition']
    elif damage == 'missing_interior_cell':
        table['cells'].remove(cell)
    elif damage in {'unreadable', 'not_attempted'}:
        cell.update(text='', words=[])
        cell['recognition'].update(state=damage, confidence=None, readings=[])
    elif damage == 'stale_cell_version':
        cell['recognition']['version'] = 'rapidocr-ruled-obsolete'
    elif damage == 'reading_outside_cell':
        cell['recognition']['readings'][0]['crop_bbox'][0] = table['bbox'][0] - 10
    elif damage == 'invalid_cell_bbox':
        cell['bbox'][2] = cell['bbox'][0]
    elif damage == 'missing_reading':
        cell['recognition']['readings'] = []
    elif damage == 'mismatched_text':
        cell['text'] = '500'
    elif damage == 'recognized_conflict':
        cell['recognition']['readings'].append(dict(text='500', confidence=.99,
            crop_bbox=list(cell['bbox']), method='target_cell'))
    elif damage == 'table_bbox':
        table['bbox'][0] += 5
    else:
        cell['numeric_verified'] = True
    before = deepcopy(table)
    assert table['recognition']['state'] == 'complete'
    assert is_primary_ruled_table(table)
    assert primary_observation_complete(table) is False
    assert table == before


def test_empty_paper_zero_and_printed_dash_remain_distinct_complete_observations():
    table = primary_table(['Пикет', 'Площадь, м2', 'Объем, м3', 'ПК1', '0', '10', 'Всего:', '0', '10'])
    blank, dash = table['cells'][7:9]
    physical_literal(blank, 'literal_empty')
    physical_literal(dash, 'literal_dash')
    zero = table['cells'][4]
    assert primary_observation_complete(table)
    assert [cell['text'] for cell in (blank, zero, dash)] == ['', '0', '–']
    assert [cell['recognition']['state'] for cell in (blank, zero, dash)] == [
        'literal_empty', 'recognized', 'literal_dash']
    before = deepcopy(table)
    index = assembled([table])
    assert index.metadata['preparation_state'] == 'complete'
    assert index.tables[0][0] == before
    assert all(not cell['numeric_verified'] for cell in index.tables[0][0]['cells'])


@pytest.mark.parametrize('damage', ['missing', 'wrong_meaning', 'stale_bbox', 'zero_text', 'dash_marker'])
def test_literal_empty_requires_matching_physical_empty_paper_marker(damage):
    table = primary_table()
    cell = table['cells'][4]
    physical_literal(cell, 'literal_empty')
    if damage == 'missing':
        del cell['literal_marker']
    elif damage == 'wrong_meaning':
        cell['literal_marker']['meaning'] = 'empty_ocr_output'
    elif damage == 'stale_bbox':
        cell['literal_marker']['bbox'][0] += 5
    elif damage == 'zero_text':
        cell['text'] = '0'
    else:
        image = Image.new('L', (100, 30), 255)
        ImageDraw.Draw(image).rectangle((40, 14, 60, 15), fill=0)
        marker = printed_dash(image)
        assert marker is not None
        cell['literal_marker'] = dict(marker, bbox=list(cell['bbox']))
    assert primary_observation_complete(table) is False


@pytest.mark.parametrize('confidence,second_text', [(.91, None), (.99, '500')])
def test_uncertain_readings_are_complete_without_erasing_first_literal(confidence, second_text):
    table = primary_table()
    cell = table['cells'][4]
    cell['quality'] = 'uncertain'
    cell['recognition'].update(state='uncertain', confidence=confidence)
    cell['recognition']['readings'][0]['confidence'] = confidence
    cell['words'][0]['confidence'] = confidence
    if second_text is not None:
        cell['recognition']['readings'].append(dict(text=second_text, confidence=.99,
            crop_bbox=list(cell['bbox']), method='target_cell'))
    before = deepcopy(table)
    assert primary_observation_complete(table)
    index = assembled([table])
    assert index.metadata['preparation_state'] == 'complete'
    observed = index.tables[0][0]['cells'][4]
    assert observed['text'] == '50'
    assert observed['recognition']['state'] == 'uncertain'
    assert observed['quality'] == 'uncertain'
    assert observed['numeric_verified'] is False
    assert table == before
    assert index.tables[0][0] == before


def test_complete_primary_assembly_needs_no_legacy_header_quorum_or_automatic_reread():
    table = primary_table()
    before = deepcopy(table)
    index = assembled([table])
    assert index.errors == {}
    assert index.metadata['failed_pages'] == []
    assert index.metadata['preparation_state'] == 'complete'
    assert index.metadata['incomplete_table_headers'] == {}
    assert pending_ids(table) == []
    assert pending_numeric_tables([table]) == []
    assert pending_numeric_pages(index) == []
    assert pending_header_pages(index) == []
    assert semantic_candidates(table) == []
    assert pending_semantic_pages(index) == []
    assert index.tables[0][0] == before
    assert all(cell['text'] in index.texts[0] for cell in table['cells'])


@pytest.mark.parametrize('state', ['unreadable', 'not_attempted'])
def test_incomplete_primary_assembly_keeps_partial_evidence_and_technical_failure(state):
    table = primary_table()
    table['cells'][4].update(text='', words=[])
    table['cells'][4]['recognition'].update(state=state, confidence=None, readings=[])
    before = deepcopy(table)
    index = assembled([table], errors=['ruled_ocr_failed:TimeoutError'])
    assert primary_observation_complete(table) is False
    assert index.metadata['preparation_state'] == 'complete_with_errors'
    assert index.metadata['failed_pages'] == [1]
    assert 'incomplete_cell_observation' in index.errors[0]
    assert 'ruled_ocr_failed:TimeoutError' in index.errors[0]
    assert 'incomplete_header_observation' not in index.errors[0]
    assert index.metadata['incomplete_table_headers'] == {}
    assert index.tables[0][0] == before
    assert pending_ids(table) == []
    assert pending_header_pages(index) == []
    assert pending_semantic_pages(index) == []


def test_mixed_primary_and_legacy_page_only_defers_real_legacy_work():
    primary = primary_table()
    scalar_legacy = physical_table(offset=(0, 100))
    paired_values = ['Наименование', 'Единица измерения', 'Количество',
        'Основание', 'м2/м3', '50/10', 'Всего:', 'м2/м3', '50/10']
    paired_primary = primary_table(paired_values, offset=(320, 0))
    paired_legacy = physical_table(paired_values, offset=(320, 100))
    # Explicit proof tools remain available; only automatic scheduling excludes primary cells.
    paired_ids = {cell['cell_id'] for cell, _ in pending_paired_cells(paired_legacy)}
    assert paired_ids
    assert pending_paired_cells(paired_primary)
    assert set(pending_ids(paired_legacy)) == paired_ids
    assert pending_ids(scalar_legacy)
    assert semantic_candidates(scalar_legacy)
    tables = [primary, scalar_legacy, paired_primary, paired_legacy]
    before = deepcopy(tables)
    assert pending_numeric_tables(tables) == [scalar_legacy, paired_legacy]
    assert pending_ids(primary) == pending_ids(paired_primary) == []
    assert semantic_candidates(primary) == semantic_candidates(paired_primary) == []
    index = assembled(tables)
    assert pending_header_pages(index) == [0]
    assert pending_numeric_pages(index) == [0]
    assert pending_semantic_pages(index) == [0]
    assert set(index.metadata['incomplete_table_headers']['1']) == {
        scalar_legacy['table_id'], paired_legacy['table_id']}
    assert index.metadata['preparation_state'] == 'complete_with_errors'
    assert index.tables[0][0] == before[0]
    assert index.tables[0][2] == before[2]
    assert tables == before


def test_primary_literals_require_review_in_matching(tmp_path):
    table = primary_table(['Пикет', 'Площадь, м2', 'Объем, м3',
        'ПК1', '50', '10 м3', 'Всего:', '50', '10'])
    with pymupdf.open() as document:
        page = document.new_page(width=320, height=160)
        page.insert_text((10, 120), 'Native paragraph retained. ' * 3)
        with closing(PDFTextSource(document, cache_dir=tmp_path, full_document=True)) as source:
            _, locators = source._store_tables(0, [table], origin='ocr')
    locator = next(item for item in locators if item['text'] == '10 м3')
    assert locator['recognition'] == table['cells'][5]['recognition']
    assert locator['quality'] == 'uncertain'
    assert locator['numeric_verified'] is False
    assert locator['unit_verified'] is False
    evidence = [dict(source_id=locator['source_id'], page=0, quote=locator['text'], locator=locator)]
    before = deepcopy(evidence)
    quantity, quantity_evidence = _evaluate_quantity(evidence, '10', 'м3', '')
    unit, unit_evidence = _evaluate_unit(evidence, 'м3')
    assert quantity.state == ClaimState.REVIEW_REQUIRED
    assert unit.state == ClaimState.REVIEW_REQUIRED
    assert 'unverified_ocr_literal' in unit.reasons
    assert quantity_evidence == unit_evidence == evidence
    assert quantity.facts and unit.facts
    assert evidence == before

    independent_unit = deepcopy(evidence)
    independent_unit[0]['locator']['unit_verified'] = True
    proved_unit, _ = _evaluate_unit(independent_unit, 'м3')
    still_unproved_quantity, _ = _evaluate_quantity(independent_unit, '10', 'м3', '')
    assert proved_unit.state == ClaimState.CONFIRMED
    assert still_unproved_quantity.state == ClaimState.REVIEW_REQUIRED

    native = deepcopy(evidence)
    native[0]['locator'].pop('recognition')
    native[0]['locator'].update(origin='native', quality='usable', unit_verified=False)
    native_quantity, _ = _evaluate_quantity(native, '10', 'м3', '')
    native_unit, _ = _evaluate_unit(native, 'м3')
    assert native_quantity.state == native_unit.state == ClaimState.CONFIRMED
