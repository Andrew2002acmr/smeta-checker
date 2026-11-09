from copy import deepcopy

import pytest

from audit_core import PageTextIndex
from pdf_ocr_acceptance import evaluate


def sample():
    cell = dict(cell_id='c', role='cell', text='1 234,50', bbox=[10, 10, 40, 30], numeric_verified=False)
    index = PageTextIndex([''], table_rows=[()], tables=[[dict(table_id='t', cells=[cell])]])
    annotations = dict(source_sha256='source', cases=[dict(id='number', page=1,
        bbox=[10, 10, 40, 30], check='literal', normalization='decimal', expected='1234.5')])
    return index, annotations


def test_acceptance_is_read_only_and_expected_values_do_not_repair_readings():
    index, annotations = sample()
    original = deepcopy(index.tables)
    assert evaluate(index, 'source', annotations)['passed'] == 1
    annotations['cases'][0]['expected'] = '1234.6'
    assert evaluate(index, 'source', annotations)['cases'][0]['reason'] == 'reading_differs'
    assert index.tables == original
    annotations['cases'][0].update(expected='1234.5', require_verified=True)
    assert evaluate(index, 'source', annotations)['cases'][0]['reason'] == 'reading_not_verified'


def test_wrong_document_or_ambiguous_geometry_cannot_pass():
    index, annotations = sample()
    with pytest.raises(ValueError, match='different PDF'):
        evaluate(index, 'other', annotations)
    index.tables[0][0]['cells'].append(deepcopy(index.tables[0][0]['cells'][0]))
    assert evaluate(index, 'source', annotations)['cases'][0]['reason'] == 'source_cell_ambiguous'
