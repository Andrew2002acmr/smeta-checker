"""Recover missing decimal literals from bounded crops of physical PDF cells."""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import ocr_engine
from pdf_cell_ocr import _rapid_numeric, _read_variants, _recover_numeric, clean_glyphs, recovered_numeric_value

VERSION = "ruled-decimal-crops-v2"
MAX_CELLS = 32
BUDGET_SECONDS = 25
_DECIMAL = re.compile(r"[+-]?\d+[,.]\d+")


def has_valid_recovery(record):
    """A partial checkpoint is reusable only with independently checked crop evidence."""
    if ('incomplete_cell_observation' not in record.get('errors', [])
            or record['quality'].get('recognition_state') != 'incomplete'):
        return False
    found = False
    for table in record['tables']:
        for cell in table['cells']:
            proof = cell.get('recognition', {}).get('numeric_consensus')
            if proof is None:
                continue
            if (cell.get('geometry_validation', {}).get('requires_source_review')
                    or not _DECIMAL.fullmatch(cell['text'])
                    or recovered_numeric_value(cell, proof) is None):
                return False
            found = True
    return found


def _read(data, **options):
    return ocr_engine.parse_tsv(ocr_engine.run_tsv(data, **options))


def recover(page, candidates, rotation, deadline):
    import pymupdf

    for cell_id, box in candidates:
        if time.monotonic() >= deadline:
            break
        clip = pymupdf.Rect(box) * page.rotation_matrix & page.rect
        if clip.is_empty or clip.get_area() * (600 / 72) ** 2 > 1_000_000:
            continue
        pix = page.get_pixmap(dpi=600, clip=clip, alpha=False)
        image = ocr_engine._rotate(ocr_engine._pil_from_pixmap(pix), rotation)
        clean = clean_glyphs(image)
        if clean is None:
            continue
        rapid = _rapid_numeric(clean)
        if (len(rapid) != 1 or rapid[0]['confidence'] < .9
                or not _DECIMAL.fullmatch(rapid[0]['text'])):
            continue
        cell = dict(bbox=box, text='', words=[])
        try:
            variants = _read_variants(image, numeric=True, read=_read, deadline=deadline)
            proof = dict(image_sha256=hashlib.sha256(ocr_engine._to_png(image)).hexdigest(),
                dpi=600, rotation=rotation, variants=variants)
            _recover_numeric(image, cell, proof, _read, deadline, initial_rapid=rapid)
        except (RuntimeError, ValueError):
            continue
        if cell['numeric_verified']:
            yield cell_id, rapid[0]['text'], rapid[0]['confidence'], cell['numeric_recovery']


def main():
    import pymupdf

    request, result = map(Path, sys.argv[1:])
    options = json.loads(request.read_text(encoding='utf-8'))
    deadline = time.monotonic() + BUDGET_SECONDS - 1
    with pymupdf.open(options['pdf']) as document, result.open('w', encoding='utf-8') as output:
        for observation in recover(document[0], options['cells'], options['rotation'], deadline):
            output.write(json.dumps(observation, ensure_ascii=False) + '\n')
            output.flush()


if __name__ == '__main__':
    main()
