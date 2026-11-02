"""Ready literal quantity proposals; engineering scope is always unresolved.

Inputs are the live read_table / sum_cells registry, never a declared VOR
quantity. Registry geometry and literal source text must agree independently.
"""
import hashlib
import json
import math
from decimal import Decimal

from normative_parameters import measure, number, quoted, unit_quoted


def sum_supersedes_operand(proposal, total, sources):
    """A verified whole replaces its own literal part, never another quantity."""
    source = sources.get(proposal.get('source_id')) or {}
    return bool(total.get('operation') == 'sum' and total.get('scope_state') == 'verified'
        and proposal.get('parameter') == 'quantity' and proposal.get('operation', 'direct') == 'direct'
        and not proposal.get('conditions') and proposal.get('source_id') in total.get('operand_source_ids', [])
        and source.get('kind') == 'project' and source.get('unit_verified')
        and (source.get('origin') == 'native' or source.get('numeric_verified'))
        and number(proposal.get('value')) == number(source.get('text', '').rsplit('\n', 1)[-1])
        and measure('1', proposal.get('unit', '')) == measure('1', total.get('unit', '')))


def _physical(source_id, sources, cells):
    source=sources.get(source_id)
    entry=cells.get(source_id)
    if not source or not entry or ':cell:' not in source_id:
        return None
    cell=entry.get('cell') or {}
    unit=entry.get('unit','')
    native=source.get('origin')==cell.get('origin')=='native'
    verified_ocr=(source.get('origin')==cell.get('origin')=='ocr'
        and source.get('numeric_verified') is True and cell.get('numeric_verified') is True)
    bbox=cell.get('bbox')
    raw=cell.get('text','')
    value=number(raw)
    if (source.get('kind')!='project' or not source.get('version_id')
            or entry.get('version_id')!=source['version_id'] or not entry.get('table_id')
            or not isinstance(source.get('page'),int) or source['page']<1
            or entry.get('page')!=source['page'] or cell.get('quality','usable')!='usable'
            or cell.get('role')!='cell' or not (native or verified_ocr)
            or source.get('unit_verified') is not True or not unit or source.get('unit')!=unit
            or measure('1',unit) is None or value is None or not cell.get('cell_id')
            or not isinstance(bbox,(list,tuple)) or len(bbox)!=4
            or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in bbox)
            or not (0<=bbox[0]<bbox[2] and 0<=bbox[1]<bbox[3])):
        return None
    namespace=hashlib.sha256(f"{source['version_id']}:{source['page']}:{entry['table_id']}:{cell['cell_id']}".encode()).hexdigest()[:24]
    if source_id!=f"agent:p{source['page']}:cell:{namespace}":
        return None
    text=source.get('text','')
    header=entry.get('header_text','')
    unit_line='Единица физического столбца: '+unit
    if (entry.get('headers')!=text or not header or not text.startswith(header+'\n')
            or not header.endswith(unit_line) or not text.endswith('\n'+raw)):
        return None
    return dict(source=source,cell=cell,header=header,value=value,unit=unit,
        quote=text[len(header)-len(unit_line):].strip(),scope_quote=text.strip(),
        provenance=dict(source_id=source_id,version_id=source['version_id'],page=source['page'],
            table_id=entry['table_id'],cell_id=cell['cell_id'],row=cell.get('row'),column=cell.get('column'),bbox=list(bbox),
            origin=source['origin'],value=str(value),unit=unit))


def _proposal(source, value, unit, quote, scope, *, operands=()):
    value=str(value)
    if (len(value)>40 or len(unit)>30 or len(source['id'])>80 or not quote or not scope
            or max(len(quote),len(scope))>900 or not unit_quoted(unit,quote)
            or not quoted(value,quote,source['text']) or not quoted(value,quote,scope)
            or scope not in source['text']):
        return None
    return dict(parameter='quantity',operation='sum' if operands else 'direct',
        operand_source_ids=list(operands),source_id=source['id'],value=value,unit=unit,
        quote=quote,scope_quote=scope,scope_state='unresolved')


def quantity_parameter_candidates(sources, cells, sums=(), *, limit=12, max_chars=12000):
    """Return whole bounded templates and their provenance, without scope approval."""
    lookup={}
    duplicate=set()
    for source in sources:
        key=source.get('id')
        if not key or key in lookup or key in duplicate:
            duplicate.add(key)
            lookup.pop(key,None)
        else:
            lookup[key]=source
    physical={key:value for key in cells if (value:=_physical(key,lookup,cells)) is not None}
    candidates=[]
    seen=set()
    for record in reversed(list(sums)):
        ids=record.get('operand_source_ids',[])
        source=lookup.get(record.get('source_id'))
        unit=record.get('unit','')
        if (not source or source.get('origin')!='calculated_from_cells' or source.get('kind')!='project'
                or source.get('numeric_verified') is not True or source.get('unit_verified') is not True
                or source.get('unit')!=unit or not 2<=len(ids)<=12 or len(set(ids))!=len(ids)
                or any(i not in physical for i in ids)):
            continue
        operands=[physical[i] for i in ids]
        if len({tuple(o['cell']['bbox']) for o in operands})!=len(operands):
            continue  # Different table IDs cannot count the same physical cell twice.
        if (any(o['source'].get('version_id')!=source.get('version_id')
                or o['source']['page']!=source.get('page') or measure('1',o['unit'])!=measure('1',unit)
                for o in operands)):
            continue
        total=sum((o['value'] for o in operands),Decimal(0))
        if number(record.get('value'))!=total:
            continue
        context='\n'.join(dict.fromkeys(o['header'] for o in operands))
        equation=' + '.join(str(o['value']) for o in operands)+f' = {total} {unit}'
        expected=context+'\n'+equation+'. Ячейки: '+', '.join(ids)
        identity=hashlib.sha256(json.dumps([ids,unit],ensure_ascii=False).encode()).hexdigest()[:24]
        if source['id']!='agent:sum:'+identity or source.get('text')!=expected:
            continue
        proposal=_proposal(source,total,unit,equation,context+'\n'+equation,operands=ids)
        if proposal:
            candidates.append(dict(proposal=proposal,scope_verified=False,
                provenance=dict(kind='calculated_from_physical_cells',version_id=source['version_id'],
                    page=source['page'],operands=[o['provenance'] for o in operands])))
    for source in reversed(sources):
        entry=physical.get(source.get('id'))
        if entry is None:
            continue
        proposal=_proposal(source,entry['value'],entry['unit'],entry['quote'],entry['scope_quote'])
        if proposal:
            candidates.append(dict(proposal=proposal,scope_verified=False,
                provenance=dict(kind='physical_cell',**entry['provenance'])))
    output=[]
    used=0
    for candidate in candidates:
        key=candidate['proposal']['source_id']
        size=len(json.dumps(candidate,ensure_ascii=False))
        if key in seen or len(output)>=limit or used+size>max_chars:
            continue
        seen.add(key)
        output.append(candidate)
        used+=size
    return output
