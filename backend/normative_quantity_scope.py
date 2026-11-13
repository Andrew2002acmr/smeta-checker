"""Reject a one-sided cell as an unqualified whole-work quantity.

This only detects a literal pair of comparable columns already read from the
same pinned page. It never sums them, chooses a side, or proves project scope.
"""
import math
import re


def _header(source):
    if (source.get('kind')!='project' or ':cell:' not in source.get('id','')
            or not source.get('version_id') or not source.get('page')
            or source.get('unit_verified') is not True
            or not (source.get('origin')=='native' or source.get('numeric_verified') is True)):
        return None
    text=source.get('text','').rsplit('\n',1)[0]
    if '|' not in text or 'Единица физического столбца:' not in text:
        return None
    sides=re.findall(r'\b(?:слева|справа)\b',text,re.I)
    if len(set(side.casefold() for side in sides))!=1:
        return None
    side=sides[0].casefold()
    signature=' '.join(re.sub(r'\b(?:слева|справа)\b','{side}',text,flags=re.I).casefold().split())
    return side,signature


def _same_physical_row(left,right):
    def rect(source):
        values=source.get('rects') or []
        if len(values)!=1 or len(values[0])!=4:
            return None
        box=values[0]
        if (not all(type(value) in (int,float) and math.isfinite(value) for value in box)
                or not (0<=box[0]<box[2]<=1 and 0<=box[1]<box[3]<=1)):
            return None
        return box
    a,b=rect(left),rect(right)
    return bool(a and b and abs(a[1]-b[1])<1e-5 and abs(a[3]-b[3])<1e-5
        and (a[2]<=b[0]+1e-5 or b[2]<=a[0]+1e-5))


def partial_side_issue(proposal,sources,work_scope='',section_context=''):
    """Return a negative finding independent of the expected VOR number."""
    if proposal.get('parameter')!='quantity' or proposal.get('operation','direct')!='direct':
        return None
    selected=[s for s in sources if s.get('id')==proposal.get('source_id')]
    if len(selected)!=1 or (header:=_header(selected[0])) is None:
        return None
    source=selected[0]
    side,signature=header
    declared=(work_scope+' '+section_context).casefold()
    # Only an explicit, unique side in the owner's own work/section scopes a
    # single side. A child's name or expected number is never considered here.
    right=bool(re.search(r'\bсправа\b|\bправ(?:ая|ой|ую|ом|ые|ых)\s+(?:сторон|ос[ьи]|част)',declared))
    left=bool(re.search(r'\bслева\b|\bлев(?:ая|ой|ую|ом|ые|ых)\s+(?:сторон|ос[ьи]|част)',declared))
    if (right and not left and side=='справа') or (left and not right and side=='слева'):
        return None
    counterpart=next((other for other in sources if other.get('id')!=source['id']
        and other.get('version_id')==source['version_id'] and other.get('page')==source['page']
        and other.get('unit')==source.get('unit') and (found:=_header(other)) is not None
        and found[0]!=side and found[1]==signature and _same_physical_row(source,other)),None)
    if counterpart is None:
        return None
    return dict(kind='partial_side_quantity',source_id=source['id'],counterpart_source_id=counterpart['id'],
        reason='Выбрана только одна сторона таблицы («'+side+'»), а для той же конструкции прочитана противоположная сторона. '
            'Принадлежность одной стороны всей работе не доказана; сравнение с ВОР не выполнено. '
            'Проверь охват работы и обе колонки; при общем объёме используй проверенную сумму подходящих ячеек.')


def unresolved_layer_issue(proposal,source,work_scope,full_height_cm):
    """A full-layer table needs an established operation group for comparison."""
    from normative_parameters import construction_conflict,measure
    if (proposal.get('parameter')!='quantity' or not source or source.get('kind')!='project'
            or full_height_cm is not None or 'двухслойн' not in work_scope.casefold()):
        return None
    heights=list(re.finditer(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',work_scope,re.I))
    if len(heights)!=1 or (height:=measure(heights[0][1],heights[0][2].lower())) is None:
        return None
    scope=(source.get('text','') if ':cell:' in source.get('id','')
        or source.get('origin')=='calculated_from_cells' else proposal.get('scope_quote',''))
    if not construction_conflict(scope,'',height[1]*100):
        return None
    return ('В таблице указана другая полная толщина, а связанные технологические слои этой двухслойной работы '
        'не установлены однозначно. Охват количества ещё не доказан; сравнение с ВОР не выполнено.')
