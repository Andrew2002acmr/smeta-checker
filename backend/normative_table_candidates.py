"""Locate symmetric grand-total columns without searching for a VOR quantity.

These are evidence candidates, never an engineering verdict. OCR confidence,
units, material and construction scope must still pass the normal validators.
"""
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal

from normative_parameters import number, unit_quoted, material_fractions
from normative_retrieval import terms
from pdf_cell_ocr import column_unit
from normative_header_observations import discovery_readings


def grouped_totals(table, query, *, unit='', height_cm=None, include_single=False,
                   include_parameter_alternatives=False):
    cells = table['cells']
    headers = {c['cell_id']: c for c in cells if c.get('role') == 'header'}
    totals = [c['row'] for c in cells if c['text'].strip().casefold().rstrip(':') == 'всего']
    if not totals:
        return []  # Do not add intermediate subtotals or adjacent page totals.
    total_row = max(totals)
    groups = defaultdict(list)
    for cell in cells:
        if cell['row'] != total_row or number(cell['text']) is None:
            continue
        ancestry = [headers[i] for i in cell.get('header_ids', []) if i in headers]
        signature = []
        for header in ancestry:
            if header.get('colspan', 1) >= table['col_count']:
                continue  # Drawing title and axis group are not column identity.
            text = unicodedata.normalize('NFKC', header['text'].split('\nЕдиница по Tesseract:')[0]).casefold()
            text = re.sub(r'\b(?:справа|слева|правый|левый)\b', '', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if not re.search(r'[а-яa-z]{2,}|\bh\s*=', text):
                continue  # Do not treat corrupt unit glyphs as material names.
            # Keep the sub-column offset: area and volume can share one
            # physical header, but must never be added to each other.
            # A category spanning several narrower material groups is shared
            # context, not a measure sub-column. Its page-wide offset must not
            # split symmetric left/right groups of the same material. Keep
            # offsets on the actual material/measure branch so area and volume
            # can never merge merely because the broad heading is identical.
            span=header.get('colspan',1)
            structural_parent=any(child.get('row',-1)>header.get('row',-1)
                and 1<child.get('colspan',1)<span
                and header['column']<=child['column']
                and child['column']+child.get('colspan',1)<=header['column']+span for child in ancestry)
            offset=0 if structural_parent else cell['column']-header['column']
            signature.append((text.replace('m', 'м'),offset,span))
        if signature:
            groups[tuple(signature)].append((cell, ancestry))
    wanted = terms(query)
    fractions = material_fractions(query)
    from vor_material_links import _family
    wanted_family=_family(query)
    output = []
    for signature, members in groups.items():
        if not (1 if include_single else 2) <= len(members) <= 12:
            continue
        text = ' | '.join(dict.fromkeys(h['text'] for _, ancestry in members for h in ancestry))
        observed=[dict(header_id=h['cell_id'],text=reading,recognition_verified=False)
            for h in {h['cell_id']:h for _,ancestry in members for h in ancestry}.values()
            for reading in discovery_readings(h) if wanted & terms(reading)]
        # Alternative OCR helps find a column; it never repairs literal text,
        # proves a thickness/unit, or relaxes a known material constraint.
        discovery_terms=terms(text).union(*(terms(item['text'])for item in observed))
        score = len(wanted & discovery_terms)
        if not score:
            continue
        thicknesses = [Decimal(v.replace(',', '.'))*(100 if u.lower() in {'m','м'} else 1)
            for v,u in re.findall(r'(?:\bh\s*=\s*|[([]\s*)(\d+(?:[.,]\d+)?)\s*(см|cm|[мm])\b', text, re.I)]
        found_family=_family(text)
        if not found_family and re.search(r'\b[аa]\d{1,3}[а-яa-z]+|щма\s*[-–]?\s*\d',text,re.I):
            found_family='асфальтобетон'
        if wanted_family and found_family and wanted_family!=found_family:
            continue
        found_fractions = material_fractions(text)
        if fractions and found_fractions and fractions.isdisjoint(found_fractions):
            continue
        parameter_mismatches=[]
        missing_parameters=['thickness'] if height_cm is not None and not thicknesses else []
        if height_cm is not None and thicknesses and number(height_cm) not in thicknesses:
            if not include_parameter_alternatives:
                continue
            parameter_mismatches.append(dict(parameter='thickness',requested_cm=str(number(height_cm)),
                observed_cm=sorted({str(v) for v in thicknesses}),
                requires_normative_transform_check=True))
        known_units = [column_unit(table, c) for c,_ in members]
        if unit and any(u and not unit_quoted(unit,u) for u in known_units):
            continue
        if height_cm is not None and number(height_cm) in thicknesses:
            score += 10
        if unit and any(unit_quoted(unit, ' '.join(h['text'] for h in ancestry)) for _,ancestry in members):
            score += 2
        values = [number(c['text']) for c,_ in members]
        output.append(dict(score=score, row=total_row, columns=[c['column'] for c,_ in members],
            cell_ids=[c['cell_id'] for c,_ in members],
            operation='direct' if len(members) == 1 else 'sum',
            values=[c['text'] for c,_ in members],
            calculated_sum=str(sum((v for v in values if v is not None), Decimal(0))),
            headers=list(dict.fromkeys(s[0] for s in signature)),
            matched_header_observations=observed[:3],
            matched_header_observation_count=len(observed),
            numeric_verified=all(c.get('numeric_verified') for c,_ in members),
            unit_verified=bool(unit) and all(u and unit_quoted(unit,u) for u in known_units),
            parameter_mismatches=parameter_mismatches,
            missing_parameters=missing_parameters,
            parameter_match_state=('conflicting' if parameter_mismatches else 'not_observed' if missing_parameters
                else 'matched' if height_cm is not None else 'not_requested'),
            discovery_only=True,
            scope_verified=False))
    return sorted(output, key=lambda c: -c['score'])
