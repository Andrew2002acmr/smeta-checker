from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ocr_engine import run_tsv


def test_tsv_mode_does_not_depend_on_tessdata_configs_directory():
    header=b'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    with patch('ocr_engine.shutil.which',return_value='/bin/tesseract'),patch('ocr_engine.subprocess.run',
            return_value=SimpleNamespace(stdout=header,stderr=b'',returncode=0)) as run:
        assert run_tsv(b'png',language='rus',tessdata_dir='/pinned-model')==header.decode()
    cmd=run.call_args.args[0]
    assert 'tessedit_create_tsv=1' in cmd and '--tessdata-dir' in cmd and 'tsv' not in cmd


def test_plain_text_success_is_a_technical_error_instead_of_empty_ocr():
    with patch('ocr_engine.shutil.which',return_value='/bin/tesseract'),patch('ocr_engine.subprocess.run',
            return_value=SimpleNamespace(stdout='Покрытие'.encode(),stderr=b'',returncode=0)):
        with pytest.raises(RuntimeError,match='no TSV header'):
            run_tsv(b'png')


@pytest.mark.parametrize('oem',[0,1])
def test_pinned_engine_mode_is_passed_without_changing_tsv_contract(oem):
    header=b'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    with patch('ocr_engine.shutil.which',return_value='/bin/tesseract'),patch('ocr_engine.subprocess.run',
            return_value=SimpleNamespace(stdout=header,stderr=b'',returncode=0))as run:
        assert run_tsv(b'png',language='rus',tessdata_dir='/pinned',oem=oem)==header.decode()
    cmd=run.call_args.args[0]
    assert cmd[cmd.index('--oem')+1]==str(oem)
    assert 'tessedit_create_tsv=1'in cmd
