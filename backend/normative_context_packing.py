"""Budget source previews only after all protected model metadata is present."""
from copy import deepcopy
import json
import re

from normative_embedded_context import literal_paths
from normative_thickness_rules import source_definition, adjustment_rule


def serialize_context(value):
    """The exact provider JSON: remove separator spaces, never literal content."""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _ids(value):
    found = []
    if isinstance(value, dict):
        for key, part in value.items():
            if key in {'source_id', 'base_source_id', 'adjustment_source_id'} and isinstance(part, str):
                found.append(part)
            elif key in {'source_ids', 'operand_source_ids'} and isinstance(part, list):
                found.extend(v for v in part if isinstance(v, str))
            else:
                found.extend(_ids(part))
    elif isinstance(value, list):
        for part in value:
            found.extend(_ids(part))
    return found


READ_TOOLS = {'read_source', 'read_page', 'read_request', 'read_table', 'sum_cells'}


def completed_read_ids(events):
    """Recent actual tool receipts, without retaining duplicate reply payloads."""
    return list(dict.fromkeys(sid for event in reversed(events)
        if (event.get('request') or {}).get('tool') in READ_TOOLS
        for sid in _ids(event.get('result', {}))))


def source_order(value, sources, preferred, embedded, *, read_source_ids=()):
    """Navigation priority, never selection or engineering applicability."""
    lookup = {source['id']: source for source in sources}
    definitions = {source['id']: definition for source in sources
                   if (definition := source_definition(source)) is not None}
    preferred = list(dict.fromkeys(preferred))
    primary, anchors, remaining = [], [], []

    def add(target, sid):
        if sid in lookup and sid not in target:
            target.append(sid)

    def group(sid):
        add(primary, sid)
        definition = definitions.get(sid)
        if not definition:
            return
        for source in sources:
            if (source['id'].startswith(sid + ':resources:')
                    and source.get('source_id') == definition['source_id']
                    and source.get('text', '').startswith('Ресурсы нормы ' + definition['code'] + ';')):
                add(primary, source['id'])

    # Retained model anchors/read requests precede the first retrieval candidate.
    selected = [sid for sid in _ids(value.get('previous_literal_parameters', [])) + preferred
                if sid in definitions and sid not in embedded]
    pairs = value.get('normative_thickness_adjustments') or []
    # A retained adjustment still needs its literal base(s), not merely its
    # resource row. This reverses exact validated pairs, never a table prefix.
    selected = list(dict.fromkeys([pair.get('base_source_id') for pair in pairs
        if isinstance(pair, dict) and pair.get('adjustment_source_id') in selected
        and pair.get('base_source_id') in definitions
        and adjustment_rule(definitions[pair['adjustment_source_id']], definitions[pair['base_source_id']])]
        + selected))
    if not selected:
        selected = [pair.get('base_source_id') for pair in pairs
                    if isinstance(pair, dict) and pair.get('base_source_id') in definitions][:1]
    if not selected and definitions:
        selected = [next(iter(definitions))]
    core_size=0
    for sid in dict.fromkeys(selected):
        group(sid)
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get('base_source_id') != sid:
                continue
            adjustment = pair.get('adjustment_source_id')
            if adjustment in definitions and adjustment_rule(definitions[adjustment], definitions[sid]):
                # The definition establishes the literal step; its resources
                # remain available after current project facts.
                add(primary, adjustment)
        if not core_size:
            core_size=len(primary)
    # Read construction paragraphs before competing cell candidates; numeric
    # operands alone cannot establish which physical layer the work belongs to.
    paragraphs = [sid for sid in preferred if sid in lookup and lookup[sid].get('kind') == 'project'
                  and ':cell:' not in sid and ':sum:' not in sid]
    for sid in _ids(value.get('previous_literal_parameters', [])) + paragraphs + preferred + _ids([
            value.get('parameter_candidates', []), value.get('quantity_parameter_candidates', [])]):
        if sid in lookup and lookup[sid].get('kind') == 'project':
            add(anchors, sid)
    # Full embedded clauses remain in protected metadata. Their duplicate
    # previews must not displace the selected work norm or project evidence.
    for sid in preferred:
        if sid not in embedded:
            add(remaining, sid)
    for source in sources:
        if source.get('kind') == 'normative' and source['id'] not in embedded:
            add(remaining, source['id'])
    for sid in embedded:
        add(remaining, sid)
    for source in sources:
        add(remaining, source['id'])
    # A completed explicit read must precede stale previews and earlier search
    # candidates. Its handle alone is not delivery of the requested words.
    reads, searches = [], []
    for sid in completed_read_ids(value.get('tool_results', [])) + list(read_source_ids):
        if sid not in embedded:
            add(reads, sid)
    table_reads = [sid for sid in reads if ':cell:' in sid]
    reads = [sid for sid in reads if sid not in table_reads]
    # A selected computed sum and its exact read operands form one small
    # navigation bundle. Other cells from the same table read must not push
    # out that bundle or the literal work-norm definition.
    calculations = []
    for candidate in value.get('quantity_candidates', []):
        operands = candidate.get('operand_source_ids') or []
        sid = candidate.get('source_id')
        if (candidate.get('operation') == 'sum' and operands and set(operands) <= set(table_reads)
                and lookup.get(sid, {}).get('origin') == 'calculated_from_cells'):
            for part in [sid, *operands]:
                add(calculations, part)
    # A requested new search is navigation evidence too. Otherwise many old
    # preferred norm/resource cards can consume the budget before even one
    # excerpt of the latest found clause is delivered at the final step.
    for event in reversed(value.get('tool_results', [])):
        if (event.get('request') or {}).get('tool') == 'search_norms':
            for sid in _ids(event.get('result', {})):
                if sid not in embedded:
                    add(searches, sid)
    conditions = [sid for sid in _ids([value.get('coefficient_condition_sources', []),
            value.get('material_definition_sources', [])])
        if sid in lookup and lookup[sid].get('kind') == 'normative' and sid not in embedded]
    # A directly cited clause is not a GESN work definition. Even a preferred
    # handle otherwise falls behind the larger definition/resource bundles.
    comment = (value.get('position') or {}).get('comment') or ''
    clause_refs = set(re.findall(r'\bп\.\s*(\d+(?:\.\d+)+)\b', comment, re.I))
    cited = [sid for sid in remaining if sid not in embedded and lookup[sid].get('kind') == 'normative'
             and (prefix := re.match(r'\s*(\d+(?:\.\d+)+)\b', lookup[sid].get('text') or ''))
             and prefix[1] in clause_refs]
    # Keep the first coherent base/resource/adjustment bundle intact before
    # exploring newly found alternatives. Later old candidates may yield room.
    return list(dict.fromkeys(reads + cited + primary[:core_size] + calculations + table_reads + conditions + anchors + searches
        + primary[core_size:] + remaining)), set(reads + cited + table_reads + calculations + searches + primary + conditions + anchors)


def pack_sources(value, sources, preferred=(), *, limit, planning=False, measure=None, read_source_ids=()):
    """Keep metadata intact; measure exactly what will be sent, including aliases."""
    output = deepcopy(value)
    output['sources'] = []
    embedded = literal_paths(output, sources)
    output['archived_source_ids'] = [source['id'] for source in sources if source['id'] not in embedded]
    size = measure or (lambda payload: len(serialize_context(payload)))
    if size(output) > limit:
        raise ValueError('Agent context metadata exceeds budget')
    ordered, critical = source_order(output, sources, preferred, embedded, read_source_ids=read_source_ids)
    lookup = {source['id']: source for source in sources}
    if len(lookup) != len(sources):
        raise ValueError('Agent source registry has duplicate identities')
    preferred = set(preferred)
    for sid in ordered:
        source = lookup[sid]
        text = str(source.get('text', ''))
        if not text:
            continue
        brief = {key: source[key] for key in ('id', 'kind', 'page', 'label', 'version_id', 'origin',
            'numeric_verified', 'unit_verified', 'unit', 'verified_parameters',
            'confirmed_unit_context','expert_laying_width','physical_source_id','table_scope_context',
            'header_observation_addresses','table_id') if key in source}
        from normative_paired_scope import preview as paired_parent_preview
        parent=paired_parent_preview(source)
        if parent:
            brief['paired_parent_observation']=parent
        if sid in embedded:
            cap = 160
        elif sid in critical or (sid in preferred and ':cell:' not in sid):
            cap = 3500
        elif source.get('kind') == 'normative':
            cap = 1300
        else:
            cap = 1000 if ':cell:' in sid else 500
        if planning and sid not in critical and sid not in embedded and len(text) > cap:
            cap = min(cap, 450)

        def preview(length, *, brief=brief, text=text, sid=sid):
            part = dict(brief, text=text[:length], excerpt_truncated=len(text) > length,
                        full_length=len(text), next_offset=length if len(text) > length else None)
            if sid in embedded:
                part.update(text_embedded_at=embedded[sid], embedded_text_complete=True, next_offset=None)
            return part

        # Reserve the omission list too. A late tool result never triggers an
        # unordered tail-pop after the priority decision.
        archived = [item for item in output['archived_source_ids'] if item != sid]
        candidate = dict(output, sources=[*output['sources'], preview(min(len(text), cap))],
                         archived_source_ids=archived)
        if size(candidate) <= limit:
            output = candidate
            continue
        low, high = 0, min(len(text), cap)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = dict(output, sources=[*output['sources'], preview(middle)], archived_source_ids=archived)
            if size(candidate) <= limit:
                low = middle
            else:
                high = middle - 1
        if low:
            output = dict(output, sources=[*output['sources'], preview(low)], archived_source_ids=archived)
    if size(output) > limit:
        raise ValueError('Agent context metadata exceeds budget')
    return output
