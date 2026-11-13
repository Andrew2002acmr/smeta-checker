"""Resolve prepared evidence paths and explicitly corroborated native geometry."""

from hashlib import sha256
from itertools import pairwise
import json
import re

from audit_models import position_source_dict
from normative_source_addresses import cell_source_handles


_WORK = re.compile(r'^\s*(?:устройство|установка|монтаж|возведение)\s+', re.I)
_SECTION = re.compile(r'^\s*раздел\s+\d+(?:\.\d+)*[.):]?\s*', re.I)
_WORDS = re.compile(r'[а-яё]{3,}', re.I)
_ENDING = re.compile(r'(иями|ами|ого|ему|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)$')
_HEADING = re.compile(r'^[ \t]*(?P<mark>\*\*|_+)[^\n]{1,95}?(?P=mark)[^\n]{0,50}$', re.M)
_THICKNESS = re.compile(r'\bтолщин[а-яё]*\s+(\d+(?:[.,]\s*\d+)?)\s*(мм|см|м)\b', re.I)
_DECLARED_THICKNESS = re.compile(r'\bh\s*=\s*\d+(?:[.,]\s*\d+)?\s*(?:мм|см|м)\b', re.I)
_ALTERNATE_THICKNESS = re.compile(r'\bh\s*(?:(?:min|max)\s*)?[=<>≤≥]', re.I)
_CONSTRAINED = re.compile(r'\b(?:от|до|не|либо|или|кроме|около|примерно|менее|более)\b|[±<>≤≥≈~]', re.I)
_LOCATION = re.compile(r'\b(?:улиц\w*|ул\.|съезд\w*|останов\w*|тротуар\w*|участ\w*|пикет\w*|слева|справа)(?=\W|$)', re.I)
_PARTITION = re.compile(r'\b(?:(?:нижн|верхн|средн|перв|втор|трет)\w*\s+сло[йя]|'
                        r'(?:двух|трех|трёх|много)слойн\w*|слой\s*\d|'
                        r'этап\w*|очеред\w*|захватк\w*|сектор\w*|част[ьи]|кроме|только|без)\b', re.I)


def _words(text):
    words = _WORDS.findall(text.lower().replace('ё', 'е'))
    return tuple(stem if len(stem := _ENDING.sub('', word)) >= 3 else word for word in words)


def _subject(position):
    """Require the exact operation object in both the work and leaf section."""
    name = position.work_name
    source = position_source_dict(position) or {}
    sections = source.get('sections', [])
    if not sections or not _WORK.match(name):
        return ()
    section = _SECTION.sub('', sections[-1]['text']).strip(' .:')
    subject = _words(_WORK.sub('', section))
    if not subject or re.search(r'\d', section) or _LOCATION.search(section):
        return ()
    tail = _WORK.sub('', name)
    # A qualifier before the prepositional work details belongs to the object
    # name. A broad heading cannot stand for a narrower or differently named one.
    object_name = re.split(r'\s+(?:с|из|по|на|для|в|при)\s+|[,(;]', tail, maxsplit=1, flags=re.I)[0]
    return subject if _words(object_name) == subject else ()


def _operation_bigrams(text, subject):
    words = _words(text)
    excluded = {'устройств', 'толщин', 'слой', 'слоя', 'площад', 'работ', 'конструкци'} | set(subject)
    return {pair for pair in pairwise(words) if not set(pair) & excluded}


def _native_paragraphs(index, subject):
    for page, text in enumerate(index.texts, 1):
        headings = list(_HEADING.finditer(text))
        for i, heading in enumerate(headings):
            heading_words = _words(heading[0])
            if heading_words[:len(subject)] != subject:
                continue
            end = headings[i+1].start() if i+1 < len(headings) else None
            yield page, heading.start(), heading_words, text[heading.end():end], text[heading.start():end], end


def resolve_native_geometry(position, positions, tools, result):
    """Bind exact stated geometry using a unique, independently located native section."""
    from normative_parameters import clean, construction_conflict, quoted, unit_quoted
    from normative_scope_tokens import scope_tokens
    from pdf_inline_evidence import excerpt_origin
    from vor_material_links import _family

    if (tools.index is None or not tools.version or position.position_type.casefold() != 'работа'
            or len(_DECLARED_THICKNESS.findall(position.work_name)) != 1
            or len(_ALTERNATE_THICKNESS.findall(position.work_name)) != 1
            or getattr(tools, 'additional_documents', None)):
        return False
    subject = _subject(position)
    declared_sections = (position_source_dict(position) or {}).get('sections', [])
    declared_scope = ' '.join(section['text'] for section in declared_sections)
    declared = declared_scope + ' ' + position.work_name
    if (not subject or scope_tokens(declared) or _LOCATION.search(declared) or _PARTITION.search(declared)
            or any(p.index != position.index and _subject(p) == subject for p in positions.values())):
        return False
    paragraphs = list(_native_paragraphs(tools.index, subject))
    if len(paragraphs) != 1:
        return False
    page, start, title, body, context, end = paragraphs[0]
    thicknesses = list(_THICKNESS.finditer(body))
    if (end is None or title != subject or len(context) > 800 or len(thicknesses) != 1
            or _ALTERNATE_THICKNESS.search(body) or _PARTITION.search(body)
            or _words(body)[:len(subject)] != subject
            or tools.index.quality.get(page-1, {}).get('origin') != 'native'
            or excerpt_origin(tools.index, page, start, len(context)) != 'native'
            or not (_operation_bigrams(position.work_name, subject) & _operation_bigrams(body, subject))):
        return False
    thickness = thicknesses[0]
    clause_start = max(0, body.rfind('. ', 0, thickness.start()) + 2)
    # A dot followed by a newline also terminates a preceding sentence.
    clause_start = max(clause_start, body.rfind('.\n', 0, thickness.start()) + 2)
    layer = body[clause_start:thickness.start()]
    suffix = body[thickness.end():]
    wanted_family, observed_family = _family(position.work_name), _family(layer)
    if (not re.search(r'\bсло[йя]\b', layer, re.I) or _CONSTRAINED.search(layer)
            or (wanted_family and observed_family and wanted_family != observed_family)
            or construction_conflict(layer, position.work_name)
            or re.match(r'\s*(?:[-–—±+<>≤≥]|(?:до|или)\s+\d)', suffix, re.I)):
        return False
    value, unit = re.sub(r'\s+', '', thickness[1]).replace(',', '.'), thickness[2].lower()
    quote = thickness[0]
    if not quoted(value, quote, context) or not unit_quoted(unit, quote):
        return False
    # Expose exactly the same native text through the ordinary read tool. An
    # older synthetic alias, source from another pin, or a model quote cannot
    # authorize this deterministic projection.
    from normative_agent import EvidenceRequest

    proposals = result.setdefault('parameter_proposals', [])
    if any(p.get('parameter') == 'thickness' and p.get('scope_state') == 'verified' for p in proposals):
        return False
    read = tools.execute(EvidenceRequest(tool='read_page', page=page, offset=start,
                                         document_version_id=tools.version))
    source = next((entry for entry in tools.sources if entry['id'] == read.get('source_id')), None)
    if (not source or source.get('origin') != 'native' or source.get('version_id') != tools.version
            or clean(context) not in clean(source['text'])):
        return False
    if not any(s['id'] == source['id'] for s in result['sources']):
        result['sources'].append(source)
    from workspace_models import NormativeParameterProposal

    proposal = NormativeParameterProposal(parameter='thickness', value=value, unit=unit,
        source_id=source['id'], quote=quote, scope_quote=context.strip(), scope_state='verified')
    proposals.append(proposal.model_dump())
    result.setdefault('evidence_trace', []).append(dict(
        kind='unique_native_section_geometry_v1', source_id=source['id'], position_index=position.index,
        page=page, version_id=tools.version, text_offset=start, heading=' '.join(title),
        basis='exact_named_workbook_leaf_and_native_heading_with_shared_operation_phrase',
        expected_value_used_for_selection=False))
    return True


def _identity(kind, version, *address):
    raw = json.dumps([kind, version, *address], ensure_ascii=False, separators=(',', ':'))
    return f'evidence:{kind}:{sha256(raw.encode()).hexdigest()[:24]}'


def _node(kind, identity, parent=None, **fields):
    return dict(id=identity, kind=kind, parent_id=parent, **fields)


def _cell_node(kind, cell, parent, version, page, table_id, **fields):
    return _node(kind, _identity(kind, version, page, table_id, cell['cell_id']), parent,
                 cell_id=cell['cell_id'], row=cell['row'], column=cell['column'],
                 bbox=list(cell.get('bbox') or []), text=cell.get('text', ''), **fields)


def table_evidence_path(table, cell, *, version, page, source_id, source_anchor, unit=''):
    """Make a typed path from the exact prepared table used to issue the source."""
    table_id = table['table_id']
    cell_id = cell['cell_id']
    version = str(version)
    by_id = {item['cell_id']: item for item in table['cells']}
    if (by_id.get(cell_id) != cell or type(page) is not int or page < 1 or not version
            or source_id not in cell_source_handles(version, page, table_id, cell_id)
            or (str(source_anchor.get('version_id')), source_anchor.get('page'),
                source_anchor.get('table_id'), source_anchor.get('cell_id'))
            != (version, page, table_id, cell_id)
            or list(source_anchor.get('bbox') or []) != list(cell.get('bbox') or [])):
        return None

    doc = _node('document', _identity('document', version), state='pinned')
    page_node = _node('page', _identity('page', version, page), doc['id'], state='located')
    table_node = _node('table', _identity('table', version, page, table_id), page_node['id'],
                       table_id=table_id, bbox=list(table.get('bbox') or []), state='structural')
    headers = []
    missing = []
    for header_id in cell.get('header_ids') or []:
        header = by_id.get(header_id)
        if (not header or header.get('role') != 'header'
                or header['row'] >= cell['row']
                or not header['column'] <= cell['column'] < header['column'] + header.get('colspan', 1)):
            missing.append(dict(link='header_to_cell', reason='invalid_header_ancestry', id=header_id))
            continue
        parent = headers[-1]['id'] if headers else table_node['id']
        headers.append(_cell_node('header', header, parent, version, page, table_id, state='structural'))
    if not headers:
        missing.append(dict(link='header_to_cell', reason='no_structural_column_header'))

    row_scope = cell.get('row_scope') or {}
    section = None
    section_id = row_scope.get('section_id')
    if section_id:
        metadata = next((item for item in table.get('row_sections', [])
                         if item['cell_id'] == section_id), {})
        divider = by_id.get(section_id)
        if (divider and divider.get('role') == 'group'
                and metadata.get('text') == divider.get('text')
                and metadata.get('start_row', cell['row'] + 1) <= cell['row']
                < metadata.get('end_row', cell['row'])):
            section = _cell_node('row_section', divider, table_node['id'], version, page, table_id,
                                 state='structural' if metadata.get('text', '').strip() else 'unresolved')
        else:
            missing.append(dict(link='section_to_row', reason='invalid_section_interval'))
    elif row_scope.get('state') == 'total_without_section':
        missing.append(dict(link='section_to_row', reason='total_has_no_local_section'))

    row_labels = [item for item in table['cells']
                  if item['row'] <= cell['row'] < item['row'] + item.get('rowspan', 1)
                  and item['column'] < min(cell['column'], 2) and item.get('role') != 'header']
    row = _node('row', _identity('row', version, page, table_id, cell['row']),
                section['id'] if section else table_node['id'], row=cell['row'], state='structural')
    labels = [_cell_node('row_label', label, row['id'], version, page, table_id, state='structural')
              for label in sorted(row_labels, key=lambda item: (item['column'], item['row']))]
    physical_state = source_anchor.get('state', 'geometry_unavailable')
    observed = _cell_node('cell', cell, row['id'], version, page, table_id, state=physical_state,
                          source_id=source_id, header_ids=[header['id'] for header in headers])
    if physical_state != 'located':
        missing.append(dict(link='cell_to_page', reason=physical_state))
    verified_number = bool(cell.get('numeric_verified')) and physical_state == 'located'
    if not verified_number:
        missing.append(dict(link='cell_to_fact', reason='no_verified_numeric_literal'))
    if not unit:
        missing.append(dict(link='cell_to_fact', reason='no_verified_unit'))
    fact = _node('fact', _identity('fact', version, page, table_id, cell_id), observed['id'],
                 value=cell.get('text', ''), unit=unit, source_id=source_id,
                 state='literal_verified' if verified_number and unit else 'unresolved')
    missing.extend((
        dict(link='fact_to_subject', reason='document_structure_does_not_prove_engineering_scope'),
        dict(link='subject_to_vor_position', reason='no_verified_subject_binding'),
    ))
    return dict(schema=1, version_id=version, page=page, source_id=source_id,
                nodes=[doc, page_node, table_node, *headers, *([section] if section else []),
                       row, *labels, observed, fact], missing_links=missing)


def compact_evidence_path(path):
    """Give the model stable addresses and short literal context for navigation."""
    if not path:
        return None
    nodes = path['nodes']
    table = next(node for node in nodes if node['kind'] == 'table')
    cell = next(node for node in nodes if node['kind'] == 'cell')
    fact = nodes[-1]
    context = [dict(id=node['id'], cell_id=node['cell_id'], text=node['text'][:100])
               for node in nodes if node['kind'] in {'header', 'row_section', 'row_label'}
               and node['text'].strip()]
    return dict(version_id=path['version_id'], page=path['page'], table_id=table['table_id'],
                row=cell['row'], column=cell['column'], cell_id=cell['cell_id'],
                source_id=path['source_id'], context=context[-6:], literal_state=fact['state'],
                missing_links=path['missing_links'], navigation_only=True)


def engineering_path_links(graph, source_id, position_index):
    """Read the verified part of a path from an already validated engineering graph.

    The graph loader must have validated pins and source hashes. A physical
    operand of a sum is returned as such, never as that position's quantity.
    """
    entities = {node['id']: node for node in graph.get('entities', [])}
    evidence_ids = {node['id'] for node in entities.values()
                    if node['kind'] == 'Evidence' and node.get('source_id') == source_id}
    edges = [edge for edge in graph.get('relations', []) if edge['state'] == 'verified']
    fact_edges = [edge for edge in edges if edge['kind'] in {'SUPPORTED_BY', 'DERIVED_FROM'}
                  and edge['target'] in evidence_ids and entities[edge['source']]['kind'] == 'Fact']
    work_edges = [edge for edge in edges if edge['kind'] == 'CORRESPONDS_TO'
                  and entities[edge['source']].get('position_index') == position_index]
    links = []
    for work_edge in work_edges:
        for quantity in edges:
            if quantity['kind'] != 'HAS_QUANTITY' or quantity['source'] != work_edge['target']:
                continue
            for fact_edge in fact_edges:
                if fact_edge['source'] != quantity['target']:
                    continue
                fact = entities[fact_edge['source']]
                links.append(dict(fact_id=fact['id'], subject_id=quantity['source'],
                                  vor_position_id=work_edge['source'],
                                  parameter=fact.get('fact', {}).get('parameter'),
                                  evidence_role='operand' if fact_edge['kind'] == 'DERIVED_FROM' else 'direct',
                                  relation_ids=[fact_edge['id'], quantity['id'], work_edge['id']],
                                  state='verified'))
    return sorted(links, key=lambda item: tuple(item['relation_ids']))
