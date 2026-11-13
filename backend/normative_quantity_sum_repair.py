"""Complete an already selected physical left/right total, never a VOR total.

Only two literal, scope-verified direct reads can be replaced. The physical
header tree must prove one complete symmetric pair; all verdict gates still run.
"""
from collections import Counter
from decimal import Decimal
from itertools import combinations
import hashlib
import json
import re

from normative_parameter_feedback import literal_parameter_issues
from normative_parameters import clean, measure, number
from normative_quantity_anchors import _physical, quantity_parameter_candidates
from normative_quantity_scope import _same_physical_row


def _side(text):
    return clean(text) if clean(text) in {'слева', 'справа'} else ''


def _signature(table, cell, headers):
    chain=[headers.get(key) for key in cell.get('header_ids', [])]
    if not chain or any(h is None for h in chain):
        return None
    branches=[h for h in chain if h and _side(h['text'])]
    if len(branches)!=1:
        return None
    branch=branches[0]
    parents=branch.get('header_ids', [])
    if not parents or any(key not in cell['header_ids'] for key in parents):
        return None
    unit_header=(table.get('column_units',{}).get(str(cell['column'])) or {}).get('header_id')
    signature=[]
    for h in chain:
        if h is None:
            return None
        col, span=h['column'],h.get('colspan', 1)
        if not (col<=cell['column']<col+span) or h.get('quality', 'usable')!='usable':
            return None
        text=clean(h['text']).replace('m', 'м')
        if h['cell_id'] in parents:
            if h['row']>=branch['row']:
                return None
            # SAME physical ancestor above the two side branches. Its offset
            # differs by design; the actual ID/text/span remain in the key.
            signature.append(('shared', h['cell_id'], text, span))
        elif h['cell_id']==branch['cell_id']:
            signature.append(('side', cell['column']-col, span))
        elif h['row']<=branch['row']:
            return None
        elif h['cell_id']==unit_header:
            if text and not re.fullmatch(r'(?:м\s*2|2\s*м)',text):
                return None  # Unit proof cannot erase a composite section label.
            continue
        elif text:
            signature.append(('leaf', text, cell['column']-col, span))
        else:
            return None
        # Only the independently verified terminal unit header may omit its
        # corrupt glyph. A single letter or digit can be a real section label.
    return tuple(signature),branch,parents


def _paired_group(table, cells):
    all_cells=table.get('cells', [])
    if len({c['cell_id'] for c in all_cells})!=len(all_cells):
        return None
    headers={c['cell_id']:c for c in all_cells if c.get('role')=='header'}
    rows={c['row'] for c in cells}
    totals=[c['row'] for c in all_cells if c['column']==0 and clean(c['text']).rstrip(':')=='всего']
    if len(rows)!=1 or not totals or rows!={max(totals)}:
        return None
    row=next(iter(rows))
    if any(c.get('row_scope', {}).get('state') not in {None, 'total_without_section'} for c in cells):
        return None
    keys=[_signature(table,c,headers) for c in cells]
    if any(k is None for k in keys):
        return None
    a,b=keys
    if not a or not b or a[0]!=b[0] or a[2]!=b[2] or {_side(a[1]['text']),_side(b[1]['text'])}!={'слева','справа'}:
        return None
    left,right=sorted((a[1],b[1]),key=lambda h:h['column'])
    span=left.get('colspan',1)
    if (left['row']!=right['row'] or span!=right.get('colspan',1)
            or left['column']+span!=right['column']):
        return None
    for key in a[2]:
        parent=headers[key]
        if not (parent['column']<=left['column'] and right['column']+span<=parent['column']+parent.get('colspan',1)):
            return None
    matches=[c for c in all_cells if c['row']==row and c.get('role')=='cell'
        and (k:=_signature(table,c,headers)) is not None and k[0]==a[0]]
    if Counter(c['cell_id'] for c in matches)!=Counter(c['cell_id'] for c in cells):
        return None
    return dict(row=row,columns=[c['column'] for c in cells],cell_ids=[c['cell_id'] for c in cells],
        branch_ids=[a[1]['cell_id'],b[1]['cell_id']],shared_ancestor_ids=a[2])


def _scope_ok(owner_work, work_scope, section_context, text, height_cm):
    # A side, axis, chainage or named subsite needs separate ownership proof.
    owner=owner_work+' '+section_context
    if re.search(r'\b(?:слева|справа|пк\s*\d)|\b(?:лев|прав)\w*\s+(?:ос|сторон|част|полос)|съезд|тротуар|газон|останов',owner,re.I):
        return False
    if not re.search(r'улиц',section_context,re.I) or not re.search(r'покрыт|усилени',owner_work,re.I):
        return False
    if not re.search(r'покрытие',text,re.I) or re.search(r'съезд|тротуар|газон|останов',text,re.I):
        return False
    def heights(value):
        return [measure(m[1],m[2].lower()) for m in re.finditer(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|мм|м|m)\b',value,re.I)]
    own=heights(owner_work)
    if len(own)!=1 or height_cm is not None and measure(height_cm,'см')!=own[0]:
        return False  # This narrow repair has no typed multi-operation group proof.
    expected=own[0]
    found=heights(text)
    if not expected or expected[1]<=0 or not found or any(h!=expected for h in found):
        return False
    # Require an explicit asphalt mix designation supplied by this work's own
    # linked material scope, not merely a generic keyword or matching number.
    def mixes(value):
        return {re.sub(r'\s+','',m[0]).replace('a','а').replace('щма-','щма').replace('щма ','щма')
            for m in re.finditer(r'щма\s*[-–]?\s*\d+|[аa]\d+\s*[а-я]{1,3}\b',clean(value))}
    wanted,actual=mixes(work_scope),mixes(text)
    return bool(len(wanted)==1 and actual==wanted)


def _pair_geometry(tools, ids, records, owner_unit):
    """The same physical proof serves navigation and post-answer repair."""
    entries=[tools.cells[i] for i in ids]
    a,b=[e['cell']['bbox'] for e in entries]
    same_band=(abs(a[0]-b[0])<1e-5 and abs(a[2]-b[2])<1e-5 and (a[3]<=b[1] or b[3]<=a[1])
        or abs(a[1]-b[1])<1e-5 and abs(a[3]-b[3])<1e-5 and (a[2]<=b[0] or b[2]<=a[0]))
    if (len({(e['page'],e['table_id'],e['cell']['row']) for e in entries})!=1
            or not same_band or not _same_physical_row(records[0]['source'],records[1]['source'])):
        return None
    page,table_id=entries[0]['page'],entries[0]['table_id']
    tables=getattr(getattr(tools,'index',None),'tables',None)
    if not tables or not 1<=page<=len(tables):
        return None
    selected=[t for t in tools._tables(page) if t.get('table_id')==table_id]
    if len(selected)!=1:
        return None
    table=selected[0]
    if any(sum(c==e['cell'] for c in table['cells'])!=1 for e in entries):
        return None
    from pdf_cell_ocr import column_unit
    if any(measure('1',column_unit(table,e['cell']))!=measure('1',owner_unit) for e in entries):
        return None
    group=_paired_group(table,[e['cell'] for e in entries])
    return dict(page=page,table_id=table_id,physical_group=group) if group is not None else None


def prepare_quantity_pair(tools, *, owner_work='', work_scope='', owner_unit='', section_context='', height_cm=None):
    """Expose one complete physical pair as an UNRESOLVED navigation candidate.

    This adds only a locally calculated source. The model still has to bind the
    owner section/construction; other tables and individual cells remain intact.
    No declared quantity, answer, item, parameter card or verdict is an input.
    """
    if not getattr(tools,'version',None) or measure('1',owner_unit)!=measure('1','м2'):
        return None
    counts=Counter(s.get('id') for s in tools.sources)
    lookup={s['id']:s for s in tools.sources if counts[s.get('id')]==1}
    eligible={}
    for sid in tools.cells:
        record=_physical(sid,lookup,tools.cells)
        if (record is not None and record['source']['version_id']==tools.version and record['value']>0
                and measure('1',record['unit'])==measure('1',owner_unit)
                and _scope_ok(owner_work,work_scope,section_context,record['header'],height_cm)):
            eligible[sid]=record
    if not 2<=len(eligible)<=32:
        return None  # Never select a convenient subset from an oversized search.
    pairs=[]
    for ids in combinations(eligible,2):
        records=[eligible[sid] for sid in ids]
        geometry=_pair_geometry(tools,ids,records,owner_unit)
        if geometry is not None:
            pairs.append((list(ids),records,geometry))
            if len(pairs)>1:
                return None
    if len(pairs)!=1:
        return None
    ids,records,geometry=pairs[0]
    # Stable physical column order makes repeated preparation idempotent.
    ids.sort(key=lambda sid:tools.cells[sid]['cell']['column'])
    from normative_agent import EvidenceRequest
    result=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit=owner_unit))
    if 'error' in result or number(result.get('value'))!=sum((r['value'] for r in records),Decimal(0)):
        return None
    matches=[c for c in quantity_parameter_candidates(tools.sources,tools.cells,tools.quantity_sums,
        limit=32,max_chars=32000) if c['proposal']['source_id']==result.get('source_id')
        and c['proposal']['operand_source_ids']==ids]
    if len(matches)!=1:
        return None
    candidate=matches[0]
    assert candidate['proposal']['scope_state']=='unresolved' and candidate['scope_verified'] is False
    preferred=getattr(tools,'preferred',None)
    if isinstance(preferred,list):
        for sid in [result['source_id'],*ids]:
            if sid not in preferred:
                preferred.append(sid)
    if not any(e.get('kind')=='prepared_physical_quantity_pair_v1' and e.get('source_id')==result['source_id']
            for e in tools.events):
        tools.events.append(dict(kind='prepared_physical_quantity_pair_v1',source_id=result['source_id'],
            operand_source_ids=ids,version_id=tools.version,scope_verified=False,**geometry,
            provenance=candidate['provenance'],
            scope_requirement='Сопоставьте раздел и конструкцию ВОР с заголовками ПД; полный итог пары не подтверждает принадлежность всей позиции.'))
    return candidate


def repair_quantity_sum(answer, tools, *, owner_work='', work_scope='', owner_unit='', section_context='', height_cm=None):
    """Replace only a uniquely proven full physical pair; do not alter items."""
    proposals=[p for p in answer.parameters if p.parameter=='quantity']
    if len(proposals)!=2 or measure('1',owner_unit)!=measure('1','м2'):
        return False
    if any(p.operation!='direct' or p.scope_state!='verified' or p.conditions
            or p.operand_source_ids not in ([],[p.source_id]) for p in proposals):
        return False
    ids=[p.source_id for p in proposals]
    if len(set(ids))!=2 or not getattr(tools,'version',None):
        return False
    counts=Counter(s.get('id') for s in tools.sources)
    if any(counts[i]!=1 for i in ids):
        return False
    lookup={s['id']:s for s in tools.sources}
    records=[_physical(i,lookup,tools.cells) for i in ids]
    if any(r is None for r in records):
        return False
    records=[r for r in records if r is not None]
    for p,r in zip(proposals,records,strict=True):
        if (r['source']['version_id']!=tools.version or r['value']<=0
                or number(p.value)!=r['value'] or measure('1',p.unit)!=measure('1',owner_unit)
                or literal_parameter_issues(p,r['source'])
                or not _scope_ok(owner_work,work_scope,section_context,r['header'],height_cm)):
            return False
    geometry=_pair_geometry(tools,ids,records,owner_unit)
    if geometry is None:
        return False
    from normative_agent import EvidenceRequest
    result=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit=owner_unit))
    if 'error' in result or number(result.get('value'))!=sum((r['value'] for r in records),Decimal(0)):
        return False
    templates=quantity_parameter_candidates(tools.sources,tools.cells,tools.quantity_sums,limit=32,max_chars=32000)
    matches=[c for c in templates if c['proposal']['source_id']==result.get('source_id')
        and c['proposal']['operand_source_ids']==ids]
    if len(matches)!=1:
        return False
    from workspace_models import NormativeParameterProposal
    proposal=NormativeParameterProposal(**{**matches[0]['proposal'],'scope_state':'verified'})
    source=next(s for s in tools.sources if s['id']==proposal.source_id)
    if literal_parameter_issues(proposal,source):
        return False
    original=[p.model_dump() for p in proposals]
    answer.parameters=[p for p in answer.parameters if p.parameter!='quantity']+[proposal]
    tools.events.append(dict(kind='repaired_physical_quantity_pair_v1',source_id=proposal.source_id,
        operand_source_ids=ids,original_parameters=original,version_id=tools.version,**geometry,
        provenance=matches[0]['provenance'],
        source_sha256=[hashlib.sha256(json.dumps(lookup[i],ensure_ascii=False,sort_keys=True).encode()).hexdigest() for i in ids]))
    return True
