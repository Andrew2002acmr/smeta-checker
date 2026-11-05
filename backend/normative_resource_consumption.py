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


def appendix_recipe(position, recipe):
    """An explicit appendix declaration cannot use the direct-resource shortcut."""
    return bool(recipe and recipe.get('rate_unit')=='т/1000м2'
        and str(getattr(position,'position_type','')).casefold()=='материал'
        and _appendix_type(getattr(position,'work_name','')) is not None
        and re.search(r'прил(?:ожени[ея])?\.?\s*27\.1\b',
        getattr(position,'comment',''),re.I))


def _appendix_type(text):
    from normative_parameters import clean
    values={('a' if m[1]=='а' else m[1],int(m[2])) for m in re.finditer(
        r'(?<![\w-])(щма|sma|sp|а|a)[ -]?(\d+)(?:[а-яa-z]{0,3})(?![\w-])',clean(text))}
    return next(iter(values)) if len(values)==1 else None


def _appendix_band(comment):
    """Keep the declared operators; a shorthand interval is only a candidate."""
    from normative_parameters import clean
    text=clean(comment).replace('³','3')
    matches=list(re.finditer(r'(?:γ|ү|y)\s*[-=:]?\s*([^();]{1,65}?)\s*т\s*/\s*[мm]3',text))
    if len(matches)!=1:
        return None
    literal=matches[0][1].strip()
    value=literal.replace(',','.').replace(' ','')
    if re.fullmatch(r'(?:до|неболее|≤|<=)2\.7',value):
        return 0,literal,False
    if re.fullmatch(r'(?:св\.?|свыше|более|>)2\.7(?:до|≤|<=)2\.9',value):
        return 1,literal,False
    if re.fullmatch(r'2\.7[-–—]2\.9',value):
        return 1,literal,True
    if re.fullmatch(r'(?:св\.?|свыше|более|>)2\.9',value):
        return 2,literal,False
    return None


def _appendix_source(sources, label):
    """Reuse the registered, visually checked literal, not editable status flags."""
    import hashlib
    from normative_collection_context import ROAD_ORDER_ID,ROAD_ORDER_SHA,ROAD_PAIR
    _,clause,page,text_sha=next(row for row in ROAD_PAIR if row[0]==label)
    found=[s for s in sources.values() if s.get('kind')=='normative'
        and s.get('source_id')==ROAD_ORDER_ID and s.get('source_sha256')==ROAD_ORDER_SHA
        and s.get('clause_id')==clause and s.get('page')==page
        and s.get('origin')=='native_visually_verified'
        and hashlib.sha256(s.get('text','').encode()).hexdigest()==text_sha]
    # Duplicate aliases of a literal are harmless; an arbitrary competing card is not selected.
    return min(found,key=lambda s:s['id']) if found else None


def appendix_rate_candidate(position,result,*,gaps=None):
    """Recompute a conditional table expression; never certify its applicability.

    Edition, scoped project-data absence and the edge prescription do not have
    replayable reviewed premises yet. A model C or metadata flag cannot supply
    them. This candidate feeds the existing rate ledger and remaining work plan.
    """
    from decimal import Decimal
    from analysis_replay import digest
    from normative_parameters import clean,measure,number
    from normative_parent_basis import verified_geometry_dependency,DEPENDENCY_TRACE
    from normative_thickness_rules import source_definition,definition_text,literal_measure,base_thickness
    from normative_workbook_arithmetic import arithmetic_claims
    from normative_workbook_rate import analyze,_declared_equation
    from normative_verdict import source_supported
    recipe=analyze(position)
    if not appendix_recipe(position,recipe):
        return None
    pending=[
        'Нужна проверенная привязка редакции приложения 27.1 и полной цепочки изменений.',
        'Нужна полнота проверки проектных данных о расходе: проектный расход имеет приоритет перед приложением.',
        'Нужна проверка проектного требования об уплотнении внешних кромок прижимным вальцом (1,004).',
    ]
    pending_ids=['edition_chain','project_consumption_priority','edge_prescription']
    def stop(reason):
        if gaps is not None:
            gaps.extend([reason,*pending])
        return None
    if (str(getattr(position,'position_type','')).casefold()!='материал'
            or not recipe or recipe['rate_unit']!='т/1000м2'
            or arithmetic_claims(result)!={'workbook:formula'}):
        return stop('Не подтверждены размерность и арифметика расчёта по приложению.')
    sources={s['id']:s for s in result.get('sources',[])}
    if len(sources)!=len(result.get('sources',[])):
        return stop('Неоднозначны идентификаторы источников приложения.')
    table=_appendix_source(sources,'27.1')
    kind,band=_appendix_type(position.work_name),_appendix_band(position.comment)
    if not table or kind is None or band is None:
        return stop('Нужны полное проверенное приложение, один тип смеси и однозначные операторы диапазона Γ.')
    # This is a maximum STONE-material density group, never mixture bulk density.
    if re.search(r'(?:объемн\w*|средн\w*)\s+плотност|плотност\w*\s+(?:асфальтобетон\w*\s+)?смеси',clean(position.comment)):
        return stop('Заявленная плотность смеси не является максимальной плотностью каменных материалов.')
    selected={sid for item in result.get('items',[]) if item.get('state')=='confirmed'
        and 'normative:applicability' in item.get('claim_ids',[]) and source_supported(item)
        for sid in item.get('normative_source_ids',[])}
    bases=[]
    for sid in sorted(selected):
        s=sources.get(sid)
        d=source_definition(s) if s else None
        h=base_thickness(d['text']) if d else None
        if not d or not h:
            continue
        head=definition_text(d['text'])
        if (set(re.findall(r'\d{2}-\d{2}-\d{3}-\d{2,3}',head))!={d['code']}
                or not re.search(r'^\s*'+re.escape(d['code'])+r'\s+[^\n]+',head,re.M)
                or len(d['text'].split('Состав работ:'))!=2
                or not d['text'].split('Состав работ:')[1].strip()
                or literal_measure(s,d['code'])!=measure('1000','м2')):
            return stop('Выбранная рабочая норма не имеет полного определения с измерителем 1000 м².')
        bases.append((s,d,h))
    unique={(d['source_id'],d['code'],clean(d['text'])) for _,d,_ in bases}
    if len(unique)!=1:
        return stop('Не установлена единственная полная выбранная рабочая норма.')
    norm,definition,base_h=bases[0]
    if base_h['value']!=Decimal('.04'):
        return stop('Базовая толщина выбранной рабочей нормы не равна 4 см приложения.')
    if not verified_geometry_dependency(result,position):
        return stop('Нужна повторно проверяемая связь материала с площадью и толщиной того же родителя.')
    dependency=next(e for e in result['evidence_trace'] if e.get('kind')==DEPENDENCY_TRACE)
    cards={c['id']:c for c in result.get('parameter_cards',[])}
    height=measure(cards['thickness']['found_value'],cards['thickness']['found_unit'])
    project_ids=set(cards['thickness'].get('source_ids',[]))
    scopes=[p.get('scope_quote','') for p in result.get('parameter_proposals',[])
        if p.get('parameter')=='thickness' and p.get('source_id') in project_ids
        and p.get('scope_state')=='verified' and p.get('scope_relation','same_layer')=='same_layer']
    if not height or height[0]!='length' or not scopes or any(_appendix_type(scope)!=kind for scope in scopes):
        return stop('Тип смеси и проектная толщина не связаны с тем же физическим слоем.')
    # The exact reviewed text fixes the full header and all row/column positions.
    rows=[]
    pattern=r'(?m)^\s*(А-5, А-8|А-11, А-16|А-22, А-32|ЩМА-8, ЩМА-11, ЩМА-16, ЩМА-\s*(?:22)?|SP-4, SP-8|SP-11, SP-16|SP-22, SP-32|SMA-8, SMA-11, SMA-16, SMA-22)\s+т\s+((?:\d+(?:[.,]\d+)?\s+){5}\d+(?:[.,]\d+)?)'
    for match in re.finditer(pattern,table['text']):
        tokens={('a' if m[1]=='А' else m[1].casefold(),int(m[2]))
            for m in re.finditer(r'(ЩМА|SMA|SP|А)-(\d+)',re.sub(r'ЩМА-\s*22','ЩМА-22',match[1]))}
        if match[1].rstrip().endswith('ЩМА-') and re.match(r'\s*22\b',table['text'][match.end():]):
            tokens.add(('щма',22))
        if kind in tokens:
            rows.append(match)
    if len(rows)!=1:
        return stop('Тип смеси не связан с единственной полной строкой приложения.')
    row=rows[0]
    row_quote=row[0]
    if row[1].rstrip().endswith('ЩМА-'):
        continuation=re.match(r'\s*22\b',table['text'][row.end():])
        if continuation:
            row_quote+=continuation[0]
    values=[Decimal(v.replace(',','.')) for v in row[2].split()]
    base,step=values[band[0]*2:band[0]*2+2]
    signed=(height[1]-base_h['value'])/Decimal('.005')
    if signed!=signed.to_integral_value() or abs(signed)>1000:
        return stop('Проектная толщина не выражается целыми шагами ±0,5 см от 4 см.')
    equation=recipe.get('declared_equation') or {}
    expression=equation.get('expression','').replace(',','.').strip()
    try:
        tree=ast.parse(expression,mode='eval').body
        if not isinstance(tree,ast.BinOp) or not isinstance(tree.op,(ast.Add,ast.Sub)):
            raise ValueError
        product=tree.right
        if not isinstance(product,ast.BinOp) or not isinstance(product.op,ast.Mult):
            raise ValueError
        operands=[number(ast.get_source_segment(expression,n)) if isinstance(n,ast.Constant) else None
            for n in (tree.left,product.left,product.right)]
        if operands!=[base,step,abs(signed)] or (signed<0)!=(isinstance(tree.op,ast.Sub)):
            raise ValueError
    except (SyntaxError,ValueError,TypeError):
        return stop('Операнды или знак объявленной формулы не совпадают с выбранными ячейками и проектной толщиной.')
    amount=base+step*signed
    if amount<=0 or _declared_equation(expression)!=amount or number(recipe['rate'])!=amount:
        return stop('Объявленный расход не совпадает с исходными ячейками и шагами приложения.')
    clause=_appendix_source(sources,'1.27.35')
    if not clause:
        pending.append('Не доставлена полная проверенная новая 1.27.35 с условием приоритета проектных данных.')
        pending_ids.append('fallback_clause_source')
    if definition['table_code'] not in {'27-06-029','27-06-030','27-06-031','27-06-032'} or definition['code']=='27-06-030-02':
        pending.append('Выбранная рабочая норма не входит в область fallback новой 1.27.35.')
        pending_ids.append('work_norm_scope')
    if band[2]:
        pending.append('Интервал Γ в ВОР записан сокращённо: требуется привязка к строгому нижнему и включённому верхнему пределу колонки.')
        pending_ids.append('density_interval_operators')
    area=cards['quantity']
    if measure(area['found_value'],area['found_unit'])!=measure(recipe['area'],'м2'):
        pending.append('Площадь ПД и исходное число формулы различаются; нужна отдельная проверка точности записи операнда.')
        pending_ids.append('area_precision')
    dependencies=set(dependency['proof']['source_sha256'])|{norm['id'],table['id']}|{sid for sid in selected if sid in sources}
    if clause:
        dependencies.add(clause['id'])
    if gaps is not None:
        gaps.extend(pending)
    from copy import deepcopy
    return dict(kind='appendix_derived_rate_candidate_v1',rate=format(amount,'f'),unit=recipe['rate_unit'],
        expression=expression,base=format(base,'f'),step=format(step,'f'),signed_steps=int(signed),
        base_thickness_cm='4',step_cm='0.5',project_thickness=deepcopy(cards['thickness']),
        selected_type=dict(family=kind[0],size=kind[1]),density_kind='maximum_stone_material_density',
        density_band=dict(index=band[0],literal=band[1],shorthand=band[2],
            bounds=[dict(upper='2.7',upper_operator='le'),dict(lower='2.7',lower_operator='gt',upper='2.9',upper_operator='le'),dict(lower='2.9',lower_operator='gt')][band[0]]),
        table_source_id=table['id'],row_quote=row_quote,norm_source_id=norm['id'],norm_code=definition['code'],
        area_operand=dict(kind='formal_parent',card=deepcopy(area),parent_index=dependency['proof']['parent']['index'],
            dependency_sha256=dependency['sha256'],declared_area=recipe['area']),
        root_source_ids=sorted(dependencies),source_sha256={sid:digest(sources[sid]) for sid in sorted(dependencies)},
        proposals_sha256=digest(result.get('parameter_proposals',[])),pending_conditions=pending,
        pending_condition_ids=pending_ids,
        selected_applicability_sha256=digest([i for i in result.get('items',[]) if i.get('state')=='confirmed'
            and 'normative:applicability' in i.get('claim_ids',[]) and source_supported(i)]),
        conditional_arithmetic_verified=True,normative_basis_verified=False,dependent_output_verified=False,
        actual_material_density_verified=False,normative_applicability_inferred=False)


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
