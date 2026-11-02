"""Bind complete area columns by section, material and layer, never VOR amount."""
from copy import deepcopy
import json
import re
from typing import Any

from normative_parameters import clean, measure, number, quoted

TRACE = 'physical_schedule_layer_quantity_v2'
HEIGHT = re.compile(r'\bh\s*(min)?\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|мм|м|m)\b', re.I)


def mixes(text):
    return {re.sub(r'\s+|[-–]', '', m[0]).replace('a', 'а') for m in re.finditer(
        r'щма\s*[-–]?\s*\d+|[аa]\d+\s*[а-я]{1,3}\b', clean(text))}


def section(text):
    text = clean(text)
    specific = {key for key, pattern in [('stops', 'останов'), ('exits', 'съезд'),
        ('sidewalk', 'тротуар|лестнич')] if re.search(pattern, text)}
    return next(iter(specific)) if len(specific) == 1 else ('street' if not specific and 'улиц' in text else None)


def identity(position, materials) -> dict | None:
    if (getattr(position, 'position_type', '').casefold() != 'работа' or measure('1', position.unit) != measure('1', 'м2')
            or not re.search(r'покрыти|усилени|выравнивающ', position.work_name, re.I)
            or re.search(r'двухслойн|слой\s*[12]|\bпк\s*\d|слева|справа', position.work_name, re.I)):
        return None
    src = position.position_source
    sections = getattr(src, 'sections', []) if src else []
    scope = section(' '.join(s.get('text', '') if isinstance(s, dict) else s.text for s in sections))
    heights = list(HEIGHT.finditer(position.work_name))
    material = mixes(position.work_name + ' ' + ' '.join(materials))
    if not scope or len(heights) != 1 or len(material) != 1:
        return None
    h = heights[0]
    height = measure(h[2], h[3].lower())
    if not height:
        return None
    return dict(section=scope, mix=next(iter(material)), height=height, minimum=bool(h[1]))


def native_street_layer(position, result, key, version):
    """An explicit native construction binds the table's strengthening branch."""
    candidates = []
    for p in result.get('parameter_proposals', []):
        if p.get('parameter') != 'thickness' or p.get('scope_state') != 'verified':
            continue
        source = next((s for s in result['sources'] if s['id'] == p['source_id']), None)
        if (not source or source.get('origin') != 'native' or source.get('version_id') != version
                or not quoted(p['value'], p['quote'], source['text'])
                or measure(p['value'], p['unit']) != key['height']):
            continue
        text = re.sub(r'[*_]', '', source['text'])
        headings = list(re.finditer(r'\bтип\s*[ivxlcdm]+\s*[-–—]', text, re.I))
        for at, heading in enumerate(headings):
            block = text[heading.start():headings[at+1].start() if at+1 < len(headings) else len(text)]
            title = block.split('Трехслойное')[0]
            scope = p.get('scope_quote', '')
            if (clean(scope) in clean(block) and mixes(scope) == {key['mix']}
                    and re.search(r'проезжей\s+части.*ул\.', clean(title)) and 'усилени' in clean(title)
                    and ('выравнивающ' in clean(scope)) == ('выравнивающ' in clean(position.work_name))):
                own = list(HEIGHT.finditer(scope))
                if len(own) == 1 and bool(own[0][1]) == key['minimum']:
                    candidates.append(dict(source_id=source['id'], scope_quote=scope, construction_title=title.strip()))
    return candidates[0] if len(candidates) == 1 else None


def table_scope(table):
    full = ' '.join(c['text'] for c in table['cells'] if c.get('role') == 'header'
        and c.get('colspan', 1) == table['col_count'])
    if re.search(r'останов|съезд|тротуар|лестнич|улиц', clean(full)):
        return section(full)
    labels = [clean(c['text']) for c in table['cells'] if c['column'] == 0
        and c.get('role') != 'header' and clean(c['text']) and not re.match(r'итог|всего', clean(c['text']))]
    exits = r'съезд\s*(?:№|no)\s*\d+'
    if (sum(bool(re.fullmatch(exits, v)) for v in labels) >= 2
            and all(re.fullmatch(exits + r'|ул\.\s*[^|]+', v) for v in labels)):
        return 'exits'
    if len(labels) >= 2 and all(re.fullmatch(r'(?:\d+\s*\+\s*\d+(?:[.,]\d+)?\s*){1,2}|(?:левая|правая) ось', v) for v in labels):
        return 'street'
    return None


def select(tables, key):
    """Select headers before reading values; ambiguity never selects a subset."""
    from pdf_cell_ocr import column_unit
    from normative_quantity_sum_repair import _paired_group
    choices = []
    for page, table in tables:
        if table_scope(table) != key['section']:
            continue
        totals = [c['row'] for c in table['cells'] if c['column'] == 0 and re.fullmatch(r'\s*Всего\s*[:.]?\s*', c['text'], re.I)]
        if len(totals) != 1:
            continue
        headers = {c['cell_id']: c for c in table['cells'] if c.get('role') == 'header'}
        cells = []
        for c in table['cells']:
            if c['row'] != totals[0] or c.get('role') != 'cell':
                continue
            chain = [headers.get(h) for h in c.get('header_ids', [])]
            if not chain or any(h is None for h in chain):
                continue
            text = ' | '.join(h['text'] for h in chain if h is not None)
            hs = list(HEIGHT.finditer(text))
            if (mixes(text) == {key['mix']} and len(hs) == 1
                    and measure(hs[0][2], hs[0][3].lower()) == key['height']
                    and re.search(r'покрытие', text, re.I) and not re.search(r'основание', text, re.I)
                    and measure('1', column_unit(table, c)) == measure('1', 'м2')):
                cells.append(c)
        if not cells:
            continue
        pair = _paired_group(table, cells) if len(cells) == 2 else None
        if len(cells) != 1 and pair is None:
            return None
        if key['section'] == 'street' and (pair is None or any('усилени' not in clean(' '.join(
                headers[h]['text'] for h in c.get('header_ids', []) if h in headers)) for c in cells)):
            continue
        choices.append((page, table, cells))
    return choices[0] if len(choices) == 1 else None


def apply(context, record, index, position, result, kb, positions):
    from vor_material_ownership import material_owners
    owners = material_owners([p.legacy_task() for p in positions.values()])
    materials = [positions[i].work_name for i, owner in owners.items() if owner['index'] == index]
    from normative_base_schedule import identity as base_identity
    if identity(position, materials) is None and base_identity(position, materials, positions) is None:
        return False
    from normative_agent import EvidenceTools
    return project(position, result, EvidenceTools(context, record, index, [], kb, {}), materials, positions=positions)


def prepare(position, positions, tools, related):
    """Expose literal candidates for the model; never create a verified verdict.

    Retrieval uses table structure and work vocabulary, not the declared amount.
    The agent must request/read sources, choose scope and return its own answer.
    """
    from vor_material_ownership import material_owners
    from normative_base_schedule import identity as base_identity, native_construction, select as select_base, prepared_native_sources
    from normative_agent import EvidenceRequest
    from normative_quantity_anchors import quantity_parameter_candidates
    owners = material_owners([p.legacy_task() for p in positions.values()])
    materials = [positions[i].work_name for i, owner in owners.items() if owner['index'] == position.index]
    key = identity(position, materials) or base_identity(position, materials, positions)
    if not key or not tools.version or tools.index is None:
        return None
    base = key.get('family') == 'granular_base'
    pool: dict[str, Any] = dict(sources=[*tools.sources, *prepared_native_sources(tools)]) if base else dict(sources=tools.sources)
    native = native_construction(pool, key, tools.version) if base else None
    if base and native is None:
        return None
    tables = [(at+1, t) for at in range(len(tools.index.texts)) for t in tools._tables(at+1)]
    selected = select_base(tables, key, native) if base else select(tables, key)
    if selected is None:
        return None
    page, table, cells = selected
    request = EvidenceRequest(tool='read_table', page=page, table_id=table['table_id'],
        row_ids=[cells[0]['row']], column_ids=[c['column'] for c in cells])
    read = tools.execute(request)
    ids = [c['source_id'] for c in read.get('rows', []) if c.get('source_id')
        and c.get('cell_id') in {v['cell_id'] for v in cells}]
    if len(ids) != len(cells):
        return None
    read_requests = [request.model_dump()]
    if native is not None:
        source = next(s for s in pool['sources'] if s['id'] == native['source_id'])
        tools._add(deepcopy(source))
        tools.preferred.append(source['id'])
        read_requests.append(EvidenceRequest(tool='read_source', source_id=source['id']).model_dump())
    source_id = ids[0]
    if len(ids) > 1:
        computed = tools.execute(EvidenceRequest(tool='sum_cells', operand_ids=ids, unit=position.unit))
        source_id = computed.get('source_id')
    suggestions = [v for v in quantity_parameter_candidates(tools.sources, tools.cells, tools.quantity_sums,
        limit=32, max_chars=32000) if v['proposal']['source_id'] == source_id]
    tools.events.append(dict(kind='prepared_schedule_navigation_v1', page=page, table_id=table['table_id'],
        operand_source_ids=ids, scope_verified=False, model_decision_required=True))
    return dict(state='candidate', scope_verified=False, model_decision_required=True,
        quantity_candidates=suggestions, construction_candidate=native, read_requests=read_requests,
        checks_required=['same project section', 'same material', 'whole construction depth',
            'technological layer assembly', 'normative applicability and consumption conditions'])


def project(position, result, tools, materials, *, positions=None):
    positions = positions or {position.index: position}
    key: dict[str, Any] | None = identity(position, materials)
    from normative_base_schedule import identity as base_identity, native_construction, select as select_base
    key = key or base_identity(position, materials, positions)
    if not key or not tools.version or result.get('execution_state') == 'technical_failure':
        return False
    base = key.get('family') == 'granular_base'
    native_pool = result
    if base:
        from normative_base_schedule import prepared_native_sources
        native_pool = {**result, 'sources': [*result['sources'], *prepared_native_sources(tools)]}
    native = native_construction(native_pool, key, tools.version) if base else (
        native_street_layer(position, result, key, tools.version) if key['section'] == 'street' else None)
    if (base or key['section'] == 'street') and native is None:
        return False
    tables = [(at+1, t) for at in range(len(tools.index.texts)) for t in tools._tables(at+1)]
    selected = select_base(tables, key, native) if base else select(tables, key)
    if selected is None:
        return False
    page, table, cells = selected
    from normative_agent import EvidenceRequest
    from normative_quantity_anchors import _physical, _proposal, quantity_parameter_candidates
    read = tools.execute(EvidenceRequest(tool='read_table', page=page, table_id=table['table_id'],
        row_ids=[cells[0]['row']], column_ids=[c['column'] for c in cells]))
    lookup = {s['id']: s for s in tools.sources}
    selected_ids = [c['source_id'] for c in read.get('rows', []) if c.get('cell_id') in {v['cell_id'] for v in cells}
        and c.get('source_id')]
    physical = [_physical(sid, lookup, tools.cells) for sid in selected_ids]
    if len(physical) != len(cells) or any(p is None for p in physical):
        return False
    physical = [p for p in physical if p is not None]
    if any(p['source']['version_id'] != tools.version for p in physical):
        return False
    if len(physical) == 1:
        p = physical[0]
        proposal = _proposal(p['source'], p['value'], p['unit'], p['quote'], p['scope_quote'])
    else:
        ids = [p['source']['id'] for p in physical]
        summed = tools.execute(EvidenceRequest(tool='sum_cells', operand_ids=ids, unit='м2'))
        candidates = [c for c in quantity_parameter_candidates(tools.sources, tools.cells, tools.quantity_sums)
            if c['proposal']['source_id'] == summed.get('source_id')]
        proposal = candidates[0]['proposal'] if len(candidates) == 1 else None
    if proposal is None:
        return False
    proposal['scope_state'] = 'verified'
    from workspace_models import NormativeParameterProposal
    proposal = NormativeParameterProposal.model_validate(proposal).model_dump()
    original = [p for p in result.get('parameter_proposals', []) if p['parameter'] == 'quantity']
    if original == [proposal] and any(e.get('kind') == TRACE for e in result.get('evidence_trace', [])):
        return False
    from normative_quantity_anchors import sum_supersedes_operand
    covered = [p for p in original if sum_supersedes_operand(p, proposal, lookup)]
    if any(p.get('scope_state') == 'verified' and p != proposal and p not in covered for p in original):
        return False
    preview = deepcopy(result)
    if base and native is not None and native['source_id'] not in {s['id'] for s in preview['sources']}:
        preview['sources'].append(deepcopy(next(s for s in native_pool['sources'] if s['id'] == native['source_id'])))
    fresh = [s for s in tools.sources if s['id'] in {proposal['source_id'], *proposal['operand_source_ids']}]
    for source in fresh:
        old = next((s for s in preview['sources'] if s['id'] == source['id']), None)
        if old and (any(old.get(k) != source.get(k) for k in ('version_id', 'page', 'rects', 'origin', 'numeric_verified', 'unit', 'unit_verified'))
                or number(old['text'].rsplit('\n', 1)[-1]) != number(source['text'].rsplit('\n', 1)[-1])):
            return False
    preview['sources'] = [s for s in preview['sources'] if s['id'] not in {v['id'] for v in fresh}] + deepcopy(fresh)
    preview['parameter_proposals'] = [p for p in preview.get('parameter_proposals', []) if p['parameter'] != 'quantity'] + [proposal]
    bound_thickness = None
    if base and native is not None:
        source = next(s for s in preview['sources'] if s['id'] == native['source_id'])
        scope = native['scope_quote']
        depth = HEIGHT.search(scope)
        if depth and clean(scope) in clean(source['text']):
            # Bind thickness to the exact native layer sentence, rather than
            # a page excerpt containing lower, middle and upper layers at once.
            thickness = dict(parameter='thickness', operation='direct', operand_source_ids=[], conditions=[],
                source_id=source['id'], value=depth[2], unit=depth[3], quote=depth[0],
                scope_quote=scope, scope_state='verified')
            other = [p for p in preview['parameter_proposals'] if p['parameter'] == 'thickness'
                and measure(p['value'], p['unit']) != key['height']]
            if not other:
                preview['parameter_proposals'] = [p for p in preview['parameter_proposals']
                    if p['parameter'] != 'thickness'] + [thickness]
                bound_thickness = thickness
    removed = []
    kept = []
    superseded_claims = {'quantity'} | ({'geometry:thickness', 'thickness'} if bound_thickness else set())
    for item in preview['items']:
        if item['state'] == 'needs_facts' and superseded_claims.intersection(item.get('claim_ids', [])):
            removed.append(deepcopy(item))
            residual = [v for v in item['claim_ids'] if v not in superseded_claims]
            if residual:
                kept.append({**item, 'claim_ids': residual})
        else:
            kept.append(item)
    preview['items'] = kept
    if bound_thickness:
        preview['items'].append(dict(topic='Толщина той же конструкции ПД', state='confirmed', required=True,
            claim_ids=['geometry:thickness'], project_source_ids=[bound_thickness['source_id']],
            normative_source_ids=[], explanation=bound_thickness['scope_quote']))
    from normative_parameters import build_cards
    card = next(c for c in build_cards(position, preview, positions, {}) if c['id'] == 'quantity')
    if card['state'] != 'confirmed':
        # Preserve located evidence for the next step, without converting
        # uncertain precision or a conflict into an automatic VOR error.
        event = dict(kind='located_schedule_area_v1', value=proposal['value'], unit=proposal['unit'],
            proposal=proposal, source_records=deepcopy(fresh), physical=[p['provenance'] for p in physical],
            native_layer=native, comparison_state=card['state'], reason=card['reason'], model_requests=0)
        if event in result.get('evidence_trace', []):
            return False
        result.setdefault('evidence_trace', []).append(event)
        return True
    preview['items'].append(dict(topic='Количество по собственной колонке ведомости ПД', state='confirmed', required=True,
        claim_ids=['quantity'], project_source_ids=[proposal['source_id']], normative_source_ids=[],
        explanation='Полный итог раздела, материала и толщины слоя: ' + proposal['quote'] + '. ' + card['reason']))
    serialized_key = json.loads(json.dumps({**key, 'height': str(key['height'][1])}, default=str))
    preview.setdefault('evidence_trace', []).append(dict(kind=TRACE, identity=serialized_key,
        version_id=tools.version, page=page, table_id=table['table_id'], cell_ids=[c['cell_id'] for c in cells],
        native_layer=native, original_proposals=deepcopy(original), superseded_items=removed,
        proposal=proposal, physical_trace=deepcopy(tools.events), model_requests=0))
    result.update(preview)
    return True
