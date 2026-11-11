from copy import deepcopy
import hashlib
import json
import sys
from types import SimpleNamespace
from uuid import UUID

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
    with pytest.raises(ValueError):
        evaluate(index, 'other', annotations)
    index.tables[0][0]['cells'].append(deepcopy(index.tables[0][0]['cells'][0]))
    assert evaluate(index, 'source', annotations)['cases'][0]['reason'] == 'source_cell_ambiguous'


def structured_sample():
    cells = [
        dict(cell_id='runtime-top', role='header', text='Объём', bbox=[0, 0, 40, 10],
            row=0, column=0, rowspan=1, colspan=2, header_ids=[]),
        dict(cell_id='runtime-unit', role='header', text='м³', bbox=[0, 10, 40, 20],
            row=1, column=0, rowspan=1, colspan=2, header_ids=['runtime-top']),
        dict(cell_id='runtime-value', role='cell', text='1,16', bbox=[0, 20, 40, 30],
            row=2, column=0, rowspan=1, colspan=2, header_ids=['runtime-top', 'runtime-unit']),
    ]
    for cell in cells:
        cell.update(numeric_verified=False, recognition=dict(state='recognized'))
    table = dict(table_id='runtime-table', page=1, row_count=3, col_count=2, cells=cells)
    index = PageTextIndex([''], tables=[[table]])
    cases = []
    for case_id, cell, header_cases in zip(('top', 'unit', 'value'), cells,
        ([], ['top'], ['top', 'unit']), strict=True):
        cases.append(dict(id=case_id, page=1, bbox=list(cell['bbox']), check='literal',
            expected=cell['text'], normalization='exact', role=cell['role'],
            row=cell['row'], column=cell['column'], rowspan=cell['rowspan'], colspan=cell['colspan'],
            header_case_ids=header_cases, expected_recognition_state='nonempty', require_verified=False))
    annotations = dict(source_sha256='source', cases=cases,
        expected_table=dict(page=1, row_count=3, col_count=2, physical_cells=3))
    return index, annotations


def failed_structure(result, check):
    return [item for item in result['structure_checks'] if item['check'] == check and not item['passed']]


@pytest.mark.parametrize('actual,expected', [
    ('1,16', '1.16'), ('7,16', '1,16'), ('１,１６', '1,16'),
    ('A', 'a'), (' 1,16', '1,16'), ('1,\n16', '1, 16'), ('-', '–'),
])
def test_exact_readings_do_not_normalize_source_characters(actual, expected):
    index, annotations = sample()
    index.tables[0][0]['cells'][0]['text'] = actual
    annotations['cases'][0].update(expected=expected, normalization='exact')
    result = evaluate(index, 'source', annotations)
    assert result['failed'] == 1
    assert result['cases'][0]['reason'] == 'reading_differs'


def test_exact_structure_and_header_cases_are_read_only_and_independent_of_runtime_ids():
    index, annotations = structured_sample()
    before_tables, before_annotations = deepcopy(index.tables), deepcopy(annotations)
    result = evaluate(index, 'source', annotations)
    assert result['total'] == result['passed'] == 3
    assert result['failed'] == 0
    assert result['structure_checks']
    assert all(check['passed'] and check['reason'] == 'ok' for check in result['structure_checks'])
    assert index.tables == before_tables
    assert annotations == before_annotations


@pytest.mark.parametrize('change,reason', [
    ('missing', 'source_cell_missing'),
    ('stale', 'source_cell_missing'),
    ('ambiguous', 'source_cell_ambiguous'),
])
def test_exact_source_anchor_never_falls_back_to_logical_coordinates(change, reason):
    index, annotations = structured_sample()
    cells = index.tables[0][0]['cells']
    if change == 'missing':
        cells.pop()
    elif change == 'stale':
        cells[-1]['bbox'] = [5, 20, 45, 30]
    else:
        cells.append(deepcopy(cells[-1]))
    result = evaluate(index, 'source', annotations)
    assert result['cases'][-1]['reason'] == reason
    assert failed_structure(result, 'source_cell')


@pytest.mark.parametrize('field,value', [('row', 1), ('column', 1), ('rowspan', 2), ('colspan', 1)])
def test_logical_cell_shape_is_checked_separately_from_literal_totals(field, value):
    index, annotations = structured_sample()
    index.tables[0][0]['cells'][-1][field] = value
    result = evaluate(index, 'source', annotations)
    assert result['passed'] == result['total'] == 3
    assert result['failed'] == 0
    assert failed_structure(result, field)[0]['reason'] == 'cell_geometry_differs'


@pytest.mark.parametrize('change,reason', [
    ('stale', 'header_ancestor_missing_or_duplicate'),
    ('wrong_order', 'header_ancestry_differs'),
    ('duplicate_actual', 'header_ancestry_duplicate'),
    ('missing_actual', 'header_ancestry_missing'),
    ('missing_case', 'header_case_missing'),
    ('duplicate_case', 'header_case_duplicate'),
    ('ambiguous_case', 'header_case_ambiguous'),
    ('missing_header_cell', 'header_source_cell_missing'),
    ('duplicate_header_cell_id', 'header_ancestor_missing_or_duplicate'),
])
def test_header_ancestry_uses_unique_matched_annotation_cases(change, reason):
    index, annotations = structured_sample()
    cells = index.tables[0][0]['cells']
    value = cells[-1]
    if change == 'stale':
        value['header_ids'] = ['obsolete-top', 'runtime-unit']
    elif change == 'wrong_order':
        value['header_ids'].reverse()
    elif change == 'duplicate_actual':
        value['header_ids'].append('runtime-top')
    elif change == 'missing_actual':
        del value['header_ids']
    elif change == 'missing_case':
        annotations['cases'][-1]['header_case_ids'] = ['unknown']
    elif change == 'duplicate_case':
        annotations['cases'][-1]['header_case_ids'] = ['top', 'top']
    elif change == 'ambiguous_case':
        annotations['cases'].append(deepcopy(annotations['cases'][0]))
    elif change == 'missing_header_cell':
        cells[0]['bbox'] = [100, 0, 140, 10]
    else:
        cells.append(dict(cells[0], bbox=[100, 0, 140, 10]))
    result = evaluate(index, 'source', annotations)
    assert any(check.get('case_id') == 'value' and check['reason'] == reason
        for check in failed_structure(result, 'header_case_ids'))


def test_header_from_another_table_cannot_supply_ancestry():
    index, annotations = structured_sample()
    table = index.tables[0][0]
    header = table['cells'].pop(0)
    index.tables = ((table, dict(table_id='other-table', cells=[header])),)
    result = evaluate(index, 'source', annotations)
    assert any(check.get('case_id') == 'value' and check['reason'] == 'header_from_other_table'
        for check in failed_structure(result, 'header_case_ids'))


def test_extra_physical_cell_fails_even_when_all_annotated_literals_match():
    index, annotations = structured_sample()
    index.tables[0][0]['cells'].append(dict(cell_id='extra', role='cell', text='',
        bbox=[40, 20, 50, 30], row=2, column=2, rowspan=1, colspan=1))
    result = evaluate(index, 'source', annotations)
    assert result['failed'] == 0
    assert failed_structure(result, 'physical_cells')[0]['actual'] == 4
    assert failed_structure(result, 'col_count')


@pytest.mark.parametrize('field', ['row_count', 'col_count'])
def test_table_dimensions_cannot_be_satisfied_by_stale_metadata(field):
    index, annotations = structured_sample()
    index.tables[0][0][field] += 1
    result = evaluate(index, 'source', annotations)
    assert result['failed'] == 0
    assert failed_structure(result, field)


@pytest.mark.parametrize('count,reason', [(0, 'source_table_missing'), (2, 'source_table_ambiguous')])
def test_expected_table_requires_exactly_one_table_on_its_page(count, reason):
    index, annotations = structured_sample()
    if count:
        index.tables = ((index.tables[0][0], deepcopy(index.tables[0][0])),)
    else:
        index.tables = ((),)
    result = evaluate(index, 'source', annotations)
    assert failed_structure(result, 'table_cardinality')[0]['reason'] == reason


@pytest.mark.parametrize('state', ['recognized', 'uncertain', 'unreadable', 'not_attempted', None])
@pytest.mark.parametrize('explicit_state', [False, True])
def test_exact_empty_is_not_an_unreadable_or_unproved_empty_result(state, explicit_state):
    index, annotations = sample()
    cell = index.tables[0][0]['cells'][0]
    cell.update(text='', recognition=dict(state=state))
    annotations['cases'][0].update(expected='', normalization='exact')
    if explicit_state:
        annotations['cases'][0]['expected_recognition_state'] = 'literal_empty'
    result = evaluate(index, 'source', annotations)
    assert result['cases'][0]['reason'] == 'recognition_state_differs'


@pytest.mark.parametrize('state,text,version,meaning', [
    ('literal_empty', '', 'physical-empty-paper-v1', 'empty_paper_not_numeric_zero'),
    ('literal_dash', '–', 'physical-printed-dash-v1', 'printed_dash_not_numeric_zero'),
])
def test_physical_literal_requires_both_matching_state_and_marker_proof(state, text, version, meaning):
    index, annotations = sample()
    cell = index.tables[0][0]['cells'][0]
    cell.update(text=text, recognition=dict(state=state))
    annotations['cases'][0].update(expected=text, normalization='exact', expected_recognition_state=state)
    assert evaluate(index, 'source', annotations)['failed'] == 1
    cell['literal_marker'] = dict(version=version, meaning=meaning)
    assert evaluate(index, 'source', annotations)['passed'] == 1
    cell['literal_marker']['meaning'] = 'not_a_physical_proof'
    assert evaluate(index, 'source', annotations)['failed'] == 1


@pytest.mark.parametrize('state,passed', [('recognized', True), ('uncertain', True),
    ('unreadable', False), ('not_attempted', False), ('literal_empty', False)])
def test_nonempty_observation_is_not_independent_numeric_verification(state, passed):
    index, annotations = structured_sample()
    cell = index.tables[0][0]['cells'][-1]
    case = annotations['cases'][-1]
    cell['recognition']['state'] = state
    result = evaluate(index, 'source', annotations)
    assert result['cases'][-1]['passed'] is passed
    case['require_verified'] = True
    assert not evaluate(index, 'source', annotations)['cases'][-1]['passed']
    if passed:
        assert evaluate(index, 'source', annotations)['cases'][-1]['reason'] == 'reading_not_verified'
        cell['numeric_verified'] = True
        assert evaluate(index, 'source', annotations)['cases'][-1]['passed']


def test_legacy_annotations_keep_decimal_literal_and_no_structure_requirements():
    index, annotations = sample()
    assert evaluate(index, 'source', annotations)['structure_checks'] == []
    cell = index.tables[0][0]['cells'][0]
    cell['text'] = '  М³\n  '
    annotations['cases'][0].update(normalization='literal', expected='м3')
    assert evaluate(index, 'source', annotations)['passed'] == 1


@pytest.mark.parametrize('failure', ['none', 'reading', 'structure', 'legacy'])
def test_checkpoint_cli_rejects_structure_failures_without_changing_case_totals(
    monkeypatch, tmp_path, failure,
):
    from scripts import check_prepared_ocr

    index, annotations = sample() if failure == 'legacy' else structured_sample()
    if failure == 'reading':
        index.tables[0][0]['cells'][-1]['text'] = '7,16'
    elif failure == 'structure':
        index.tables[0][0]['cells'][-1]['colspan'] = 1
    raw = b'original PDF bytes'
    source_sha256 = hashlib.sha256(raw).hexdigest()
    version_id = UUID('00000000-0000-0000-0000-000000000001')
    index.metadata['document_version_id'] = str(version_id)
    annotations['source_sha256'] = source_sha256
    checkpoint, annotation_path, out = (tmp_path / name for name in ('checkpoint.json', 'gold.json', 'out.json'))
    checkpoint.write_text(json.dumps(dict(config=dict(prepared_documents=dict(document=dict(
        version_id=str(version_id), snapshot='saved-snapshot'))))), encoding='utf-8')
    annotation_path.write_text(json.dumps(annotations), encoding='utf-8')
    blobs = SimpleNamespace(get=lambda key: raw)
    version = SimpleNamespace(id=version_id, sha256=source_sha256, blob_key='source-pdf')
    repo = SimpleNamespace(load_version=lambda key: version)
    monkeypatch.setattr(check_prepared_ocr.AuditSettings, 'load',
        lambda: SimpleNamespace(blob_store_url='unused', database_url='unused'))
    monkeypatch.setattr(check_prepared_ocr, 'open_blob_store', lambda url: blobs)
    monkeypatch.setattr(check_prepared_ocr, 'open_repository', lambda *args, **kwargs: repo)
    monkeypatch.setattr(check_prepared_ocr, 'load_snapshot', lambda *args: index)
    monkeypatch.setattr(sys, 'argv', ['check_prepared_ocr', '--checkpoint', str(checkpoint),
        '--annotations', str(annotation_path), '--out', str(out)])
    if failure in {'reading', 'structure'}:
        with pytest.raises(SystemExit):
            check_prepared_ocr.main()
    else:
        check_prepared_ocr.main()
    saved = json.loads(out.read_text(encoding='utf-8'))
    if failure == 'structure':
        assert saved['failed'] == 0
        assert failed_structure(saved, 'colspan')


def test_expected_table_rejects_stale_source_page_metadata():
    index, annotations = structured_sample()
    index.tables[0][0]['page'] = 2
    result = evaluate(index, 'source', annotations)
    assert result['failed'] == 0
    assert failed_structure(result, 'table_page')[0]['reason'] == 'table_page_differs'


def test_duplicate_annotation_ids_are_not_silently_resolved():
    index, annotations = structured_sample()
    annotations['cases'].append(deepcopy(annotations['cases'][-1]))
    result = evaluate(index, 'source', annotations)
    assert result['failed'] == 0
    assert failed_structure(result, 'annotation_case_ids')
