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


def consumption_proof(work, owner, result, value, expression, *, arithmetic_confirmed, areal_recipe=None, project_scopes=(), scope_gaps=None, scope_gap_source_ids=None):
    from normative_parameters import clean,measure,number,quoted,unit_scale_quoted
    from normative_verdict import source_supported
    from vor_material_links import _family
    if (result.get('execution_state')=='technical_failure' or not arithmetic_confirmed or (owner.index==work.index and not areal_recipe)
            or str(getattr(work,'position_type','')).casefold()!='материал'
            or (not areal_recipe and str(getattr(owner,'position_type','')).casefold()!='работа')
            or (not areal_recipe and not _positive_multiplier(expression,value))):
        return None
    if re.search(r'купл|к[\s_.-]*упл|коэфф[^\n]{0,35}уплотн',getattr(work,'comment',''),re.I):
        return None
    owner_measure,material_measure=measure('1','м2' if areal_recipe else owner.unit),measure('1',work.unit)
    if not owner_measure or not material_measure or (not areal_recipe and owner_measure[0]!=material_measure[0]):
        return None
    family=_family(work.work_name)
    if not family and not areal_recipe:
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
        raw=clean(event.get('norm_measure',''))
        match=re.fullmatch(r'(?:(\d+(?:[.,]\d+)?)\s*)?(м[23]?|га|шт\.?|т|кг)\.?',raw)
        if not match:
            continue
        base_value,base_unit=match[1] or '1',match[2]
        if base_unit=='га' and not areal_recipe:
            continue
        base=measure(base_value,base_unit)
        if not base or base[1]<=0 or base[0]!=owner_measure[0]:
            continue
        if any(c.get('state')!='match' and not (areal_recipe
                and c.get('label')=='Единица ВОР' and c.get('state')=='mismatch'
                and measure('1',c.get('actual',''))==material_measure
                and clean(c.get('expected',''))==raw)
                for c in event.get('checks',[])):
            continue
        # Select by material BEFORE comparing the expected numerical factor.
        for resource in event.get('resources',[]):
            source=sources.get(resource.get('source_id'))
            if (not source or source.get('kind')!='normative'
                    or (event['norm_code'],resource.get('pdf_source_id')) not in scopes
                    or source.get('source_id')!=resource.get('pdf_source_id')
                    or source.get('page')!=resource.get('page')
                    or not _material_matches(work.work_name,resource.get('name',''),exact_fallback=bool(areal_recipe))
                    or not resource.get('resource_code')):
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
            if areal_recipe:
                # Both the selected measuring header and complete typed row must
                # survive in the current source, not just its numeric token.
                if clean(resource_quote({**resource,'code':resource['resource_code']}))!=clean(resource.get('quote','')) or clean(resource['quote']) not in clean(source['text']):
                    continue
                norm_source=sources.get(event.get('norm_source_id'))
                if (not norm_source or norm_source.get('kind')!='normative'
                        or not norm_source.get('origin') or norm_source.get('origin')=='uncertain_table'
                        or (norm_source['origin'].startswith('ocr') and not norm_source.get('numeric_verified'))
                        or norm_source.get('source_id')!=resource['pdf_source_id']
                        or clean(measure_quote) not in clean(norm_source.get('text',''))):
                    continue
                from normative_thickness_rules import source_definition,definition_text,CODE
                definition=source_definition(norm_source)
                if (not definition or definition['code']!=event['norm_code']
                        or event.get('norm_source_id') not in selected):
                    continue
                selected_definitions=[d for sid in selected if sid in sources
                    and (d:=source_definition(sources[sid])) and d['code']==event['norm_code']
                    and d['source_id']==resource['pdf_source_id']]
                if len({clean(d['text']) for d in selected_definitions})!=1:
                    continue
                head=definition_text(definition['text'])
                titles=re.findall(r'^\s*'+re.escape(event['norm_code'])+r'\s+([^\n]+)',head,re.M)
                table_titles=re.findall(r'^\s*Таблица\s+(?:(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\s+)?'
                    +re.escape(definition['table_code'])+r'\s+([^\n]+)',head,re.M|re.I)
                composition=definition['text'].split('Состав работ:')
                if (len(composition)!=2 or not re.search(r'[а-яёa-z]{3,}',composition[1],re.I)
                        or set(re.findall(CODE,head))!={event['norm_code']} or len(titles)!=1 or len(table_titles)!=1
                        or not re.search(r'[а-яёa-z]{3,}',titles[0],re.I)
                        or re.match(r'(?:измерител|ресурс|состав работ)',clean(titles[0]))):
                    continue
                from normative_lawn_soil_scope import areal_resource_scope_gap
                # Conjunctive table/selected-line conditions. Never union
                # unrelated lawn categories into a broader allowed menu.
                gap=areal_resource_scope_gap(titles[0],project_scopes) or areal_resource_scope_gap(table_titles[0],project_scopes)
                if gap:
                    if scope_gaps is not None and gap not in scope_gaps:
                        scope_gaps.append(gap)
                    if scope_gap_source_ids is not None:
                        for sid in (norm_source['id'],source['id']):
                            if sid not in scope_gap_source_ids:
                                scope_gap_source_ids.append(sid)
                    continue
                numerator=measure('1',areal_recipe['rate_unit'].split('/')[0])
                per_area=number(areal_recipe['rate_denominator_area'])
                if not numerator or numerator[0]!=amount[0] or per_area is None:
                    continue
                rate=amount[1]/base[1]*per_area/numerator[1]
            else:
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
        found_value=str(selected_rate['rate']),found_unit=areal_recipe['rate_unit'] if areal_recipe else f'{work.unit}/{owner.unit}',
        source_ids=[resource['source_id']],quote=quote,
        source_anchors=[dict(source_id=resource['source_id'],quote=resource['quote'],rects=[]),
                        dict(source_id=resource['source_id'],quote=selected_rate['measure_quote'],rects=[])]),proof=proof)


def _material_matches(declared, resource, *, exact_fallback=False):
    from normative_parameters import clean
    from vor_material_links import _family
    family=_family(declared)
    if family:
        return _family(resource)==family
    # An unnamed broad family cannot make grass, flower and grain seeds
    # interchangeable. Only an otherwise exact declared name is supported.
    name=re.sub(r'\s*\(смесь\)\s*$', '', clean(declared)).strip()
    return bool(exact_fallback and name and name==clean(resource))


def areal_consumption_proof(position,result,*,area_card,arithmetic_card,scope_gaps=None,scope_gap_source_ids=None):
    """Bind the existing bounded area recipe to one selected literal KB row.

    Cards passed by callers are only comparison targets: the area is rebuilt
    from current project proposals. No semantic applicability is inferred.
    """
    from analysis_replay import digest
    from normative_parameters import build_cards,clean,measure,quoted
    from normative_parent_basis import _anchored_card
    from normative_workbook_rate import analyze
    recipe=analyze(position)
    if (not recipe or recipe['density_required'] or arithmetic_card.get('state')!='confirmed'
            or area_card.get('state')!='confirmed'):
        return None
    literal={**result,'local_work_basis':None,'quantity_composition':None,'parameter_cards':[]}
    raw_cards=build_cards(position,literal,{position.index:position},{})
    rebuilt=[c for c in raw_cards if c['id']=='input:area']
    arithmetic=[c for c in raw_cards if c['id']==f'calculation:{position.index}']
    sources={s['id']:s for s in result.get('sources',[])}
    if (len(arithmetic)!=1 or arithmetic[0]['state']!='confirmed'
            or len(rebuilt)!=1 or rebuilt[0]['state']!='confirmed'
            or measure(rebuilt[0].get('found_value'),rebuilt[0].get('found_unit',''))!=measure(recipe['area'],'м2')
            or not _anchored_card(rebuilt[0],sources)):
        return None
    project_proposals=[p for p in result.get('parameter_proposals',[])
        if p.get('parameter')=='quantity' and p.get('source_id') in rebuilt[0]['source_ids']
        and p.get('scope_state')=='verified'
        and measure(p.get('value'),p.get('unit',''))==measure(rebuilt[0]['found_value'],rebuilt[0]['found_unit'])
        and clean(p.get('quote',''))==clean(rebuilt[0]['quote'])
        and clean(p.get('scope_quote','')) in clean(sources[p['source_id']]['text'])
        and quoted(p.get('value'),p.get('quote',''),p.get('scope_quote',''))]
    selected=consumption_proof(position,position,result,recipe['rate'],recipe['expression'],
        arithmetic_confirmed=True,areal_recipe=recipe,scope_gaps=scope_gaps,scope_gap_source_ids=scope_gap_source_ids,
        project_scopes=[p['scope_quote'] for p in project_proposals])
    if not selected:
        return None
    from normative_parent_basis import _proposal_dependencies
    dependencies=set()
    for proposal in project_proposals:
        ids=_proposal_dependencies(proposal,sources)
        if not ids:
            return None
        dependencies.update(ids)
    # The selected typed event's base header must remain independently replayable.
    norm_sources={e.get('norm_source_id') for e in result.get('evidence_trace',[])
        if e.get('kind')==EVENT and e.get('norm_id')==selected['proof']['norm_id']
        and any(r.get('source_id')==selected['proof']['source_id'] for r in e.get('resources',[]))}
    dependencies.update(sid for sid in norm_sources if sid in sources)
    dependencies.add(selected['proof']['source_id'])
    selected['proof'].update(quantity_role='calculated_areal_output',expression=recipe['expression'],
        area_card=rebuilt[0],recipe=recipe,root_source_ids=sorted(dependencies),
        source_sha256={sid:digest(sources[sid]) for sid in sorted(dependencies)},
        proposals_sha256=digest(project_proposals),
        normative_applicability_inferred=False,project_literal_output=False)
    return selected
