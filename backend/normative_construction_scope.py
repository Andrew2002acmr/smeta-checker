"""Repair a role-conflicting thickness citation using unique explicit PD scope.

Only explicit strengthening-layer labels are supported initially. No global
synonyms, VOR quantity lookup or lower/middle role substitution is permitted.
"""
from copy import deepcopy
import re

from normative_parameters import clean, measure, quoted

TRACE='explicit_construction_scope_repair_v1'
HEADER=re.compile(r'\bтип\s*[ivxlcdm]+\s*[-–—]',re.I)
HEIGHT=re.compile(r'\bh\s*=\s*\d+(?:[.,]\d+)?\s*(мм|см|м)\b',re.I)


def _role(text):
    matches=set(re.findall(r'\b(нижн|средн|верхн)[а-яё]*\s+сло[йя]\b',clean(text)))
    return next(iter(matches)) if len(matches)==1 else None


def _mixes(text):
    return {re.sub(r'\s+','',m[0]).replace('a','а')
        for m in re.finditer(r'щма\s*[-–]?\s*\d+|[аa]\d+\s*[а-я]{1,3}\b',clean(text))}


def _selection(position,result, *, current=False):
    """Keep every old numeric value; replace only a uniquely misplaced citation."""
    if (not re.search(r'\bсло[йя]\s+усилени',position.work_name,re.I)
            or 'асфальтобетон' not in clean(position.work_name)):
        return False
    role=_role(position.work_name)
    if not role:
        return False
    proposals=[p for p in result.get('parameter_proposals',[]) if p.get('parameter')=='thickness']
    if len(proposals)!=1:
        return False
    proposal=proposals[0]
    old=next((s for s in result['sources'] if s['id']==proposal.get('source_id')),None)
    if (not old or old.get('kind')!='project' or old.get('origin')!='native' or not old.get('version_id')
            or proposal.get('operation','direct')!='direct' or proposal.get('scope_state')!='verified'
            or not _role(proposal.get('scope_quote',''))
            or (not current and _role(proposal['scope_quote'])==role)
            or (current and _role(proposal['scope_quote'])!=role)
            or not quoted(proposal['value'],proposal['quote'],old['text'])):
        return False
    value=measure(proposal['value'],proposal['unit'])
    declared=list(HEIGHT.finditer(position.work_name))
    if len(declared)!=1:
        return False
    digits=re.search(r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',declared[0][0])
    if not digits or measure(digits[1],digits[2])!=value:
        return False
    mixes=_mixes(position.work_name) or _mixes(proposal.get('scope_quote',''))
    if len(mixes)!=1:
        return False
    sections=getattr(position.position_source,'sections',[]) if position.position_source else []
    street=any('улиц' in clean(s.get('text','') if isinstance(s,dict) else s.text) for s in sections)
    declared_context=clean(position.work_name+' '+' '.join(s.get('text','') if isinstance(s,dict) else s.text for s in sections))
    if not street or re.search(r'ровик|уширен|съезд|тротуар|останов',declared_context):
        return False  # Initial support requires an explicit street section.
    candidates=[]
    for source in result['sources']:
        if (source.get('kind')!='project' or source.get('origin')!='native'
                or source.get('version_id')!=old['version_id']
                or type(source.get('page')) is not int or source['page']<1):
            continue
        text=source['text']
        # Markdown markers must not shift offsets in literal source citations.
        normalized=re.sub(r'[*_]', ' ', text)
        headings=list(HEADER.finditer(normalized))
        for n,heading in enumerate(headings):
            end=headings[n+1].start() if n+1<len(headings) else len(text)
            block=text[heading.start():end]
            first_layer=re.search(r'(?:верхн|нижн|средн)[а-яё]*\s+сло[йя]',block,re.I)
            if not first_layer:
                continue
            title=clean(re.sub(r'[*_]', '',block[:first_layer.start()]))
            if not ('конструкци' in title and 'усилени' in title and ('улиц' in title or 'ул.' in title)):
                continue
            for match in re.finditer(r'(?:верхн|нижн)[а-яё]*\s+сло[йя]\s+покрыти[а-яё]*'
                    r'\s*\(\s*слой\s+усиления\s*\)',block,re.I):
                if _role(match[0])!=role:
                    continue
                # The first literal h after this layer must precede a new layer
                # or heading. Decimal dots in standards are not delimiters.
                tail=block[match.start():]
                height=HEIGHT.search(tail)
                if not height or height.start()>650:
                    continue
                scope=tail[:height.end()]
                if 'асфальтобетон' not in clean(scope) or _mixes(scope)!=mixes:
                    continue
                if text.count(scope)!=1:
                    continue
                if len(re.findall(r'\bсло[йя]\s+покрыти',scope,re.I))!=1:
                    continue
                digits=re.search(r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',height[0])
                if not digits or measure(digits[1],digits[2])!=value:
                    continue
                candidates.append(dict(source_id=source['id'],quote=height[0],scope_quote=scope,
                    construction_title=block[:first_layer.start()],version_id=source['version_id'],page=source.get('page')))
    unique={(c['version_id'],c['page'],c['scope_quote'],c['construction_title']):c for c in candidates}
    if len(unique)!=1:
        return False
    candidate=next(iter(unique.values()))
    if current and any(proposal.get(k)!=candidate[k] for k in ('source_id','quote','scope_quote')):
        return False
    return proposal,candidate


def verified_layer(position,result):
    selected=_selection(position,result,current=True)
    return deepcopy(selected[1]) if selected else None


def repair_thickness(position,result):
    selected=_selection(position,result)
    if not selected:
        return False
    proposal,candidate=selected
    original=deepcopy(proposal)
    proposal.update(source_id=candidate['source_id'],quote=candidate['quote'],scope_quote=candidate['scope_quote'])
    result.setdefault('evidence_trace',[]).append(dict(kind=TRACE,original_proposal=original,
        replacement_proposal=deepcopy(proposal),construction_title=candidate['construction_title'],
        scope='Explicit street strengthening construction and exact layer role; no numeric or verdict promotion'))
    return True
