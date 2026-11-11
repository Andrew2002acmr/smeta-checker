"""Pinned optional high-accuracy model, used only on uncertain header crops."""
import hashlib
import os
from functools import lru_cache
from pathlib import Path

REVISION='e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
SHA256='b617eb6830ffabaaa795dd87ea7fd251adfe9cf0efe05eb9a2e8128b7728d6b6'
SOURCE=f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{REVISION}/rus.traineddata'
DEFAULT_PATH='/opt/smeta/header-tessdata'


@lru_cache(maxsize=4)
def _verified(directory):
    path=Path(directory)/'rus.traineddata'
    if directory in {None, ''} or not path.exists():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest()!=SHA256:
        raise ValueError('Header OCR model hash mismatch')
    return dict(tessdata_dir=str(path.parent),model='tessdata_best/rus',sha256=SHA256,revision=REVISION,source=SOURCE)


def configured():
    return _verified(os.environ.get('SMETA_HEADER_TESSDATA_DIR',DEFAULT_PATH))
