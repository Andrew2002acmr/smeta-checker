"""Generic optical variants for tight crops and italic digits; no expected number."""
import hashlib
import time


def padded_rapid(clean, initial, recognize, deadline):
    from PIL import ImageOps
    from normative_parameters import number
    attempts = [dict(padding=0, words=initial)]
    selected = initial
    for padding in (8, 16):
        if time.monotonic() >= deadline or (len(selected) == 1 and
                selected[0].get('confidence', 0) >= .9 and number(selected[0]['text']) is not None):
            break
        words = recognize(ImageOps.expand(clean, border=padding, fill='white'))
        attempts.append(dict(padding=padding, words=words))
        if len(words) == 1 and number(words[0]['text']) is not None:
            if not selected or words[0].get('confidence', 0) > selected[0].get('confidence', 0):
                selected = words
    return selected, attempts


def italic_reads(clean, read, deadline):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    from ocr_engine import _to_png
    for shear in (.15, .25):
        if time.monotonic() >= deadline:
            return
        array = cv2.warpAffine(np.array(clean), np.array([[1, shear, 0], [0, 1, 0]], dtype=np.float32),
            (clean.width + round(clean.height * shear), clean.height), borderValue=(255,))
        corrected = Image.fromarray(array)
        digest = hashlib.sha256(_to_png(corrected)).hexdigest()
        for height in (40, 48, 64):
            if time.monotonic() >= deadline:
                return
            resized = corrected.resize((max(1, round(corrected.width * height / corrected.height)), height),
                Image.Resampling.LANCZOS)
            image = ImageOps.expand(resized, border=20, fill='white')
            words = read(_to_png(image), language='eng', psm=7, timeout=max(.1, min(3, deadline - time.monotonic())))
            yield dict(height=height, psm=7, shear=shear, transform_image_sha256=digest, words=words)
