"""Literal reading of two separate unit glyphs; no expected unit or digit input."""
import time
import unicodedata
from typing import Any

VERSION = 'physical-unit-components-v2'


def _agreed_reads(attempts):
    readable = [r for r in attempts if len(r.get('words', [])) == 1 and r['words'][0].get('conf', 0) >= 75]
    def token(read):
        return unicodedata.normalize('NFKC', read['words'][0]['text']).casefold().replace('m', 'м')
    for first in readable:
        same = [r for r in readable if token(r) == token(first)]
        if len(same) >= 2 and not any(token(r) != token(first) and r['words'][0]['conf'] >= 90 for r in readable):
            return same[:2]
    return []


def read_components(crop, deadline):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    import ocr_engine
    ink = cv2.threshold(np.array(crop.convert('L')), 0, 255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    components: list[dict[str, Any]] = [dict(bbox=list(map(int, s[:4])), area=int(s[4]), reads=[])
        for s in stats[1:] if s[4] >= 3]
    result = dict(version=VERSION, image_size=list(crop.size), components=components)
    if len(components) != 2:
        return result
    for component in components:
        x, y, width, height = component['bbox']
        glyph = crop.crop((x, y, x + width, y + height))
        component['attempts'] = []
        for target in (48, 64, 96, 128):
            if time.monotonic() >= deadline:
                return result
            image = ImageOps.expand(glyph.resize((max(1, round(width * target / height)), target),
                Image.Resampling.LANCZOS), border=20, fill='white')
            # Unrestricted alphabet: a 9 must not be forced into a 2 or 3.
            words = ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(image),
                language='rus+eng', psm=10, timeout=max(.1, min(3, deadline - time.monotonic()))))
            component['attempts'].append(dict(height=target, words=words))
            component['reads'] = _agreed_reads(component['attempts'])
            if component['reads']:
                break
    return result


def verified_component_unit(proof):
    from pdf_cell_ocr import literal_unit
    reading = proof.get('component_reading') or {}
    if reading.get('version') not in {VERSION, 'physical-unit-components-v1'} or len(reading.get('components', [])) != 2:
        return None
    region = proof.get('region') or []
    if len(region) != 4 or reading.get('image_size') != [region[2] - region[0], region[3] - region[1]]:
        return None
    votes = []
    for at in range(2):
        words = []
        for component in reading['components']:
            box = component.get('bbox') or []
            reads = component.get('reads') or []
            heights = [r.get('height') for r in reads]
            if (len(box) != 4 or len(heights) != 2 or len(set(heights)) != 2
                    or any(h not in (48, 64, 96, 128) for h in heights)):
                return None
            if component.get('attempts') is not None and _agreed_reads(component['attempts']) != reads:
                return None
            x, y, width, height = box
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > reading['image_size'][0] or y + height > reading['image_size'][1]:
                return None
            glyphs = reads[at].get('words') or []
            if len(glyphs) != 1:
                return None
            words.append(dict(glyphs[0], left=x, top=y, width=width, height=height))
        votes.append(literal_unit(words))
        if votes[-1]:
            base = next(w for w in words if w['text'].casefold() in {'м', 'm'})
            exponent = next(w for w in words if w['text'] in {'2', '3'})
            if exponent['top'] + exponent['height'] > base['top'] + base['height'] * .5:
                return None
    unit = votes[0] if votes[0] and votes[0] == votes[1] else None
    attempts = proof.get('attempts', proof.get('variants', []))
    if any(literal_unit(v['words']) not in (None, unit) for v in attempts):
        return None
    return unit
