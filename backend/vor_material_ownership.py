"""Workbook ownership for navigation, independent of engineering derivation.

No family, unit, quantity or formula result can prove this structural relation.
Conversely, knowing the owner never proves material compatibility or conversion.
"""
from collections import Counter
from copy import deepcopy
import re


def _region(source):
    digest=source.get('document_sha256')
    sheet=source.get('sheet')
    header=source.get('header_row')
    row=source.get('row')
    if (not isinstance(digest,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',digest)
            or not isinstance(sheet,str) or not sheet.strip()
            or type(header) is not int or type(row) is not int or not 0<header<row):
        return None
    return digest.lower(),sheet,header


def _sections(source):
    sections=source.get('sections')
    return bool(isinstance(sections,list) and sections and all(isinstance(s,dict)
        and isinstance(s.get('address'),str) and s['address'].strip()
        and isinstance(s.get('text'),str) and s['text'].strip() for s in sections))


def material_owners(tasks):
    """Only explicit work -> material blocks with fully identified VOR regions."""
    regions={}
    unlocated=[]
    indices=Counter(t.get('idx') for t in tasks if type(t.get('idx')) is int)
    for task in tasks:
        source=task.get('position_source') or {}
        region=_region(source)
        if region:
            regions.setdefault(region,[]).append(task)
        else:
            unlocated.append(task)
    owners={}
    for region,entries in regions.items():
        rows=Counter(t['position_source']['row'] for t in entries)
        parent=None
        for task in sorted(entries,key=lambda t:t['position_source']['row']):
            source=task['position_source']
            index=task.get('idx')
            if (type(index) is not int or index<0 or indices[index]!=1 or rows[source['row']]!=1
                    or not _sections(source)):
                parent=None
                continue
            kind=str(task.get('position_type','')).strip().casefold()
            if kind=='работа':
                parent=task
                continue
            if kind!='материал':
                parent=None  # Never jump across an unclassified work boundary.
                continue
            if parent is None or parent['position_source']['sections']!=source['sections']:
                parent=None
                continue
            if any(type(t.get('idx')) is int and parent['idx']<t['idx']<index for t in unlocated):
                parent=None  # An unlocated row cannot safely be skipped as a boundary.
                continue
            parent_source=parent['position_source']
            expression=(task.get('calculation') or {}).get('expression') or task.get('w_formula') or ''
            owners[index]=dict(kind='vor_material_ownership',state='ownership_only',
                index=parent['idx'],number=parent.get('number') or str(parent['idx']+1),
                name=parent.get('w_name',''),row=parent_source['row'],sheet=parent_source['sheet'],
                expression=expression,formula_present=bool(str(expression).strip()),conversion_verified=False,
                provenance=dict(basis='nearest_preceding_work_in_explicit_material_block',
                    document_sha256=region[0],sheet=region[1],header_row=region[2],
                    sections=deepcopy(source['sections']),work_row=parent_source['row'],material_row=source['row'],
                    work_index=parent['idx'],material_index=index))
    return owners


def ownership_requirement(task, owner):
    """A mandatory pending check for navigation-only owners, never a green proof."""
    if not owner or owner.get('state')!='ownership_only':
        return None
    formula=owner.get('formula_present')
    reason=(f"Принадлежность материала работе №{owner['number']} установлена по структуре ВОР. ")
    reason+=('Нужно подтвердить исходное количество работы, назначение всех чисел формулы и перевод единиц/расход; '
        'структурная связь этого не доказывает.' if formula else
        'Расчёт из количества работы не задан. Собственное количество материала и его единицу нужно подтвердить '
        'спецификацией или паспортом изделия; количество работы не заменяет массу или объём материала.')
    return dict(id=f"material:conversion:{task['idx']}",label='Основание количества материала',
        role='calculation',required=True,state='needs_facts',
        declared_value=str(task.get('w_vol','')),unit=task.get('w_unit',''),reason=reason)


def navigation_owner(index, positions, relation):
    """Separate hint: never supply the formal derivation parent or children."""
    if relation.get('parent'):
        return None
    return material_owners([p.legacy_task() for p in positions.values()]).get(index)


def annotate_quantity_cards(cards, task, owner):
    """Explain unresolved ownership; unlike dimensions cannot prove a VOR error."""
    requirement=ownership_requirement(task,owner)
    if not requirement:
        return
    from normative_parameters import measure
    for card in cards:
        if card.get('id')!='quantity':
            continue
        declared=measure('1',card.get('unit',''))
        found=measure('1',card.get('found_unit',''))
        if (card['state']=='possible_mismatch' and declared and found and declared[0]!=found[0]):
            # The project figure may be the owning work's length/area. Without
            # a conversion basis it is not a comparable material quantity.
            card['state']='needs_facts'
        if card['state']=='needs_facts':
            card['reason']=requirement['reason']
