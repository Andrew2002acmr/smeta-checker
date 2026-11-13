"""Typed location identifiers for discovery; never amount or applicability proofs."""
import re
import unicodedata
from decimal import Decimal
from typing import Any

LABELS=re.compile(r'\b(участ(?:ок|ка|ке)|съезд(?:а|е)?|секци[яи]|пут[ьи]|опор[аы]|ось|оси|захватк[аи])'
    r'\s*(?P<marker>№|#|номер\s+|No\.?\s*|N\s*)?\s*(?P<identifier>[a-zа-яё0-9]+(?:[./-]\d+)?)\b',re.I)
CHAINAGE=re.compile(r'(?<!\w)(?:[пpπ][кkκ]|пикет)\s*(\d+)\s*\+\s*(\d+(?:[.,]\d+)?)',re.I)


def scope_tokens(text):
    text=unicodedata.normalize('NFKC',str(text).replace('№','#'))
    output=set()
    for match in LABELS.finditer(text):
        identifier=match['identifier']
        if (not any(c.isdigit() for c in identifier) and not re.fullmatch('[IVX]+',identifier)
                and not (len(identifier)==1 and (identifier.isupper() or match['marker']))):
            continue
        raw=match[1].casefold()
        label=next(canonical for start,canonical in [('участ','участок'),('съезд','съезд'),('секц','секция'),
            ('пут','путь'),('опор','опора'),('ос','ось'),('захват','захватка')] if raw.startswith(start))
        if identifier.isdecimal():
            identifier=str(int(identifier))
        identifier=identifier.casefold()
        output.add(f'scope:{label}:{identifier}')
    for match in CHAINAGE.finditer(text):
        offset=format(Decimal(match[2].replace(',','.')).normalize(),'f')
        output.add(f'scope:пк:{int(match[1])}+{offset}')
    return output


def compare_scopes(requested, observed):
    if not requested:
        return 'not_requested'
    if requested<=observed:
        kinds={key.rsplit(':',1)[0] for key in requested}
        if any(key not in requested and key.rsplit(':',1)[0]in kinds for key in observed):
            return 'includes_requested_and_other_scopes'
        return 'matched'
    observed_kinds={key.rsplit(':',1)[0] for key in observed}
    if any(key.rsplit(':',1)[0] in observed_kinds and key not in observed for key in requested):
        return 'conflicting'
    return 'not_observed'


def physical_row_labels(table) -> list[dict[str, Any]]:
    """Raw first-column observations locate scope; they never allocate totals."""
    return [dict(cell_id=c['cell_id'],row=c['row'],column=0,text=c['text']) for c in table['cells']
        if c.get('role')=='cell' and c.get('column')==0 and c.get('text','').strip()
        and not (c.get('geometry_validation')or{}).get('requires_source_review')]
