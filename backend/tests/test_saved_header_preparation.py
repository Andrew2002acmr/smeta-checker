from copy import deepcopy
from contextlib import closing
from unittest.mock import patch

import pymupdf
import pytest

from audit_core import PageTextIndex
from audit_snapshot import load_snapshot, store_json, store_snapshot
from pdf_header_preparation import complete_saved_headers
from pdf_text import PDFTextSource
from storage_blobs import FileBlobStore
from test_pdf_table_verification import table


@pytest.mark.parametrize('interrupt_after_pass', [False, True])
def test_saved_header_completion_preserves_body_and_refreshes_text_and_locators(tmp_path, interrupt_after_pass):
    blobs = FileBlobStore(tmp_path / 'blobs')
    with pymupdf.open() as doc:
        page = doc.new_page(width=200, height=200)
        page.insert_text((10, 80), 'Native paragraph ' * 10)
        raw = doc.tobytes()
        with closing(PDFTextSource(doc, cache_dir=tmp_path / 'pdf', full_document=True)) as source:
            native = source.native_state()
            t = table()
            t['row_count'] = 2
            t['header_verification'] = {'deferred_header_ids': ['h']}
            old_rows, old_locators = source._store_tables(0, [t], origin='ocr')
            packed = dict(page=1, text='native\n'+'\n'.join(old_rows), table_rows=list(old_rows),
                locators=list(old_locators), tables=[t], quality={'state': 'uncertain'},
                errors=[], geometry={}, table_error=None)
            index = PageTextIndex([packed['text']], {}, (), [old_rows], [old_locators],
                tables=[[t]], metadata={'pdf_sha256': native['pdf_sha256']}, quality={0: packed['quality']})
            state = dict(native=store_json(blobs, native), pages=[store_json(blobs, packed)],
                snapshot=store_snapshot(blobs, index))
        old_snapshot = deepcopy(state['snapshot'])
        body = deepcopy(t['cells'][1])

        def complete(page, tables, **kwargs):
            tables[0]['cells'][0]['text'] = 'Площадь, м²'
            tables[0]['header_verification']['deferred_header_ids'] = []
            kwargs['checkpoint'](tables, dict(before={'t': ['h']}, after={'t': []}))
            return dict(state='header_observation_complete', remaining={})

        committed = []
        checkpoints = 0
        def checkpoint():
            nonlocal checkpoints
            checkpoints += 1
            if interrupt_after_pass and checkpoints == 2:
                from audit_tasks import PreparationYield
                raise PreparationYield()

        with (patch('pdf_table_verification.complete_recovered_headers', side_effect=complete),
                patch('pdf_text.PDFTextSource._ocr_page_result', side_effect=AssertionError('No page OCR'))):
            if interrupt_after_pass:
                from audit_tasks import PreparationYield
                with pytest.raises(PreparationYield):
                    complete_saved_headers(blobs, raw, state, index, checkpoint=checkpoint,
                        commit=lambda value: committed.append(deepcopy(value)))
                updated = committed[-1]
                result = load_snapshot(blobs, updated['snapshot'])
                with patch.object(PDFTextSource, '__init__', side_effect=AssertionError('Repeated finished pass')):
                    updated, result = complete_saved_headers(blobs, raw, updated, result)
            else:
                updated, result = complete_saved_headers(blobs, raw, state, index)
        assert result.tables[0][0]['cells'][1] == body
        assert 'Площадь, м²' in result.texts[0]
        assert any(item['text'] == 'Площадь, м²' for item in result.locators[0])
        assert updated['snapshot'] != old_snapshot
        assert load_snapshot(blobs, old_snapshot).tables[0][0]['cells'][0]['text'] != 'Площадь, м²'
        assert load_snapshot(blobs, updated['snapshot']).tables == result.tables
