"""Bind a granular base to a complete schedule group, without a VOR amount.

Blank OCR headings are not rewritten. A missing depth is tolerable only when
the two physical side branches and a unique native construction prove it.
"""
import re

from normative_parameters import clean, material_fractions, measure

DEPTH = re.compile(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|мм|м|m)\b', re.I)
FAMILY = re.compile(r'щпс|щеб[её]ночно[\s-]*песчан', re.I)


def identity(position, materials, positions):
    from normative_layer_assembly import layer_assembly
    if (getattr(position, 'position_type', '').casefold() != 'работа'
            or measure('1', position.unit) != measure('1', 'м2')
            or not FAMILY.search(position.work_name)
            or re.search(r'\bпк\s*\d|слева|справа', position.work_name, re.I)):
        return None
    role = re.findall(r'(нижн|средн|верхн)\w*\s+слоя\s+основания', position.work_name, re.I)
    depths = list(DEPTH.finditer(position.work_name))
    fractions = material_fractions(position.work_name + ' ' + ' '.join(materials))
    if len(role) != 1 or len(depths) != 1 or len(fractions) != 1:
        return None
    assembly = layer_assembly(position, positions)
    if 'двухслойн' in position.work_name.casefold() and not assembly:
        return None
    depth = measure(str(assembly['height']), 'см') if assembly else measure(*depths[0].groups())
    if not depth or depth[1] <= 0:
        return None
    src = position.position_source
    sections = ' '.join(s.text if hasattr(s, 'text') else s['text'] for s in (src.sections if src else []))
    # A generic workbook section is allowed only with a unique typed native
    # construction below. Explicit subsites may never borrow another subsite.
    return dict(family='granular_base', role=role[0].lower(), fraction=next(iter(fractions)),
        height=depth, sections=sections, assembly=assembly)


def native_construction(result, key, version):
    found = {}
    for source in result['sources']:
        if (source.get('kind') != 'project' or source.get('origin') != 'native'
                or source.get('version_id') != version):
            continue
        text = re.sub(r'[*_]', '', source['text'])
        headings = list(re.finditer(r'\bтип\s+[ivxlcdm\d]+\s*[-–—][^\n]*', text, re.I))
        for at, heading in enumerate(headings):
            title = clean(heading[0])
            block = text[heading.end():headings[at+1].start() if at+1 < len(headings) else len(text)]
            for match in re.finditer(key['role'] + r'\w*\s+слой\s+основания[^;\n]*(?:\n(?![^\n]*слой)[^;\n]*){0,4}', block, re.I):
                clause = match[0]
                # Bound at the literal depth; never absorb the next layer.
                depths = list(DEPTH.finditer(clause))
                if not depths:
                    continue
                depth = depths[0]
                clause = clause[:depth.end()]
                if (not FAMILY.search(clause) or material_fractions(clause) != {key['fraction']}
                        or measure(*depth.groups()) != key['height']):
                    continue
                scope = 'widening' if 'ровик' in title and 'уширени' in title else (
                    'exits' if 'съезд' in title else 'stops' if 'останов' in title else None)
                if scope is None:
                    continue
                declared = clean(key['sections'])
                if 'улиц' in declared and scope != 'widening':
                    continue
                if any(word in declared and word not in title for word in ('съезд', 'останов', 'тротуар', 'ровик')):
                    continue
                found[(version, clean(heading[0]), clean(clause))] = dict(
                    source_id=source['id'], construction_title=heading[0], scope=scope,
                    scope_quote=clause, height_value=depth[1], height_unit=depth[2])
    return next(iter(found.values())) if len(found) == 1 else None


def prepared_native_sources(tools):
    """Read the frozen native layer descriptions; never request fresh OCR."""
    quality = getattr(tools.index, 'quality', {})
    for at, text in enumerate(tools.index.texts):
        if (quality.get(at, {}).get('origin') != 'native' or not FAMILY.search(text)
                or not re.search(r'слой\s+основания', text, re.I)):
            continue
        yield dict(id=f'prepared:native:{at+1}', kind='project', origin='native',
            version_id=tools.version, page=at+1, label=tools.filename, text=text, rects=[])


def select(tables, key, native):
    from pdf_cell_ocr import column_unit
    choices = []
    for page, table in tables:
        headers = {c['cell_id']: c for c in table['cells'] if c.get('role') == 'header'}
        totals = [c['row'] for c in table['cells'] if c['column'] == 0
            and re.fullmatch(r'\s*Всего\s*[:.]?\s*', c['text'], re.I)]
        if len(totals) != 1:
            continue
        cells = []
        branches = []
        explicit_depths = []
        invalid = False
        for cell in table['cells']:
            if cell['row'] != totals[0] or cell.get('role') != 'cell':
                continue
            chain = [headers.get(h) for h in cell.get('header_ids', [])]
            if not chain or any(h is None for h in chain):
                continue
            chain = [h for h in chain if h is not None]
            text = ' | '.join(h['text'] for h in chain)
            if not FAMILY.search(text) or material_fractions(text) != {key['fraction']}:
                continue
            if measure('1', column_unit(table, cell)) != measure('1', 'м2'):
                continue
            scope = clean(text)
            if native['scope'] == 'widening' and not ('ровик' in scope and 'уширени' in scope):
                continue
            if native['scope'] == 'exits' and 'съезд' not in scope:
                continue
            if native['scope'] == 'stops' and 'останов' not in scope:
                continue
            sides = [h for h in chain if clean(h['text']) in {'слева', 'справа'}]
            ds = [measure(*d.groups()) for d in DEPTH.finditer(text)]
            if (len(sides) != 1 or any(d != key['height'] for d in ds)
                    or cell.get('quality', 'usable') != 'usable'):
                invalid = True
                break
            cells.append(cell)
            branches.append(sides[0])
            explicit_depths.extend(ds)
        if invalid or len(cells) != 2 or not explicit_depths:
            continue
        # Entire symmetric physical branches, same material subcolumn offset.
        # No matching on numerical cell contents or on the declared VOR total.
        if ({clean(h['text']) for h in branches} != {'слева', 'справа'}
                or branches[0]['row'] != branches[1]['row']
                or branches[0].get('colspan', 1) != branches[1].get('colspan', 1)
                or abs(branches[0]['column'] - branches[1]['column']) != branches[0].get('colspan', 1)
                or cells[0]['column'] - branches[0]['column'] != cells[1]['column'] - branches[1]['column']):
            continue
        choices.append((page, table, cells))
    return choices[0] if len(choices) == 1 else None
