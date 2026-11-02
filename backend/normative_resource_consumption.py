"""Server-owned resource-rate evidence, distinct from dimensionless factors.

Only the literal multiplier is proved. The work quantity, material dependency,
recipe, density and other formula operands keep their independent checks.
"""
import ast
import re


EVENT = 'normative_resource_consumption_candidates'
PROOF = 'resource_consumption_proof'


def resource_quote(resource):
    return (f"{resource['code']} {resource['name']}; {resource['unit']}: "
        f"{resource.get('raw_quantity') or resource.get('quantity') or '[не разобрано]'}")


def record_candidates(item, prefix, trace):
    """Keep typed KB rows in the existing internal trace, never in source DTOs."""
    if trace is None or not item.get('id') or item.get('entity_kind') != 'norm':
        return
    rows=[]
    for resource in item.get('resources',[])[:80]:
        for citation in resource.get('citations',[])[:1]:
            if not citation.get('page') or not citation.get('source_id'):
                continue
            rows.append(dict(resource_code=resource.get('code'), name=resource.get('name'),
                quantity=resource.get('quantity'), quantity_kind=resource.get('quantity_kind'),
                raw_quantity=resource.get('raw_quantity'), unit=resource.get('unit'),
                status=resource.get('status'), quote=resource_quote(resource),
                source_id=f"{prefix}:resources:{citation['page']}",
                pdf_source_id=citation['source_id'], page=citation['page']))
    if rows:
        trace.append(dict(kind=EVENT, norm_id=item['id'], norm_code=item['code'],
            norm_source_id=prefix, norm_measure=item['unit_raw'],
            checks=item.get('checks') or [], rules=item.get('rules') or [], resources=rows))


def _positive_multiplier(expression, value):
    """One occurrence in a multiplication tree; not a divisor or adjustment."""
    from normative_parameters import number
    text=expression.lstrip('=').replace('$','').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval')
    except (SyntaxError,ValueError):
        return False
    leaves=[]
    def visit(node):
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult):
            return visit(node.left) and visit(node.right)
        if isinstance(node,ast.Constant) and not isinstance(node.value,bool):
            amount=number(ast.get_source_segment(text,node))
            if amount is None or amount<=0:
                return False
            leaves.append(amount)
            return True
        if isinstance(node,ast.Name) and re.fullmatch(r'[A-Z]+[1-9]\d*',node.id):
            leaves.append(None)
            return True
        return False
    return visit(tree.body) and len(leaves)>=2 and leaves.count(number(value))==1


def consumption_proof(work, owner, result, value, expression, *, arithmetic_confirmed):
    from normative_parameters import clean,measure,number,quoted,unit_scale_quoted
    from normative_verdict import source_supported
    from vor_material_links import _family
    if (result.get('execution_state')=='technical_failure' or not arithmetic_confirmed or owner.index==work.index
            or str(getattr(work,'position_type','')).casefold()!='материал'
            or str(getattr(owner,'position_type','')).casefold()!='работа'
            or not _positive_multiplier(expression,value)):
        return None
    if re.search(r'купл|к[\s_.-]*упл|коэфф[^\n]{0,35}уплотн',getattr(work,'comment',''),re.I):
        return None
    owner_measure,material_measure=measure('1',owner.unit),measure('1',work.unit)
    if not owner_measure or not material_measure or owner_measure[0]!=material_measure[0]:
        return None
    family=_family(work.work_name)
    if not family:
        return None
    sources={s['id']:s for s in result.get('sources',[])}
    selected={sid for item in result.get('items',[]) if item.get('state')=='confirmed'
        and 'normative:applicability' in item.get('claim_ids',[]) and source_supported(item)
        for sid in item.get('normative_source_ids',[])}
    scopes={(code,sources[sid].get('source_id')) for sid in selected if sid in sources
        for code in re.findall(r'\b\d{2}-\d{2}-\d{3}-\d{2,3}\b',sources[sid].get('label',''))}
    candidates={}
    material_matches=set()
    for event in result.get('evidence_trace',[]):
        if (event.get('kind')!=EVENT or not event.get('norm_id') or event.get('rules')
                or not any(code==event.get('norm_code') for code,_ in scopes)):
            continue
        if any(c.get('state')!='match' for c in event.get('checks',[])):
            continue
        raw=clean(event.get('norm_measure',''))
        match=re.fullmatch(r'(?:(\d+(?:[.,]\d+)?)\s*)?(м[23]?|шт\.?|т|кг)\.?',raw)
        if not match:
            continue
        base_value,base_unit=match[1] or '1',match[2]
        base=measure(base_value,base_unit)
        if not base or base[1]<=0 or base[0]!=owner_measure[0]:
            continue
        # Select by material BEFORE comparing the expected numerical factor.
        for resource in event.get('resources',[]):
            source=sources.get(resource.get('source_id'))
            if (not source or source.get('kind')!='normative'
                    or (event['norm_code'],resource.get('pdf_source_id')) not in scopes
                    or source.get('source_id')!=resource.get('pdf_source_id')
                    or source.get('page')!=resource.get('page')
                    or _family(resource.get('name',''))!=family or not resource.get('resource_code')):
                continue
            # A narrower named grade/size cannot silently stand for another.
            identifiers=re.findall(r'\b[а-яa-z]*\d+[а-яa-z\d/.,-]*',clean(resource['name']))
            material=clean(work.work_name+' '+getattr(work,'comment',''))
            markers=re.findall(r'\b[А-ЯA-Z]{2,}(?:[- ]+[А-ЯA-Z](?![а-яa-z]))?',resource['name'])
            if any(token not in material for token in identifiers) or any(clean(token) not in material for token in markers):
                continue
            material_matches.add((event['norm_id'],resource['resource_code'],resource['pdf_source_id'],resource['page']))
            origin=source.get('origin','')
            if (not origin or origin=='uncertain_table' or
                    (origin.startswith('ocr') and not source.get('numeric_verified'))
                    or str(resource.get('status','')).startswith('ocr')):
                continue
            quantity=number(resource.get('quantity'))
            if (resource.get('quantity_kind')!='numeric' or quantity is None or quantity<=0
                    or number(resource.get('raw_quantity'))!=quantity):
                continue
            amount=measure(resource['quantity'],resource.get('unit',''))
            measure_quote='Измеритель: '+event['norm_measure']
            header='Ресурсы нормы '+event['norm_code']+'; '+measure_quote
            if (not amount or amount[0]!=material_measure[0]
                    or clean(header) not in clean(source['text'])
                    or not unit_scale_quoted(base_value,base_unit,measure_quote,source['text'])
                    or not quoted(resource['quantity'],resource['quote'],source['text'])):
                continue
            rate=amount[1]/base[1]*owner_measure[1]/material_measure[1]
            key=(event['norm_id'],resource['resource_code'],resource['pdf_source_id'],resource['page'],
                 resource['quantity'],resource['unit'],event['norm_measure'])
            candidates[key]=dict(resource=resource,event=event,rate=rate,
                base_value=base_value,base_unit=base_unit,measure_quote=measure_quote)
    # Ambiguous materials or norms must not be selected by matching the VOR rate.
    if len(candidates)!=1 or len(material_matches)!=1:
        return None
    selected_rate=next(iter(candidates.values()))
    if selected_rate['rate']!=number(value):
        return None
    resource,event=selected_rate['resource'],selected_rate['event']
    base_value,base_unit=selected_rate['base_value'],selected_rate['base_unit']
    quote=selected_rate['measure_quote']+'\n'+resource['quote']
    reason=(f"Расход ресурса {resource['resource_code']} «{resource['name']}» по норме {event['norm_code']}: "
        f"{resource['quantity']} {resource['unit']} на {base_value} {base_unit} работы. "
        'Подтверждён только множитель; исходное количество работы и остальные параметры проверяются отдельно.')
    proof=dict(kind=PROOF,position_index=work.index,parent_index=owner.index,
        norm_id=event['norm_id'],norm_code=event['norm_code'],resource_code=resource['resource_code'],
        source_id=resource['source_id'],quantity=resource['quantity'],unit=resource['unit'],
        base_quantity=base_value,base_unit=base_unit,multiplier=str(selected_rate['rate']))
    return dict(card=dict(role='coefficient',label='Нормативный расход',state='confirmed',reason=reason,
        found_value=str(selected_rate['rate']),found_unit=f'{work.unit}/{owner.unit}',
        source_ids=[resource['source_id']],quote=quote,
        source_anchors=[dict(source_id=resource['source_id'],quote=resource['quote'],rects=[]),
                        dict(source_id=resource['source_id'],quote=selected_rate['measure_quote'],rects=[])]),proof=proof)
