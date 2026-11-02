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
    if any(p.get('parameter')=='thickness' and p.get('source_id')==candidate['source_id']
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
