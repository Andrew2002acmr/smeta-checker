"""A cache probe must hydrate every view without rerunning OCR or rewriting proof."""
from copy import deepcopy
import json

import pytest

from pdf_text import PDFTextSource
from test_pdf_text import FakeDoc, FakePage, _fake_open_pdf, _fake_to_markdown, fake_recognize_page


def physical_table():
    proof = dict(version='numeric-crop-recovery-v1', original_text='45',
        reads=[dict(engine='rapid', text='45', confidence=.99)])
    return dict(table_id='ruled-cached', row_count=2, col_count=2, origin='ocr',
        recovery=dict(version='ruled-image-recovery-v1', geometry_digest='unchanged-grid'), cells=[
            dict(cell_id='h0', row=0, column=0, text='Work', role='header', header_ids=[],
                bbox=[0, 0, 100, 20], quality='usable', numeric_verified=False, origin='ocr'),
            dict(cell_id='h1', row=0, column=1, text='m', role='header', header_ids=[],
                bbox=[100, 0, 200, 20], quality='usable', numeric_verified=False, origin='ocr',
                header_verification=dict(version='physical-header-ocr-v2', unit='m')),
            dict(cell_id='v0', row=1, column=0, text='Copper cable', role='cell', header_ids=['h0'],
                bbox=[0, 20, 100, 40], quality='usable', numeric_verified=False, origin='ocr'),
            dict(cell_id='v1', row=1, column=1, text='45', role='cell', header_ids=['h1'],
                bbox=[100, 20, 200, 40], quality='usable', numeric_verified=True, origin='ocr',
                numeric_recovery=proof)])


@pytest.mark.parametrize('with_table', [True, False])
def test_scan_cache_preflight_reopens_all_views_without_mutating_saved_proofs(tmp_path, monkeypatch, with_table):
    monkeypatch.setattr('pdf_text.pymupdf.open', _fake_open_pdf)
    monkeypatch.setattr('pdf_text.pymupdf4llm.to_markdown', _fake_to_markdown)
    monkeypatch.setattr('pdf_text._ocr_engine.recognize_page', fake_recognize_page)
    template = [physical_table()] if with_table else []
    calls = []
    def raster(*args, **kwargs):
        calls.append('read')
        return deepcopy(template)
    monkeypatch.setattr('pdf_tables.raster_tables', raster)
    for name in ('pdf_tables.verify_numeric_cells', 'pdf_tables.recover_unit_headers',
                 'pdf_cell_ocr.refine_tables', 'pdf_table_verification.verify_recovered_tables'):
        monkeypatch.setattr(name, lambda *args, **kwargs: None)
    doc = FakeDoc([FakePage('', ocr_text='Copper cable 45 m.')])
    first = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        initial = first.for_position([0], 'Copper cable', '45').to_snapshot()
        table_path, ocr_path = first._table_path(0), first._cache_path(0)
        table_bytes, ocr_bytes = table_path.read_bytes(), ocr_path.read_bytes()
    finally:
        first.close()
    assert calls == ['read']
    second = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        current = second.for_position([0], 'Copper cable', '45', max_ocr_pages=0).to_snapshot()
        assert current == initial
        assert second.ocr_count == 0 and second.cache_hits == 1
        assert 0 in second._table_rows
        # Repeated in-memory calls must not append duplicate row text or proofs.
        assert second.for_position([0], 'Copper cable', '45', max_ocr_pages=0).to_snapshot() == initial
        assert table_path.read_bytes() == table_bytes and ocr_path.read_bytes() == ocr_bytes
        if with_table:
            cached = json.loads(table_bytes)
            assert cached['tables'][0]['cells'][-1]['numeric_recovery'] == template[0]['cells'][-1]['numeric_recovery']
            assert cached['tables'][0]['cells'][1]['header_verification'] == template[0]['cells'][1]['header_verification']
            # Decoded cache state is private to this source; the frozen payload
            # and the durable proof stay unchanged even if a consumer edits it.
            second._structured_tables[0][0]['cells'][-1]['numeric_recovery']['reads'][0]['text'] = 'different'
            assert initial['tables'][0][0]['cells'][-1]['numeric_recovery']['reads'][0]['text'] == '45'
            assert table_path.read_bytes() == table_bytes
    finally:
        second.close()
    third = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        assert third.for_position([0], 'Copper cable', '45', max_ocr_pages=0).to_snapshot() == initial
        assert third.ocr_count == 0 and calls == ['read']
    finally:
        third.close()
    # A new row-section classifier must not silently reuse the old header grid.
    # The expensive page OCR is independent and should still come from cache.
    monkeypatch.setattr('pdf_row_sections.VERSION', 'changed-section-classifier')
    fourth = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        rebuilt = fourth.for_position([0], 'Copper cable', '45', max_ocr_pages=0)
        assert list(rebuilt.texts) == initial['texts']
        assert fourth.ocr_count == 0 and fourth.cache_hits == 1
        assert calls == ['read', 'read']
        assert fourth._table_path(0) != table_path
        assert table_path.read_bytes() == table_bytes
    finally:
        fourth.close()


def test_primary_ruled_cache_reopens_tables_without_rerun_or_partial_success(tmp_path, monkeypatch):
    from copy import deepcopy
    from pdf_tables import RULED_OCR_VERSION
    monkeypatch.setattr('pdf_text.pymupdf.open', _fake_open_pdf)
    monkeypatch.setattr('pdf_text.pymupdf4llm.to_markdown', _fake_to_markdown)
    monkeypatch.setattr('pdf_text._ocr_engine.recognize_page', fake_recognize_page)
    ready = dict(text='Work 45 m\nvalue-0-0 | value-0-1 | value-0-2\nvalue-1-0 | value-1-1 | value-1-2\nvalue-2-0 | value-2-1 | value-2-2', words=[], tables=[dict(
        table_id='ruled-primary', page=1, bbox=[0, 0, 300, 90], origin='ocr',
        row_count=3, col_count=3, header_rows=1, reasons=[],
        quality='usable', recovery=dict(version='embedded-ruled-recovery-v2'),
        recognition=dict(version=RULED_OCR_VERSION, engine='rapidocr',
            rec_model='PP-OCRv5/cyrillic/mobile', state='complete', physical_cells=9),
        cells=[dict(cell_id=f'c{number}', row=row, column=column, rowspan=1, colspan=1,
            bbox=[column * 100.0, row * 30.0, (column + 1) * 100.0, (row + 1) * 30.0],
            text=f'value-{row}-{column}', words=[], numeric_verified=False, origin='ocr',
            role='header' if row == 0 else 'cell', quality='usable',
            header_ids=[] if row == 0 else [f'c{column}'],
            recognition=dict(version=RULED_OCR_VERSION, state='recognized', confidence=.99,
                readings=[dict(method='batch_line', text=f'value-{row}-{column}', confidence=.99,
                    crop_bbox=[column * 100.0, row * 30.0, (column + 1) * 100.0, (row + 1) * 30.0])]))
            for row in range(3) for column in range(3) for number in [row * 3 + column]])],
        quality=dict(state='uncertain', origin='ocr', engine='rapidocr', algo=RULED_OCR_VERSION,
            recognition_state='complete', layout=dict(resolved=False, reason='word_level_outside_tables')),
        errors=[])
    calls = []
    def producer(*args, **kwargs):
        calls.append('recognize')
        return deepcopy(ready)
    monkeypatch.setattr('pdf_tables.prepare_ruled_page', producer)
    monkeypatch.setattr('pdf_tables.verify_numeric_cells', lambda *args, **kwargs: None)
    monkeypatch.setattr('pdf_cell_ocr.refine_tables', lambda *args, **kwargs: None)
    monkeypatch.setattr('pdf_table_verification.verify_recovered_tables', lambda *args, **kwargs: None)
    doc = FakeDoc([FakePage('', ocr_text='Copper cable 45 m.')])
    first = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        initial = first.for_position([0], 'Copper cable', '45').to_snapshot()
        ruled_path = first._ruled_tables_path(0)
        table_path = first._table_path(0)
        ruled_bytes, table_bytes = ruled_path.read_bytes(), table_path.read_bytes()
        assert calls == ['recognize']
        assert 'value-2-2' in initial['texts'][0]
        assert initial['texts'][0].count('value-2-2') == 1
    finally:
        first.close()
    second = PDFTextSource(doc, cache_dir=tmp_path)
    try:
        current = second.for_position([0], 'Copper cable', '45', max_ocr_pages=0).to_snapshot()
        assert current == initial
        assert calls == ['recognize']
        assert second.ocr_count == 0 and second.cache_hits == 1
        assert ruled_path.read_bytes() == ruled_bytes
        assert table_path.read_bytes() == table_bytes
    finally:
        second.close()
    partial = deepcopy(ready)
    partial['tables'][0]['cells'][4]['recognition'].update(state='not_attempted', confidence=None, readings=[])
    partial['tables'][0]['cells'][4].update(text='', words=[])
    partial['tables'][0]['recognition']['state'] = 'incomplete'
    partial = dict(partial, errors=['incomplete_cell_observation'])
    broken = PDFTextSource(doc, cache_dir=tmp_path / 'partial')
    try:
        broken._write_ruled_page(0, partial)
    finally:
        broken.close()
    resumed = PDFTextSource(doc, cache_dir=tmp_path / 'partial')
    try:
        assert resumed._read_ruled_page(0) is None
    finally:
        resumed.close()
