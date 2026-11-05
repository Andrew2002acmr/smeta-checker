"""Download pinned official OCR weights during image build; never upload documents."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from header_ocr_model import DEFAULT_PATH,REVISION,SHA256,SOURCE


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',type=Path,default=Path(DEFAULT_PATH))
    target=parser.parse_args().target
    target.mkdir(parents=True,exist_ok=True)
    path=target/'rus.traineddata'
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=SHA256:
        with urlopen(SOURCE,timeout=120) as response:
            raw=response.read(30_000_000)
        if hashlib.sha256(raw).hexdigest()!=SHA256:
            raise ValueError('Downloaded OCR weights do not match pinned SHA-256')
        temporary=path.with_suffix('.tmp')
        temporary.write_bytes(raw)
        temporary.replace(path)
    with urlopen(f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{REVISION}/LICENSE',timeout=60) as response:
        (target/'LICENSE.txt').write_bytes(response.read(100_000))
    (target/'manifest.json').write_text(json.dumps(dict(model='tessdata_best/rus',source=SOURCE,
        revision=REVISION,sha256=SHA256,usage='uncertain header crops only'),indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
