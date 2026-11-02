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
