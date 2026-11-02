"""Discover table operands by headers and physical columns, never by expected value.

Prepared OCR is an optimisation. A new PDF can build its own cached page catalog.
Only the supported left/right total layout is accepted; ambiguity stays explicit.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import UUID
from typing import Any

import pymupdf

from normative_corpus import decimal
from normative_local_checks import layer_group, area_columns


def table_query(position, related, sources) -> dict[str, Any] | None:
    if position.unit.strip().rstrip('.') not in {'м2', 'м²'}:
        return None
    kind = next((word for word in ('нижнего', 'среднего', 'верхнего') if word+' слоя' in position.work_name.casefold()), None)
    height = re.search(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*см', position.work_name, re.I)
    if not kind or not height or not re.search(r'ЩПС|щеб[её]ночно.песчан', position.work_name, re.I):
        return None
    group = layer_group(position, related)
    if 'двухслойн' in position.work_name.casefold() and not group:
        return None
    depth = group['height'] if group else decimal(height[1])
    if depth is None or depth <= 0:
        return None
    noun = kind.replace('него', 'ний')
    matches = []
    for s in sources:
        if s['kind'] != 'project' or s['origin'] != 'native':
            continue
        for m in re.finditer(noun+r'\s+слой\s+основания(.{0,450}?)\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*см', s['text'], re.I | re.S):
            fraction = re.search(r'0/\d+(?:[.,]\d+)?', m[1])
            if fraction and decimal(m[2]) == depth:
                matches.append((fraction[0], s['id']))
    if len({m[0] for m in matches}) != 1:
        return None
    sections = ' '.join((s.get('text','') if isinstance(s,dict) else s.text) for s in (position.position_source.sections if position.position_source else []))
    scopes = [stem for stem in ('останов', 'съезд', 'тротуар') if stem in sections.casefold()]
    return dict(fraction=matches[0][0], thickness_m=depth/100, unit='м2', scope_terms=scopes,
        native_ids=list(dict.fromkeys(m[1] for m in matches)))


def relevant_table(text, query):
    folded = text.casefold().replace('ё', 'е')
    if any(term not in folded for term in query.get('scope_terms', [])):
        return False
    parts = re.fullmatch(r'0/(\d+)[,.](\d+)', query['fraction'])
    fraction = (r'(?<!\d)0\s*[/|]\s*'+parts[1]+r'\s*[.,]?\s*'+parts[2]+r'(?!\d)' if parts else re.escape(query['fraction']))
    return bool(re.search(fraction, folded) and re.search(r'площад|объем', folded)
        and re.search(r'всего|итог|ведомост|слева|справа', folded))


def related_rows(record, position):
    own = position.position_source
    if not own:
        return []
    sections = [s.model_dump(mode='json') if hasattr(s, 'model_dump') else s for s in own.sections]
    return [dict(work=p['work_name'], quantity=p.get('volume'), unit=p.get('unit'), row=p['position_source'].get('row'))
        for p in record['config'].get('position_inputs') or [] if p['index'] != position.index
        and abs(p['index']-position.index) <= 7 and (p.get('position_source') or {}).get('sheet') == own.sheet
        and (p.get('position_source') or {}).get('sections') == sections]


def augment(context, record, position, result):
    """Automatic local retrieval over a pinned PDF; no provider/model call."""
    import ocr_engine
    from normative_position_check import project_sources, _excerpt
    from workspace_models import NormativePositionCheckRequest
    if position.unit.strip().rstrip('.') not in {'м2', 'м²'} or not re.search(r'ЩПС|щеб[её]ночно.песчан', position.work_name, re.I):
        return result
    versions = {s.get('version_id') for s in result['sources'] if s['kind'] == 'project' and s.get('version_id')}
    if len(versions) != 1:
        return result
    version = next(iter(versions))
    if version not in (record['config'].get('pdf_versions') or {}).values():
        return result
    output = deepcopy(result)
    deadline = time.monotonic() + 60
    ref = context.repository.load_version(UUID(version))
    pdf = context.blob_store.get(ref.blob_key)
    sha = hashlib.sha256(pdf).hexdigest()
    related = related_rows(record, position)
    with pymupdf.open(stream=pdf, filetype='pdf') as doc:
        # Native construction descriptions can be absent from lexical top pages.
        for i in range(len(doc)):
            text = str(doc[i].get_text(sort=True) or '')
            if re.search(r'(?:нижний|средний|верхний)\s+слой\s+основания', text, re.I):
                if not any(s['id'] == f'p{i+1}' for s in output['sources']):
                    output['sources'].append(dict(id=f'p{i+1}',kind='project',label=ref.filename,page=i+1,
                        text=_excerpt(text),origin='native',version_id=version,image_key=None,words_key=None,engine=''))
        query = table_query(position, related, output['sources'])
        if not query:
            return result
        # Read the preparation cache by content hash and current extraction algo.
        prepared = {}
        root = Path(os.environ.get('NORMATIVE_PREPARED_CACHE_ROOT', '.audit_cache/pdf'))
        for path in (root/sha).glob('ocr-*/page-*.json'):
            if time.monotonic() + 15 > deadline:
                return result
            try:
                page = json.loads(path.read_text(encoding='utf-8'))
                if page.get('pdf_sha256') == sha and page.get('settings', {}).get('algo') == ocr_engine.ALGO_VERSION:
                    prepared.setdefault(page['page'], []).append(page.get('text', ''))
            except (OSError, ValueError, KeyError):
                continue
        candidates = []
        scan_count = sum(len(re.sub(r'\s','',str(doc[i].get_text()))) < 100 and i not in prepared for i in range(len(doc)))
        if scan_count > 128:
            return result  # Bounded catalog; never silently sample a large PDF.
        for i in range(len(doc)):
            page = doc[i]
            text = str(page.get_text(sort=True) or '')
            if i in prepared:
                text += '\n'+'\n'.join(prepared[i])
            elif len(re.sub(r'\s','',text)) < 100:
                if time.monotonic() + 10 > deadline:
                    return result
                def overview():
                    pix = page.get_pixmap(dpi=100)
                    words = ocr_engine.parse_tsv(ocr_engine.run_tsv(pix.tobytes('png'),language='rus+eng',psm=11,timeout=8))
                    return dict(text=' '.join(w['text'] for w in words if w['conf'] >= 35))
                try:
                    cached = context.repository.cached_normative_page(record['id'], f'quantity-catalog:{version}:{i+1}:{ocr_engine.ALGO_VERSION}:v1', overview)
                    text += '\n'+cached['text']
                except (OSError, RuntimeError, ValueError):
                    return result  # Incomplete catalog cannot establish uniqueness.
            if relevant_table(text, query):
                candidates.append(i+1)
    if not candidates or len(candidates) > 4:
        return result
    warnings = []
    pages = []
    for candidate in candidates:
        if time.monotonic() + 15 > deadline:
            return result
        pages += project_sources(context, record, position, [], NormativePositionCheckRequest(
            pdf_version_id=version, pages=[candidate]), warnings)
    proofs = []
    for source in pages:
        if not source.get('words_key'):
            continue
        data = json.loads(context.blob_store.get(source['words_key']))
        groups = area_columns(data, query['fraction'], query['thickness_m'])
        if groups:
            proofs.append((source, groups))
    if len(proofs) != 1:
        return result
    source, groups = proofs[0]
    for s in pages:
        if not any(v['id'] == s['id'] for v in output['sources']):
            output['sources'].append(s)
    total = sum((g['value'] for g in groups), Decimal(0))
    declared = decimal(position.volume)
    if declared is None:
        return result
    rounded = total.quantize(Decimal(1).scaleb(int(declared.as_tuple().exponent)), rounding=ROUND_HALF_UP)
    output['quantity_composition'] = dict(state='confirmed' if rounded == declared else 'possible_mismatch',
        unit=query['unit'], total=str(total), rounded_total=str(rounded), declared_value=str(declared),
        source_id=source['id'], page=source['page'], terms=[dict(label=g['side'],value=str(g['value']),
            rect=g['quantity_rects'][0]) for g in groups])
    if output != result:
        output['local_validation_version'] = 0
    return output
