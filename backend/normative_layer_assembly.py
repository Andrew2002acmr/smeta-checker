"""Structural technological-layer pairs, independent of equal plan areas.

This describes a declared thickness assembly. It never establishes a common
area, overlap, or eligibility to reuse a full-layer quantity for each operation.
"""
import re
from decimal import Decimal

from audit_models import position_source_dict
from normative_local_checks import _layer, _unit
from vor_material_ownership import _region, _sections


def layer_assembly(position, positions):
    own=_layer(position.work_name)
    source=position_source_dict(position)
    if (not own or not own['height'] or not source or source.get('format')!='xlsx' or not _region(source) or not _sections(source)
            or str(position.position_type).casefold()!='работа' or _unit(position.unit)!='м2'):
        return None
    region=_region(source)
    entries=[]
    for other in positions.values():
        src=position_source_dict(other)
        if src and _region(src)==region:
            entries.append((src['row'],other,src))
        elif other.index!=position.index and _region(src or {}) is None:
            # Unlocated rows are not safe to skip across a prospective pair.
            if abs(other.index-position.index)<=2:
                return None
    entries.sort(key=lambda row:row[0])
    if (len({row for row,_,_ in entries})!=len(entries)
            or len({other.index for _,other,_ in entries})!=len(entries)):
        return None
    matches=[i for i,(_,other,_) in enumerate(entries) if other.index==position.index]
    if len(matches)!=1:
        return None
    own_at=matches[0]
    step=1 if own['stage']==1 else -1
    for at in range(own_at+step,len(entries) if step==1 else -1,step):
        row,other,src=entries[at]
        if src.get('sections')!=source['sections']:
            return None
        kind=str(other.position_type).casefold()
        if kind=='материал':
            continue
        if kind!='работа':
            return None
        layer=_layer(other.work_name)
        if (not layer or layer['signature']!=own['signature'] or layer['stage']==own['stage']
                or _unit(other.unit)!='м2' or not layer['height']):
            return None
        own_height=Decimal(str(own['height']))
        other_height=Decimal(str(layer['height']))
        return dict(height=own_height+other_height,first=own_height,second=other_height,
            kind=own['kind'],other_row=row,work_indices=sorted([position.index,other.index]),
            operation_quantities=[str(position.volume),str(other.volume)],
            quantity_shared=False,basis='explicit_adjacent_work_stages_same_workbook_section')
    return None


def assembly_scope_matches(position,proposal,source):
    """A summed height cannot borrow a same-height construction at another site."""
    from normative_parameters import clean
    def role(text):
        found={name for name,pattern in (
            ('stops',r'останов'),('exits',r'съезд'),('sidewalk',r'тротуар'),('street',r'улиц'))
            if re.search(pattern,text,re.I)}
        return next(iter(found)) if len(found)==1 else None
    provenance=position_source_dict(position) or {}
    expected=role(' '.join(s['text'] for s in provenance.get('sections',[])))
    scope=clean(proposal.get('scope_quote',''))
    text=re.sub(r'[*_]', '',source.get('text',''))
    if not expected or not scope or scope not in clean(text):
        return False
    if role(scope)==expected:
        return True
    headings=list(re.finditer(r'^\s*тип\s+[IVXLCDM\d]+\b[^\n]*',text,re.I|re.M))
    enclosing=[]
    for index,heading in enumerate(headings):
        end=headings[index+1].start() if index+1<len(headings) else len(text)
        if scope in clean(text[heading.start():end]):
            enclosing.append(role(heading[0]))
    return enclosing==[expected]


def recover_thickness(position,result,positions):
    """Find a literal section/role/material anchor before comparing its value."""
    from normative_parameter_anchors import parameter_candidates
    from normative_parameters import measure,material_fractions,clean,quoted
    from vor_material_links import _family
    assembly=layer_assembly(position,positions)
    if not assembly:
        return
    layer=_layer(position.work_name)
    if not layer:
        return
    role=str(layer['kind'])[:4]
    family=_family(position.work_name)
    if not family:
        return
    candidates={}
    for source in result.get('sources',[]):
        if source.get('kind')!='project' or source.get('origin')!='native' or not source.get('version_id'):
            continue
        anchors=parameter_candidates([source])
        if len(anchors)>=32:
            return  # A truncated enumeration cannot establish uniqueness.
        for proposal in anchors:
            scope=proposal.get('scope_quote','')
            found=re.search(r'\b(нижн\w*|средн\w*|верхн\w*)\s+сло[йя]',scope,re.I)
            if (proposal['parameter']!='thickness' or not found or found[1].casefold()[:4]!=role
                    or _family(scope)!=family or not assembly_scope_matches(position,proposal,source)):
                continue
            wanted,actual=material_fractions(position.work_name),material_fractions(scope)
            if wanted and not actual<=wanted:
                continue
            key=(source['version_id'],source.get('page'),measure(proposal['value'],proposal['unit']),clean(scope))
            candidates[key]=proposal
    if len(candidates)!=1:
        return
    candidate=next(iter(candidates.values()))
    existing=result.setdefault('parameter_proposals',[])
    lookup={s['id']:s for s in result['sources']}
    def same_literal_source(proposal):
        if proposal.get('source_id')==candidate['source_id']:
            return True
        source=lookup.get(proposal.get('source_id'))
        target=lookup[candidate['source_id']]
        if source is None:
            return False
        # Alias identity and the entire project envelope must agree. Do not
        # append a second reading of the exact literal already verified by the
        # model; keep its original handle and engineering scope unchanged.
        physical=source.get('physical_source_id',source['id'])
        target_physical=target.get('physical_source_id',target['id'])
        return bool(physical==target_physical
            and {k:v for k,v in source.items() if k not in {'id','physical_source_id'}}
                =={k:v for k,v in target.items() if k not in {'id','physical_source_id'}}
            and proposal.get('operation','direct')=='direct'
            and not proposal.get('operand_source_ids')
            and proposal.get('quote')==candidate.get('quote')
            and proposal.get('scope_quote')==candidate.get('scope_quote'))
    if any(p.get('parameter')=='thickness' and same_literal_source(p)
            and measure(p.get('value'),p.get('unit',''))==measure(candidate['value'],candidate['unit'])
            and p.get('scope_state')=='verified'
            and quoted(p.get('value'),p.get('quote',''),lookup[p['source_id']]['text'])
            and assembly_scope_matches(position,p,lookup[p['source_id']])
            and (clean(candidate['scope_quote']) in clean(p.get('scope_quote',''))
                or clean(p.get('scope_quote','')) in clean(candidate['scope_quote'])) for p in existing):
        return
    existing.append(dict(operation='direct',operand_source_ids=[],conditions=[],**candidate))
    result.setdefault('evidence_trace',[]).append(dict(kind='structural_layer_thickness_anchor_v1',
        position_index=position.index,source_id=candidate['source_id'],quote=candidate['scope_quote'],
        value_selected_by='unique_literal_section_role_and_material',expected_value_used_for_selection=False,
        quantity_shared=False))


ALLOCATION_PROOF = 'planned_material_allocation_v1'


def single_base_price_application(position, result, positions, relation, *, cards=None):
    """Bind two priced courses to one complete native unbound-base construction.

    Course thicknesses come from explicit model adjustment events. This proof
    never creates physical PD layers, VOR operations, areas or material amounts.
    A missing native construction or incomplete pricing plan returns no proof.
    """
    from analysis_replay import digest
    import hashlib
    from normative_conditional_selection import CONDITION, _project_thickness, _role
    from normative_parameters import build_cards, clean, material_fractions, measure
    from normative_thickness_rules import (source_definition, base_thickness,
        adjustment_rule, literal_measure)
    from normative_thickness_adjustment import AdjustmentProposal, PROPOSAL
    from vor_material_links import _family

    own = position_source_dict(position) or {}
    if (str(position.position_type).casefold() != 'работа' or _unit(position.unit) != 'м2'
            or relation.get('parent') or not _region(own) or not _sections(own)
            or position.index not in positions or any(key != row.index for key, row in positions.items())
            or positions[position.index].model_dump(mode='json') != position.model_dump(mode='json')
            or _layer(position.work_name)
            or not re.search(r'устройств\w*\s+[^.]*основан', position.work_name, re.I)):
        return None
    # No hidden second foundation work may be priced in the same VOR section.
    for other in positions.values():
        src = position_source_dict(other) or {}
        if not _region(src) or not _sections(src):
            return None
        if (_region(src) == _region(own) and src['sections'] == own['sections']
                and other.index != position.index and str(other.position_type).casefold() == 'работа'
                and re.search(r'устройств\w*\s+[^.]*основан', other.work_name, re.I)):
            return None
    sources = result.get('sources') or []
    if len({s['id'] for s in sources}) != len(sources):
        return None
    lookup = {s['id']: s for s in sources}

    def intact(source):
        # Full-text registry handles already bind their text hash. Preserve
        # that contract when this local proof consumes a selected definition.
        match = re.search(r':full:([0-9a-f]{20})$', source['id'])
        return not match or hashlib.sha256(source.get('text', '').encode()).hexdigest().startswith(match[1])
    if cards is None:
        cards = build_cards(position, {**result, 'local_work_basis': None, 'quantity_composition': None}, positions, relation)
    quantity = next((c for c in cards if c['id'] == 'quantity' and c['state'] == 'confirmed'), None)
    fact = _project_thickness(position, result, cards)
    target = re.findall(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', position.work_name, re.I)
    if (not quantity or not fact or fact['role'] is not None or fact['family'] != 'щпс'
            or len(target) != 1 or measure(*target[0]) != fact['value']):
        return None
    header = lookup[fact['source_id']]

    def site(text):
        found = {name for name, pattern in (('sidewalk', r'тротуар'), ('exits', r'съезд'),
            ('stops', r'останов'), ('widening', r'ровик|уширен')) if re.search(pattern, text, re.I)}
        if len(found) == 1:
            return next(iter(found))
        return 'street' if not found and re.search(r'улиц', text, re.I) else None

    expected = site(' '.join(s['text'] for s in own['sections']))
    fraction = material_fractions(fact['scope_quote'])
    quantity_sources = [lookup[sid] for sid in quantity.get('source_ids', []) if sid in lookup]
    if (not expected or not fraction or site(fact['scope_quote']) != expected
            or not quantity_sources or any(s.get('kind') != 'project'
                or s.get('version_id') != header.get('version_id')
                or site(s.get('text', '')) != expected for s in quantity_sources)):
        return None
    # Heading plus a following real heading positively delimit a complete type.
    # Page end alone is not proof that no second base continues on another page.
    constructions = []
    for source in sources:
        if (source.get('kind') != 'project' or source.get('origin') != 'native'
                or source.get('version_id') != header.get('version_id')
                or type(source.get('page')) is not int or source['page'] < 1
                or source.get('excerpt_truncated')):
            continue
        text = source.get('text', '')
        mask = re.sub(r'[*_]', ' ', text)  # same offsets as the literal source
        headings = list(re.finditer(r'(?mi)^\s*(?:тип\s*[IVXLCDM\d]+\b[^\n]*|конструкци[яи]\s+[^\n]+)', mask))
        for start, end in zip(headings, headings[1:], strict=False):
            if not re.match(r'\s*тип\b', start[0], re.I) or site(start[0]) != expected:
                continue
            block = text[start.start():end.start()]
            block_mask = mask[start.start():end.start()]
            bullets = list(re.finditer(r'(?m)^\s*[\uf02d\-–—•]\s+', block_mask))
            layers = [block[b.start():bullets[i+1].start() if i+1 < len(bullets) else len(block)].strip()
                for i, b in enumerate(bullets)]
            bases = [line for line in layers if re.search(r'^\s*[\uf02d\-–—•]\s*основани[ея]\s*[—–-]', line, re.I)]
            covers = [line for line in layers if re.search(r'^\s*[\uf02d\-–—•]\s*покрыти[ея]\s*[—–-]', line, re.I)]
            if (len(layers) != 2 or len(bases) != 1 or len(covers) != 1
                    or _family(bases[0]) != 'щпс' or not fraction <= {
                        m.replace(',', '.') for m in re.findall(r'(?<![\d/])0/\d+(?:[.,]\d+)?(?![\d/])', bases[0])}
                    or 'асфальт' not in covers[0].casefold()
                    or re.search(r'цемент|битум|обработан|укрепл|двухслойн|многослойн', bases[0], re.I)
                    or _role(bases[0]) is not None):
                continue
            heights = re.findall(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', bases[0], re.I)
            if len(heights) != 1 or measure(*heights[0]) != fact['value']:
                continue
            constructions.append(dict(source_id=source['id'], source_sha256=digest(source),
                version_id=source['version_id'], page=source['page'], heading_quote=text[start.start():start.end()],
                boundary_quote=text[end.start():end.end()], scope_quote=block, base_quote=bases[0]))
    if len(constructions) != 1:
        return None
    cited = {sid for item in result.get('items', []) if 'normative:applicability' in item.get('claim_ids', [])
        for sid in item.get('normative_source_ids', [])}
    if not cited <= set(lookup):
        return None
    events = [e for e in result.get('evidence_trace', []) if e.get('kind') == PROPOSAL]
    if len(events) != 2:
        return None
    conditions = []
    for source in sources:
        if source['id'] not in cited or source.get('kind') != 'normative' or source.get('origin', '').startswith('ocr'):
            continue
        first = re.search(r'При\s+толщин', source.get('text', ''), re.I)
        if not first or _family(source['text'][:first.start()]) != 'щпс' or not source.get('source_id'):
            continue
        for match in CONDITION.finditer(source['text']):
            codes = re.findall(r'\d{2}\s*-\s*\d{2}\s*-\s*\d{3}\s*-\s*\d{2,3}', match['bases'])
            codes = [re.sub(r'\s+', '', code) for code in codes]
            limit = measure(match['value'], match['unit'])
            if (not re.search(r'однослойн', match['prefix'], re.I) or _role(match['prefix']) is not None
                    or len(codes) != 2 or len(set(codes)) != 2 or not limit
                    or match['operator'].casefold() not in {'более', 'свыше'} or fact['value'][1] <= limit[1]):
                continue
            courses = []
            for event in events:
                try:
                    proposal = AdjustmentProposal.model_validate({k: v for k, v in event.items() if k != 'kind'})
                except ValueError:
                    break
                base, step = lookup.get(proposal.base_source_id), lookup.get(proposal.adjustment_source_id)
                if not base or not step:
                    break
                definition = source_definition(base)
                change = source_definition(step)
                if (not definition or not change or definition['code'] not in codes
                        or not {proposal.base_source_id, proposal.adjustment_source_id} <= cited
                        or definition['source_id'] != source['source_id']
                        or definition.get('version_id') != source.get('version_id')
                        or not intact(base) or not intact(step)
                        or clean(proposal.base_quote) not in clean(base['text'])
                        or clean(proposal.adjustment_quote) not in clean(step['text'])
                        or _family(definition['text']) != 'щпс'):
                    break
                height = base_thickness(proposal.base_quote)
                actual_height = base_thickness(definition['text'])
                rule = adjustment_rule(dict(change, text=proposal.adjustment_quote), definition)
                scale = literal_measure(base, definition['code'])
                if (not height or height != actual_height or not rule or not scale
                        or scale != literal_measure(step, change['code']) or scale[0] != 'area'
                        or proposal.signed_steps > 0 and not rule['allow_positive']
                        or proposal.signed_steps < 0 and not rule['allow_negative']):
                    break
                effective = height['value'] + proposal.signed_steps * rule['step']
                if not 0 < effective <= limit[1]:
                    break
                courses.append(dict(base_source_id=base['id'], base_code=definition['code'],
                    adjustment_source_id=step['id'], adjustment_code=change['code'], base_quote=proposal.base_quote,
                    adjustment_quote=proposal.adjustment_quote, signed_steps=proposal.signed_steps,
                    base_m=height['value'], step_m=rule['step'], effective_m=effective, measure=scale))
            if (len(courses) != 2 or {c['base_code'] for c in courses} != set(codes)
                    or courses[0]['measure'] != courses[1]['measure']
                    or sum(c['effective_m'] for c in courses) != fact['value'][1]):
                continue
            selected = [source_definition(lookup[sid]) for sid in cited if sid in lookup]
            competing = {d['code'] for d in selected if d and d['table_code'] == codes[0].rsplit('-', 1)[0]
                and d['code'] not in {c['base_code'] for c in courses}
                and d['code'] not in {c['adjustment_code'] for c in courses}}
            if competing:
                continue
            conditions.append(dict(version='single_base_price_application_v1',
                project_construction=constructions[0], project_header_source_id=fact['source_id'],
                quantity_source_ids=quantity['source_ids'], family='щпс', total_m=str(fact['value'][1]),
                condition_source_id=source['id'], condition_quote=match[0], required_base_codes=codes,
                courses=[dict(c,base_m=str(c['base_m']),step_m=str(c['step_m']),effective_m=str(c['effective_m']),
                    measure=dict(dimension=c['measure'][0],value=str(c['measure'][1]))) for c in courses],
                root_source_ids=sorted({constructions[0]['source_id'],fact['source_id'],
                    *quantity['source_ids'],source['id'],
                    *(c['base_source_id'] for c in courses),*(c['adjustment_source_id'] for c in courses)}),
                source_fingerprints={sid:digest(lookup[sid]) for sid in {
                    constructions[0]['source_id'],fact['source_id'],*quantity['source_ids'],source['id'],
                    *(c['base_source_id'] for c in courses),*(c['adjustment_source_id'] for c in courses)}},
                physical_layer_count=1, price_application_only=True))
    if len(conditions) != 1:
        return None
    proof = conditions[0]
    proof['position_index'] = position.index
    proof['rebuild_positions'] = [p.model_dump(mode='json') for _, p in sorted(positions.items())]
    proof['workbook_sha256'] = digest(proof['rebuild_positions'])
    proof['sha256'] = digest(proof)
    return proof


def single_base_price_replay_supported(position, review):
    """Publisher gate: reprove a stored price application from current inputs.

    A legacy review without this trace is unchanged. A trace is never itself
    evidence: its source inventory, raw proposals, numeric cards and position
    bank must reproduce the complete literal proof exactly.
    """
    from analysis_replay import digest
    from audit_models import VorPosition
    from normative_parameters import build_cards

    traces = [e for e in review.get('evidence_trace', []) if isinstance(e, dict)
        and e.get('kind') == 'single_base_price_application_v1']
    if not traces:
        return True
    if len(traces) != 1:
        return False
    try:
        event = traces[0]
        original = {k:v for k,v in event.items() if k != 'kind'}
        if (event.get('sha256') != digest({k:v for k,v in original.items() if k != 'sha256'})
                or event.get('workbook_sha256') != digest(event.get('rebuild_positions'))):
            return False
        rows = event['rebuild_positions']
        positions = {row['index']:VorPosition.model_validate(row) for row in rows}
        if len(positions) != len(rows) or event['position_index'] != position.index:
            return False
        rebuilt_cards = build_cards(position, {**review, 'local_work_basis':None,
            'quantity_composition':None}, positions, {})
        # Stored cards are also publisher inputs; rebuilding a good card must
        # not silently replace a changed or missing current published card.
        keys = ('id','state','found_value','found_unit','source_ids','quote')
        def numeric(cards):
            return [{k:c.get(k) for k in keys} for c in cards
                if c.get('id') in {'quantity','thickness'} or str(c.get('id','')).startswith('unit_scale:')]
        if numeric(review.get('parameter_cards', [])) != numeric(rebuilt_cards):
            return False
        rebuilt = single_base_price_application(position, review, positions, {}, cards=rebuilt_cards)
        return bool(rebuilt and rebuilt == original)
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return False


def _allocation_product(expression):
    """Exactly two positive decimal literals, independently parsed as Decimals."""
    import ast
    text = str(expression).strip().removeprefix('=').replace(',', '.').replace('×', '*')
    if len(text) > 120 or not re.fullmatch(r'[0-9.()*\s]+', text):
        return None
    try:
        tree = ast.parse(text, mode='eval').body
        if not isinstance(tree, ast.BinOp) or not isinstance(tree.op, ast.Mult):
            return None
        output = []
        for node in (tree.left, tree.right):
            literal = ast.get_source_segment(text, node)
            if (not isinstance(node, ast.Constant) or type(node.value) not in (int, float)
                    or not literal or not re.fullmatch(r'\d+(?:\.\d+)?', literal)):
                return None
            value = Decimal(literal)
            if not value.is_finite() or not 0 < value <= Decimal('1e12'):
                return None
            output.append(value)
        return output
    except (SyntaxError, ArithmeticError, ValueError):
        return None


def _allocation_marks(text):
    return {prefix.casefold().translate(str.maketrans({'m': 'м', 'k': 'к'})) + digits
        for prefix, digits in re.findall(r'\b([MМKК])\s*(\d+)\b', text)}


def material_allocation_proof(position, result, positions, relation):
    """Conserve one planned homogeneous resource across two explicit operations.

    ``positions`` is the complete VOR input inventory, never a graph's selected
    neighbours. Literal proposals must identify a complete scoped project total
    and a native construction before any comparison with workbook quantities.
    No upstream compatibility, completeness, verdict or calculation flag proves
    these facts. Work quantities and individual physical volumes are not proved.
    """
    from copy import deepcopy
    from analysis_replay import digest
    from normative_coefficient_policy import named
    from normative_parameters import (clean, material_fractions, measure, number,
        quoted, same_numeric_result, unit_quoted)
    from vor_material_links import _family
    from vor_material_ownership import material_owners

    if (not isinstance(positions, dict) or position.index not in positions
            or any(key != row.index for key, row in positions.items())
            or position.model_dump(mode='json') != positions[position.index].model_dump(mode='json')
            or str(position.position_type).casefold() != 'материал'
            or _family(position.work_name) != 'щпс'):
        return None
    # A gross/actual or loss-inclusive claim requires a different consumption
    # proof. A balanced planned allocation cannot certify that assertion.
    gross = r'брутто|gross|фактическ\w*\s+расход|с\s+уч[её]том\s+(?:всех\s+)?потерь|включая\s+потери'
    if any(re.search(gross, str(item.get('topic', '')) + '\n' + str(item.get('explanation', '')), re.I)
            for item in result.get('items', []) if isinstance(item, dict)):
        return None
    owners = material_owners([p.legacy_task() for p in positions.values()])
    owner = owners.get(position.index)
    work = positions.get(owner['index']) if owner else None
    assembly = layer_assembly(work, positions) if work else None
    if not assembly:
        return None
    work_indices = assembly['work_indices']
    children = [[i for i, o in owners.items() if o['index'] == w] for w in work_indices]
    if any(len(group) != 1 for group in children):
        return None
    material_indices = [group[0] for group in children]
    if position.index not in material_indices:
        return None
    provenance = position_source_dict(work)
    if provenance is None:
        return None
    entries = [(p, position_source_dict(p)) for p in positions.values()]
    region = _region(provenance)
    section = [(p, s) for p, s in entries if s and _region(s) == region
        and s.get('sections') == provenance['sections']]
    if (len({s['row'] for _, s in section}) != len(section)
            or any(not s or not _region(s) or not _sections(s) for _, s in entries)):
        return None
    for p, s in section:
        cells = s.get('cells') or {}
        for column, value in [('B', p.work_name), ('C', p.unit), ('I', p.comment), ('K', p.position_type)]:
            if column+str(s['row']) in cells and str(cells[column+str(s['row'])]) != str(value):
                return None
        if 'E'+str(s['row']) in cells and str(cells['E'+str(s['row'])]) != p.formula:
            return None
        if 'D'+str(s['row']) in cells and not same_numeric_result(cells['D'+str(s['row'])], p.volume):
            return None
    # No second pair, unowned copy or differently specified resource of this
    # fraction may hide elsewhere in the same explicit workbook section.
    fraction = material_fractions(position.work_name)
    homogeneous = [p.index for p, _ in section if str(p.position_type).casefold() == 'материал'
        and _family(p.work_name) == 'щпс' and material_fractions(p.work_name) == fraction]
    if not fraction or set(homogeneous) != set(material_indices):
        return None
    sources = result.get('sources') or []
    if (not all(isinstance(s, dict) and s.get('id') for s in sources)
            or len({s['id'] for s in sources}) != len(sources)):
        return None
    lookup = {s['id']: s for s in sources}

    def table_scope(s):
        # The registry validates the physical table/cell/header/review binding;
        # only this narrow consumer interprets its literal body row labels.
        from normative_table_headers import validated_table_scope_context
        context = validated_table_scope_context(s)
        if not context:
            return None
        if (not all(clean(h['text']) in clean(s.get('text', '')) for h in context['header_cells'])
                or not s.get('text', '').rstrip().endswith(context['total_cell']['text'].strip())):
            return None
        labels = [clean(row['text']) for row in context['row_labels']]
        numbers = []
        for label in labels:
            match = re.fullmatch(r'съезд\s*(?:№|no)\s*(\d+)', label)
            if match:
                numbers.append(int(match[1]))
            elif not re.fullmatch(r'ул\.?\s+[а-яё][а-яё .-]+', label):
                return None
        sections = clean(' '.join(s['text'] for s in provenance['sections']))
        if (not numbers or len(set(numbers)) != len(numbers)
                or sorted(numbers) != list(range(1, max(numbers)+1))
                or not re.search(r'съезд', sections) or re.search(r'останов|тротуар', sections)):
            return None
        return context

    def literal(p, parameter):
        s = lookup.get(p.get('source_id')) or {}
        scope, quote = p.get('scope_quote', ''), p.get('quote', '')
        context = table_scope(s) if parameter == 'quantity' and s.get('table_scope_context') else None
        if (p.get('parameter') != parameter or p.get('operation', 'direct') != 'direct'
                or p.get('operand_source_ids') or p.get('conditions')
                or p.get('scope_state') != 'verified' or not scope
                or s.get('kind') != 'project' or not s.get('version_id')
                or type(s.get('page')) is not int or s['page'] < 1
                or s.get('excerpt_truncated') or not quoted(p.get('value'), quote, s.get('text', ''))
                or clean(scope) not in clean(s.get('text', '')) or clean(quote) not in clean(scope)
                or not unit_quoted(p.get('unit', ''), quote)
                or (s.get('table_scope_context') and not context and parameter == 'quantity')
                or (context and number(context['total_cell']['text']) != number(p.get('value')))
                or not (assembly_scope_matches(work, p, s) or context)):
            return False
        origin = s.get('origin', '')
        if origin == 'native':
            return True
        if context and origin == 'ocr':
            return bool(s.get('numeric_verified') is True and s.get('unit_verified') is True
                and measure('1', s.get('unit', '')) == measure('1', p.get('unit', '')))
        # A checked number alone does not check its column/header or site.
        return bool(origin == 'ocr_verified_header' and s.get('numeric_verified') is True
            and s.get('unit_verified') is True and measure('1', s.get('unit', '')) == measure('1', p.get('unit', ''))
            and any(f.get('parameter') == parameter
                and measure(f.get('value'), f.get('unit', '')) == measure(p.get('value'), p.get('unit', ''))
                for f in s.get('verified_parameters', [])))

    proposals = result.get('parameter_proposals') or []
    if not all(isinstance(p, dict) for p in proposals):
        return None
    constructions = []
    for p in proposals:
        s = lookup.get(p.get('source_id')) or {}
        scope = p.get('scope_quote', '')
        role = re.search(r'\b(нижн\w*|средн\w*|верхн\w*)\s+сло[йя]', scope, re.I)
        if (literal(p, 'thickness') and s.get('origin') == 'native' and role
                and role[1].casefold()[:4] == str(assembly['kind'])[:4]
                and _family(scope) == 'щпс' and material_fractions(scope) == fraction):
            constructions.append(p)
    # Select by source/site/role/fraction, never by desired thickness or total.
    unique = {(p['source_id'], clean(p['scope_quote']), measure(p['value'], p['unit'])): p
        for p in constructions}
    if len(unique) != 1:
        return None
    construction = next(iter(unique.values()))
    if measure(construction['value'], construction['unit']) != measure(str(assembly['height']), 'см'):
        return None
    totals = [p for p in proposals if literal(p, 'quantity') and _unit(p.get('unit')) == 'м3'
        and _family(p['scope_quote']) == 'щпс' and material_fractions(p['scope_quote']) == fraction
        and re.search(r'\bвсего\b', p['scope_quote'], re.I)]
    unique = {(p['source_id'], clean(p['scope_quote']), measure(p['value'], p['unit'])): p for p in totals}
    if len(unique) != 1:
        return None
    volume = next(iter(unique.values()))
    total_source, construction_source = lookup[volume['source_id']], lookup[construction['source_id']]
    total = number(volume['value'])
    if (not total or total <= 0 or total_source['version_id'] != construction_source['version_id']
            or not re.search(r'\bh\s*=\s*\d', volume['scope_quote'], re.I)):
        return None
    from normative_parameters import construction_conflict
    if construction_conflict(volume['scope_quote'], position.work_name, assembly['height']):
        return None
    # A declared project constraint for a technological part outranks pooling.
    # Fail closed: this proof does not reinterpret independently scoped parts.
    for s in sources:
        if s.get('kind') == 'project' and s.get('version_id') == total_source['version_id']:
            text = s.get('text', '')
            if (material_fractions(text) & fraction and re.search(
                    r'(?:технологическ\w*\s+сло[йя]\s*[12]|сло[йя]\s*[12]|[12][ -]*(?:й|го)\s+сло[йя]'
                    r'|(?:перв\w*|втор\w*)\s+(?:технологическ\w*\s+)?сло[йя])', text, re.I)):
                return None

    operands, specifications, coefficients = [], [], []
    marks = _allocation_marks(construction['scope_quote'] + '\n' + volume['scope_quote'])
    def standards(text):
        return set(re.findall(r'гост\s*(?:р\s*)?\d+(?:\.\d+)*\s*[-–—]\s*\d{4}', clean(text)))
    project_standards = standards(construction['scope_quote'] + '\n' + volume['scope_quote'])
    for w, i in zip(work_indices, material_indices, strict=True):
        material = positions[i]
        entries = named(material)
        factors = _allocation_product(material.formula)
        calc = material.calculation or {}
        if (re.search(gross, material.work_name + '\n' + material.comment, re.I)
                or _unit(material.unit) != 'м3' or len(entries) != 1 or not factors
                or entries[0]['purpose'] != 'коэффициент запаса материала на уплотнение'
                or _allocation_product(calc.get('expression', '')) != factors
                or material_fractions(material.work_name) != fraction
                or not standards(material.work_name + '\n' + material.comment)
                or not standards(material.work_name + '\n' + material.comment) <= project_standards
                or _allocation_marks(material.work_name + '\n' + material.comment) != marks):
            return None
        # This is the reproduced ЩПС class, not a generic material certificate.
        # Unknown density/frost/additive/etc. qualifications need their own
        # project binding; matching sibling strings cannot establish them.
        name = clean(material.work_name).replace('ё', 'е').replace('m', 'м')
        note = clean(material.comment.replace(entries[0]['literal'], '')).translate(
            str.maketrans({'m': 'м', 'k': 'к'}))
        if (not re.fullmatch(r'щебеночно-песчаная смесь (?:типа )?м\d+ (?:типа |фр\. ?)?0/\d+(?:[.,]\d+)? мм', name)
                or not re.fullmatch(r'гост\s+р\s*\d+(?:\.\d+)*-\d{4}\s+категори[яи]\s+к\d+[,;]?\s+марк[аи]\s+м\d+', note)):
            return None
        coefficient = number(entries[0]['value'])
        if not coefficient or factors.count(coefficient) != 1:
            return None
        base = factors[1] if factors[0] == coefficient else factors[0]
        product = base * coefficient
        if (not same_numeric_result(product, material.volume)
                or not same_numeric_result(product, calc.get('value')) or calc.get('error')
                or calc.get('cached_mismatch') is True):
            return None
        coefficients.append(coefficient)
        specifications.append(clean(material.work_name + '\n' + material.comment.replace(entries[0]['literal'], '')))
        operands.append(dict(work_index=w, material_index=i, declared_volume=str(base), unit='м3',
            coefficient=str(coefficient), formula=material.formula,
            calculated_material_volume=str(product), work_row=positions[w].position_source.row,
            material_row=material.position_source.row, binding=deepcopy(owners[i]['provenance'])))
    if (len(set(specifications)) != 1 or len(set(coefficients)) != 1
            or sum((Decimal(o['declared_volume']) for o in operands), Decimal(0)) != total):
        return None
    roots = sorted({volume['source_id'], construction['source_id']})
    proof = dict(kind=ALLOCATION_PROOF, position_index=position.index,
        state='verified_planned_allocation', material_indices=material_indices, work_indices=work_indices,
        quantity_role='planned_homogeneous_material_allocation', project_version_id=total_source['version_id'],
        operands=operands, project_volume=str(total), declared_volume_sum=str(total), unit='м3',
        project_basis=dict(volume=deepcopy(volume), construction=deepcopy(construction)),
        root_source_ids=roots, source_anchors=[dict(source_id=sid,
            quote=(volume if sid == volume['source_id'] else construction)['scope_quote'],
            rects=deepcopy(lookup[sid].get('rects', []))) for sid in roots],
        source_sha256={sid: digest(lookup[sid]) for sid in roots},
        source_document_sha256=(total_source.get('table_scope_context') or {}).get('provenance', {}).get('document_sha256'),
        table_scope_context=deepcopy(total_source.get('table_scope_context')),
        scope_rows=[dict(index=p.index, row=s['row']) for p, s in sorted(section, key=lambda v: v[1]['row'])],
        workbook_sha256=digest([p.model_dump(mode='json') for _, p in sorted(positions.items())]),
        individual_project_volumes_verified=False, stage_areas_verified=False,
        coefficient_applicability_verified=False, normative_rate_verified=False,
        quantity_shared=False, expected_value_used_for_selection=False)
    # Replay checks the actual formula/source inventory, not stored green flags.
    proof['rebuild_positions'] = [p.model_dump(mode='json') for _, p in sorted(positions.items())]
    proof['sha256'] = digest(proof)
    return proof


def verified_material_allocation(result, position=None):
    """Rebuild a stored proof against current literal sources and proposals."""
    from audit_models import VorPosition
    for event in result.get('evidence_trace', []):
        if not isinstance(event, dict) or event.get('kind') != ALLOCATION_PROOF:
            continue
        try:
            rows = event.get('rebuild_positions', [])
            positions = {p['index']: VorPosition.model_validate(p) for p in rows}
            if len(positions) != len(rows):
                continue
            own = positions.get(event.get('position_index'))
            if own is None or (position is not None and position.model_dump(mode='json') != own.model_dump(mode='json')):
                continue
            rebuilt = material_allocation_proof(own, result, positions, {})
            if rebuilt and rebuilt == event:
                return True
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return False
