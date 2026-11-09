from types import SimpleNamespace
from unittest.mock import patch

import pytest

from header_ocr_model import _verified


def test_missing_weights_are_explicit_and_corrupt_weights_fail_closed(tmp_path):
    assert _verified(str(tmp_path)) is None
    (tmp_path/'rus.traineddata').write_bytes(b'not pinned model bytes')
    _verified.cache_clear()
    with pytest.raises(ValueError,match='hash mismatch'):
        _verified(str(tmp_path))
    _verified.cache_clear()


def test_header_model_changes_processed_identity_but_not_raw_ocr_contract():
    from analysis_replay import algorithm,prepared_identity
    record=dict(project_id='project')
    try:
        with patch('header_ocr_model.configured',return_value=None):
            algorithm.cache_clear()
            before=prepared_identity(record,b'PDF bytes','version')
        with patch('header_ocr_model.configured',return_value={'sha256':'pinned weights'}):
            algorithm.cache_clear()
            after=prepared_identity(record,b'PDF bytes','version')
        assert before['algorithm']!=after['algorithm']
        assert before['inputs']==after['inputs']  # Reuse original page OCR.
    finally:
        algorithm.cache_clear()


def test_new_model_reopens_only_header_observations_that_lack_its_reading():
    from pdf_header_lines import VERSION,pending_pages
    header=dict(cell_id='h',role='header',text='(5см)',bbox=[0,0,10,10],
        header_line_observation=dict(version=VERSION,header_best_model_sha256=None))
    index=SimpleNamespace(tables=([dict(recovery={'version':'test'},cells=[header])],))
    with patch('header_ocr_model.configured',return_value=None):
        assert not pending_pages(index)
    with patch('header_ocr_model.configured',return_value={'sha256':'pinned weights'}):
        assert pending_pages(index)==[0]
        header['header_line_observation']['header_best_model_sha256']='pinned weights'
        assert not pending_pages(index)
