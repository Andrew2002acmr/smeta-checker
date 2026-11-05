"""Download pinned free OCR weights; never send documents to a remote service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.request import urlopen

from pdf_paired_models import MODELS,ROOT


def install(root):
    for model in MODELS:
        path=Path(root)/model['directory']/model['file']
        if path.is_file()and hashlib.sha256(path.read_bytes()).hexdigest()==model['sha256']:
            continue
        url=f"https://raw.githubusercontent.com/tesseract-ocr/{model['repository']}/{model['revision']}/{model['file']}"
        with urlopen(url,timeout=60)as response:
            raw=response.read(80_000_001)
        if len(raw)>80_000_000 or hashlib.sha256(raw).hexdigest()!=model['sha256']:
            raise ValueError('Downloaded paired OCR model hash mismatch: '+model['id'])
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_bytes(raw)
        os.replace(temporary,path)
    manifest=[dict(model,source=f"https://raw.githubusercontent.com/tesseract-ocr/{model['repository']}/{model['revision']}/{model['file']}")for model in MODELS]
    (Path(root)/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(os.environ.get('SMETA_PAIRED_TESSDATA_ROOT',ROOT)))
    args=parser.parse_args()
    install(args.root)


if __name__=='__main__':
    main()
