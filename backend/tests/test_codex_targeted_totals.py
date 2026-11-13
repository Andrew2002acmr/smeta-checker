from types import SimpleNamespace
from unittest.mock import patch

from pdf_targeted_cells import _is_total, _matches_leaf, corroborate_scope_text, find_totals, verify_scope_binding


def test_split_total_label_and_distinct_numeric_designation():
    assert _is_total('Вс его:')
    assert not _is_total('Всего по смете')
    assert _matches_leaf('БР 100.20.8, м', ['бр', '100', '20', '8'], 'м')
    assert not _matches_leaf('БР 100.20.80, м', ['бр', '100', '20', '8'], 'м')
    assert not _matches_leaf('БР 100.8.20, м', ['бр', '100', '20', '8'], 'м')


def test_ambiguous_total_keeps_addresses_and_binds_selected_column(tmp_path):
    def item(text, bbox, slots):
        return dict(text=text, bbox=bbox, slots=slots)

    title = item('Участок А', [0, 0, 30, 10], [0, 0, 0, 2])
    first = item('Проезжая часть БР 100.30.18, м', [10, 10, 20, 20], [1, 1, 1, 1])
    second = item('Тротуар БР 100.30.18, м', [20, 10, 30, 20], [1, 1, 2, 2])
    total = item('Вс его:', [0, 20, 10, 30], [2, 2, 0, 0])
    cells = [item('', [10, 20, 20, 30], [2, 2, 1, 1]), item('', [20, 20, 30, 30], [2, 2, 2, 2])]
    table = dict(cells=[title, first, second, total, *cells], rows=[total], headers=[title, first, second])
    page = SimpleNamespace(number=111)
    options = dict(pdf_sha256='a'*64, cache_dir=tmp_path)
    with patch('pdf_targeted_cells._page_structure', return_value=([table], False)), \
            patch('pdf_targeted_cells._decimal', return_value=None), \
            patch('pdf_targeted_cells._read_crop_text', return_value=''):
        broad = find_totals(page, scope='участок', column='бр 100 30 18', unit='м', **options)
        assert broad['status'] == 'ambiguous'
        assert [c['bbox'] for c in broad['candidates']] == [c['bbox'] for c in cells]
        narrow = find_totals(page, scope='участок', column='тротуар бр 100 30 18', unit='м', **options)
        assert narrow['status'] == 'unreadable'
        assert narrow['reason'] == 'numeric_consensus_missing'
        binding = narrow['scope_binding']
        assert verify_scope_binding(page, bbox=cells[1]['bbox'], binding=binding, **options)
        assert not verify_scope_binding(page, bbox=cells[0]['bbox'], binding=binding, **options)


def test_header_budget_reports_incomplete_search(tmp_path):
    headers = []
    for i in range(17):
        headers.append(dict(text='Нечитаемый заголовок', bbox=[0, i, 20, i+1], slots=[0, 0, 0, 2]))
    row = dict(text='Всего', bbox=[0, 20, 10, 30], slots=[1, 1, 0, 0])
    cell = dict(text='', bbox=[10, 20, 20, 30], slots=[1, 1, 1, 1])
    tables = [dict(cells=[header, row, cell], headers=[header], rows=[row]) for header in headers]
    with patch('pdf_targeted_cells._page_structure', return_value=(tables, False)), \
            patch('pdf_targeted_cells._read_crop_text', return_value=''):
        result = find_totals(SimpleNamespace(number=0), pdf_sha256='a'*64, cache_dir=tmp_path,
                             scope='другой участок', column='длина', unit='м')
    assert result['status'] == 'search_incomplete'
    assert result['reason'] == 'header_search_budget_exhausted'


def test_scope_text_corroboration_reads_original_crop_instead_of_cached_label():
    title, leaf = [0, 0, 30, 10], [10, 10, 20, 20]
    binding = {
        'query': {'scope': 'участок север', 'column': 'бордюр 10 20'},
        'unit': 'м',
        'headers': [
            {'text': 'Участок Север', 'bbox': title, 'slots': [0, 0, 0, 2]},
            {'text': 'Бордюр 10.20, м', 'bbox': leaf, 'slots': [1, 1, 1, 1]},
        ],
    }
    with patch('pdf_targeted_cells._read_crop_text', side_effect=lambda _, bbox, **options:
               'Участок Юг' if bbox == title else 'Бордюр 10.20, м'):
        assert not corroborate_scope_text(None, binding)
    with patch('pdf_targeted_cells._read_crop_text', side_effect=lambda _, bbox, **options:
               'Участок Север' if bbox == title else 'Бордюр 10.20, м'):
        assert corroborate_scope_text(None, binding)
