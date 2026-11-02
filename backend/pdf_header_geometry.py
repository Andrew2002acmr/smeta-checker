"""Independent literal proof for an isolated h=<decimal>m raster header.

The decimal crop is located from the printed equals sign and physical glyphs,
never from a requested thickness. This proof supplies text, not column units.
"""
import hashlib
import re
import time
import unicodedata
from decimal import Decimal

VERSION='physical-header-geometry-v1'


def literal(text):
    text=re.sub(r'\s+','',unicodedata.normalize('NFKC',text).casefold())
    match=re.fullmatch(r'h=(\d{1,8}(?:[.,]\d{1,8})?)[мm]',text)
    return (Decimal(match[1].replace(',','.')),'м') if match else None


def whole_line(variants):
    if len(variants)!=2 or [v.get('scale') for v in variants]!=[1,1.35]:
        return None
    texts=[]
    for variant in variants:
        words=variant.get('words') or []
        if not words or any(w.get('conf',0)<50 for w in words):
            return None
        texts.append(' '.join(w['text'] for w in words))
    values=[literal(text) for text in texts]
    return texts[0] if values[0] and values[0]==values[1] else None


def _geometry_candidate(variants):
    """One ambiguous OCR unit may trigger independent geometry verification.

    Never usable as a header on its own: RapidOCR and numeric-only crops must
    still verify the complete literal before geometry_text returns anything.
    """
    complete=whole_line(variants)
    if complete:
        return complete
    if len(variants)!=2 or [v.get('scale') for v in variants]!=[1,1.35]:
        return None
    texts=[]
    for variant in variants:
        words=variant.get('words') or []
        if not words or any(w.get('conf',0)<50 for w in words):
            return None
        texts.append(re.sub(r'\s+','',''.join(w['text'] for w in words)).casefold())
    matches=[re.fullmatch(r'h=(\d+(?:[.,]\d+)?)([мm]|™)',text) for text in texts]
    if not all(matches):
        return None
    values=[Decimal(m[1].replace(',','.')) for m in matches if m]
    full=[text for text in texts if literal(text)]
    return full[0] if len(full)==1 and values[0]==values[1] else None


def numeric_region(components,image_size):
    """Find one equals sign, one prefix glyph, decimal glyphs, and one m glyph."""
    if len(image_size)!=2 or any(v<=0 for v in image_size) or len(components)<5:
        return None
    width,height=image_size
    if any(len(c)!=5 or min(c)<0 or min(c[2:])<=0 or c[0]+c[2]>width
            or c[1]+c[3]>height for c in components):
        return None
    max_height=max(c[3] for c in components)
    bars=[c for c in components if c[2]>=3*c[3] and c[3]<=max_height*.3]
    pairs=[]
    for i,a in enumerate(bars):
        for b in bars[i+1:]:
            overlap=min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0])
            if (overlap>=min(a[2],b[2])*.65 and abs(a[1]-b[1])>min(a[3],b[3])
                    and abs(a[1]-b[1])<max_height*.6):
                pairs.append((a,b))
    if len(pairs)!=1:
        return None
    a,b=pairs[0]
    left=min(a[0],b[0])
    right=max(a[0]+a[2],b[0]+b[2])
    prefix=[c for c in components if c[0]+c[2]<left]
    suffix=sorted((c for c in components if c[0]>right),key=lambda c:c[0])
    if len(prefix)!=1 or len(suffix)<2 or len(prefix)+len(suffix)+2!=len(components):
        return None
    if prefix[0][3]<max_height*.5:
        return None
    unit=suffix[-1]
    digits=suffix[:-1]
    # A separate superscript would be a second suffix glyph, or enter the
    # decimal crop and prevent literal numeric OCR consensus below.
    if (not .4*max_height<=unit[3]<=1.1*max_height
            or not .35*unit[3]<=unit[2]<=2*unit[3]
            or unit[0]<max(c[0]+c[2] for c in digits)):
        return None
    box=[max(right+1,min(c[0] for c in digits)-2),max(0,min(c[1] for c in digits)-2),
        min(unit[0],max(c[0]+c[2] for c in digits)+2),
        min(height,max(c[1]+c[3] for c in digits)+2)]
    return box if box[2]>box[0] and box[3]>box[1] else None


def geometry_text(header_proof):
    from pdf_cell_ocr import numeric_agreement
    from normative_parameters import number
    proof=header_proof.get('geometry_crop') or {}
    text=_geometry_candidate(header_proof.get('variants',[]))
    rapid=proof.get('rapid_words') or []
    if (not text or proof.get('version')!=VERSION or len(rapid)!=1
            or rapid[0].get('confidence',0)<.9 or literal(rapid[0].get('text',''))!=literal(text)):
        return None
    expected=numeric_region(proof.get('components') or [],proof.get('image_size') or [])
    variants=proof.get('variants') or []
    heights=[v.get('height') for v in variants]
    if (expected is None or expected!=proof.get('region') or heights not in ([32,48],[32,48,64])
            or not numeric_agreement(str(literal(text)[0]),variants)):
        return None
    # numeric_agreement rejects any >=75 confidence contradictory value.
    # Require literal complete decimal words, no h, unit or unknown glyphs.
    readable=[v for v in variants if min((w.get('conf',0) for w in v.get('words',[])),default=0)>=90]
    if len(readable)<2 or any(number(''.join(w['text'] for w in v['words']))!=literal(text)[0] for v in readable):
        return None
    return text


def read_geometry(clean,header_proof,deadline,cache_dir=None,store_image=None):
    import cv2
    import numpy as np
    import ocr_engine
    from PIL import Image,ImageOps
    from pathlib import Path
    from pdf_cell_ocr import _rapid_numeric
    if not _geometry_candidate(header_proof.get('variants',[])) or time.monotonic()>=deadline:
        return None
    ink=cv2.threshold(np.array(clean.convert('L')),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    _,_,stats,_=cv2.connectedComponentsWithStats(ink)
    components=[list(map(int,c)) for c in stats[1:] if c[4]>=3]
    region=numeric_region(components,list(clean.size))
    if region is None:
        return None
    crop=clean.crop(region)
    raw=ocr_engine._to_png(crop)
    digest=hashlib.sha256(raw).hexdigest()
    proof=dict(version=VERSION,region=region,image_size=list(clean.size),components=components,
        image_sha256=digest,rapid_engine='RapidOCR PP-OCRv5 Cyrillic mobile recognition',
        rapid_words=_rapid_numeric(clean),variants=[])
    header_proof['geometry_crop']=proof
    if store_image is not None:
        proof['image_key']=store_image(raw).key
    if cache_dir is not None:
        directory=Path(cache_dir)/'header-images'
        directory.mkdir(parents=True,exist_ok=True)
        (directory/(digest+'.png')).write_bytes(raw)
    for height in (32,48,64):
        if time.monotonic()>=deadline:
            break
        scaled=crop.resize((max(1,round(crop.width*height/crop.height)),height),Image.Resampling.LANCZOS)
        scaled=ImageOps.expand(scaled,border=20,fill='white')
        words=ocr_engine.parse_tsv(ocr_engine.run_tsv(ocr_engine._to_png(scaled),language='eng',
            psm=7,timeout=max(.1,min(3,deadline-time.monotonic()))))
        proof['variants'].append(dict(height=height,words=words))
        confirmed=geometry_text(header_proof)
        if confirmed:
            return confirmed
    return None
