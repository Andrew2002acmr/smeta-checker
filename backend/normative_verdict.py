"""Derive the published verdict after normative validation; raw matching is immutable."""
from copy import deepcopy
import re

PENDING = 'Проверяется по нормативам'
FIX = 'Обнаружено расхождение'
REVIEW = 'Требуется ручная проверка'


def source_supported(item):
    """Source routing for already validated items, not semantic proof."""
    claims = item.get('claim_ids') or []
    if 'workbook:formula' in claims:
        return False  # Arithmetic requires independent server calculation, not a normative citation.
    from normative_workbook_rate import project_rate_claim
    project_only = bool(claims) and all(c in {'quantity','unit','presence','input:area','derived:parent'} or project_rate_claim(c)
        or c.startswith('geometry:') or project_designation_claim(c) for c in claims)
    return bool(claims and item.get('project_source_ids') and (project_only or item.get('normative_source_ids')))


def project_designation_claim(claim):
    return bool(re.fullmatch(r'attribute:(?:name|comment):designation:\d+',claim))


def literal_project_designations(item, sources, *, position=None, definitions=()):
    """An exact PD designation needs no second normative definition.

    This only certifies a positive literal comparison, never compatibility of
    different marks, a normative condition, or a designation absent from PD.
    The caller retains source-kind and construction-scope checks.
    """
    claims=item.get('claim_ids') or []
    direct=[c for c in claims if project_designation_claim(c)]
    if not direct or item.get('normative_source_ids'):
        return True
    if item.get('state')!='confirmed':
        return False
    expected={d['claim_id']:d for d in definitions}
    if position is not None:
        from audit_core import build_claim_specs
        expected.update({s.claim_id:dict(predicate=s.predicate,expected_text=s.expected_text)
            for s in build_claim_specs(position)})
    from audit_predicates import _designation_text
    lookup={s['id']:s for s in sources}
    for claim in direct:
        spec=expected.get(claim,{})
        value=_designation_text(spec.get('expected_text',''))
        if spec.get('predicate')!='designation' or not value:
            return False
        pattern=r'(?<!\w)'+re.escape(value)+r'(?!\w)'
        if not any(s.get('kind')=='project' and s.get('origin')=='native'
                and re.search(pattern,_designation_text(s.get('text','')))
                for sid in item.get('project_source_ids',[]) if (s:=lookup.get(sid))):
            return False
    return True


def absence_claim(item):
    """A negative search result is not a positive incompatibility proof."""
    return bool(item.get('state')=='possible_mismatch' and re.search(
        r'не\s+(?:найден|обнаружен|указан|подтвержд)|нет\s+подтвержден|отсутств\w*[^.!?]{0,90}(?:ПД|проект|документ)'
        r'|подтверждени[ея][^.!?]{0,110}(?:ПД|проект)[^.!?]{0,20}\sнет\b',
        item.get('explanation',''),re.I))


def readable_review(review):
    """Ephemeral model handles belong in traces, not the engineer's prose."""
    result=deepcopy(review)
    # Saved raw reviews also pass here before publication. A crop's source ID
    # proves acquisition only, not a semantic or numerical interpretation.
    from normative_source_roles import unread_visual_source, UNREAD_VISUAL_MESSAGE
    unread = {s['id'] for s in result.get('sources', []) if unread_visual_source(s)}
    visual_downgraded = False
    for item in result.get('items', []):
        if (item.get('state') in {'confirmed', 'possible_mismatch'}
                and unread.intersection(item.get('project_source_ids', []) + item.get('normative_source_ids', []))):
            result.setdefault('evidence_trace', []).append(dict(kind='unread_visual_source_downgrade_v1',
                original_item=deepcopy(item)))
            item['state'] = 'needs_facts'
            if UNREAD_VISUAL_MESSAGE not in item.get('explanation', ''):
                item['explanation'] = item.get('explanation', '') + ' ' + UNREAD_VISUAL_MESSAGE
            visual_downgraded = True
    for card in result.get('parameter_cards', []):
        if card.get('state') in {'confirmed', 'possible_mismatch'} and unread.intersection(card.get('source_ids', [])):
            result.setdefault('evidence_trace', []).append(dict(kind='unread_visual_card_downgrade_v1',
                original_card=deepcopy(card)))
            card.update(state='needs_facts', reason=UNREAD_VISUAL_MESSAGE)
            visual_downgraded = True
    if visual_downgraded:
        result['state'] = ('possible_mismatch' if any(i.get('state') == 'possible_mismatch'
            for i in result.get('items', [])) else 'needs_facts')
        result['requires_engineer_review'] = True
    labels={f's{i}':('ПД' if s['kind']=='project' and s.get('origin')!='explicit_expert_method_basis' else s.get('label','Норматив'))+
        (f", стр. {s['page']}" if s.get('page') else '') for i,s in enumerate(result.get('sources',[]))}
    def readable(text):
        return re.sub(r'(?<!\w)s\d+(?!\w)',lambda m: labels.get(m[0],m[0]),text)
    result['summary']=readable(result.get('summary',''))
    # Old workers may still finish pinned, paid tasks with strict-equality
    # cards. Correct only this provable float noise in the read-only view.
    from normative_parameters import same_numeric_result
    cards = result.get('parameter_cards') or []
    old_gaps = [c for c in cards if c.get('required', True) and c['state'] != 'confirmed']
    old_summary = ' '.join(c['label']+': '+c['reason'] for c in old_gaps)[:2500]
    precision_corrected = False
    for card in cards:
        if (card.get('role') == 'calculation' and str(card.get('id', '')).startswith('calculation:')
                and card.get('state') == 'possible_mismatch'
                and card.get('reason') == 'Результат формулы не совпадает с объёмом ВОР.'
                and same_numeric_result(card.get('declared_value'), card.get('found_value'))):
            card.update(state='confirmed', reason='Формула пересчитана программно; значения совпадают с учётом машинной точности числа Excel.')
            precision_corrected = True
    if precision_corrected and result['summary'] == old_summary:
        gaps = [c for c in cards if c.get('required', True) and c['state'] != 'confirmed']
        result['summary'] = (' '.join(c['label']+': '+c['reason'] for c in gaps)[:2500]
                             or 'Расхождений в арифметике не обнаружено; проверяются остальные основания.')
    rejected=False
    from normative_workbook_arithmetic import arithmetic_claims
    arithmetic_verified = 'workbook:formula' in arithmetic_claims(result)
    for item in result.get('items',[]):
        item['explanation']=readable(item.get('explanation',''))
        if (arithmetic_verified and item.get('claim_ids') == ['workbook:formula']
                and item.get('state') == 'needs_facts'):
            result.setdefault('evidence_trace', []).append(dict(
                kind='arithmetic_item_projection_v1', original_item=deepcopy(item)))
            item.update(state='confirmed', explanation='Арифметика формулы ВОР проверена программно: '
                'результат совпадает с указанным объёмом. Физические основания расхода и единицы проверяются отдельно.')
        if absence_claim(item):
            result.setdefault('evidence_trace', []).append(dict(
                kind='absence_only_item_downgrade_v1', original_item=deepcopy(item)))
            item['state']='needs_facts'
            rejected=True
    if (rejected or precision_corrected) and result.get('state')=='possible_mismatch' and not any(i.get('state')=='possible_mismatch' for i in result.get('items',[])):
        result['state']='needs_facts'
    from normative_parent_basis import verified_geometry_dependency
    allocation_dependency=verified_allocation_dependency(result)
    if verified_geometry_dependency(result) or allocation_dependency:
        # Render a certified dependency separately from the material's still
        # unresolved rate, unit or identity. Raw model items remain immutable.
        ids=list(dict.fromkeys(sid for card in result.get('parameter_cards',[])
            if card['id'] in {'quantity','thickness'} and card['state']=='confirmed'
            for sid in card.get('source_ids',[])))
        dependency=dict(topic='Геометрическое основание расчёта материала',state='confirmed',
            required=True,claim_ids=['derived:parent'],project_source_ids=ids,normative_source_ids=[],
            explanation='Исходные количество, единица и толщина родительской работы подтверждены по ПД. '
                'Применимость нормы, расход и свойства материала проверяются отдельно.')
        if allocation_dependency:
            dependency.update(topic='Плановое распределение проектного объёма материала',
                explanation='Полная проектная потребность и заявленное распределение материала '
                    'между операциями пересобраны по буквальным источникам и ВОР. '
                    'Объёмы отдельных физических слоёв, площади и частичные толщины работ '
                    'не подтверждаются. Расход, коэффициент и нормативная применимость проверяются отдельно.')
        resolved=[]
        for item in result.get('items',[]):
            claims=item.get('claim_ids') or []
            if 'derived:parent' not in claims or item.get('state')!='needs_facts':
                resolved.append(item)
                continue
            remaining=[claim for claim in claims if claim!='derived:parent']
            if remaining:
                resolved.append({**item,'claim_ids':remaining})
            resolved.append(deepcopy(dependency))
        if not any('derived:parent' in i.get('claim_ids',[]) for i in resolved):
            resolved.append(dependency)
        result['items']=resolved
    return result


def verified_quantity(review, position_key, unit, *, derived=False, position=None):
    """Use a scoped, source-backed quantity in the UI without mutating matching."""
    if derived or not review or review.get('position_key') != position_key:
        return None
    def normalize(value):
        return str(value).strip().rstrip('.').replace('²','2').replace('³','3')
    from normative_curb_quantity import verified_mismatch
    mismatch=any(c.get('id')=='quantity' and c.get('state')=='possible_mismatch'
        for c in review.get('parameter_cards') or []) and verified_mismatch(position,review)
    card = next((c for c in review.get('parameter_cards') or [] if c.get('id') == 'quantity'
        and (c.get('state') == 'confirmed' or c.get('state')=='possible_mismatch' and mismatch)
        and c.get('source_ids') and c.get('found_value') is not None
        and normalize(c.get('found_unit')) == normalize(unit)), None)
    return (card['found_value'], card['found_unit']) if card else None


def required_claims(stored, report_status, derived=False):
    unresolved = {c['claim_id'] for c in stored.get('checks') or []
        if c.get('claim_id') and c.get('required', True)
        and c.get('state') not in {'confirmed', 'not_applicable'}}
    if report_status != 'ОК' and not unresolved:
        unresolved.add('matching:verdict')
    unresolved.add('normative:applicability')
    if derived:
        unresolved.update({'derived:rate', 'derived:parent'})
    return unresolved


def confirmed_comment_aggregate(stored, confirmed):
    """A derived comment badge follows its explicit, fully proved predicates."""
    checks = stored.get('checks') or []
    aggregate = next((c for c in checks if c.get('claim_id') == 'comment'), None)
    if (not aggregate or aggregate.get('reasons') != ['comment_predicates_evaluated']
            or aggregate.get('state') in {'contradiction', 'source_conflict'}):
        return False
    constituents = [c for c in checks if ':comment:' in c.get('claim_id', '')
        and c.get('required', True)]
    return bool(constituents) and all(
        c.get('state') in {'confirmed', 'not_applicable'} or
        (c.get('state') not in {'contradiction', 'source_conflict'} and c['claim_id'] in confirmed)
        for c in constituents)



def _allocation_cards_supported(review,position=None):
    """A recomputed allocation still needs its actual output/base/rate cards."""
    from normative_layer_assembly import ALLOCATION_PROOF
    from normative_parameters import measure,number,same_numeric_result
    events=[e for e in review.get('evidence_trace',[]) if e.get('kind')==ALLOCATION_PROOF]
    if len(events)!=1:
        return False
    event=events[0]
    index=position.index if position is not None else event.get('position_index')
    operands=[o for o in event['operands'] if o['material_index']==index]
    if len(operands)!=1:
        return False
    operand=operands[0]
    cards=review.get('parameter_cards',[])
    def one(predicate):
        matches=[c for c in cards if predicate(c)]
        return matches[0] if len(matches)==1 else {}
    quantity=one(lambda c:c.get('id')=='quantity')
    base=one(lambda c:c.get('id','').startswith(f'factor:{index}:')
        and number(c.get('declared_value'))==number(operand['declared_volume']))
    arithmetic=one(lambda c:c.get('id')==f'calculation:{index}')
    coefficient=one(lambda c:c.get('id','').startswith(f'factor:{index}:')
        and number(c.get('declared_value'))==number(operand['coefficient']))
    for card,value,role in ((quantity,operand['calculated_material_volume'],'calculation'),
            (base,operand['declared_volume'],'calculation_declared_allocation')):
        if (card.get('state')!='confirmed' or card.get('role')!=role
                or measure(card.get('found_value'),card.get('found_unit',''))!=measure(value,operand['unit'])
                or set(card.get('source_ids',[]))!=set(event['root_source_ids'])
                or card.get('source_anchors')!=event['source_anchors']
                or card.get('quote')!=event['project_basis']['volume']['scope_quote']):
            return False
    return bool(arithmetic.get('state')=='confirmed'
        and same_numeric_result(arithmetic.get('found_value'),operand['calculated_material_volume'])
        and coefficient.get('state')=='confirmed' and coefficient.get('role')=='coefficient')


def verified_allocation_dependency(review, position=None):
    """One dependency predicate for publication, planning and presentation."""
    from normative_layer_assembly import verified_material_allocation
    return verified_material_allocation(review, position) and _allocation_cards_supported(review, position)


def publish(report, stored, position_key, validation, *, enabled=False, derived=False, position=None):
    """A failed/unsupported second layer is uncertainty, never a proved mismatch."""
    if not enabled and not validation:
        return report
    if report['Статус аудита'] == 'Проверка не выполнена':
        return report
    output = deepcopy(report)
    stage = (validation or {}).get('stage')
    review = (validation or {}).get('review')
    if review and review.get('position_key') != position_key:
        review = None
    if review:
        review=readable_review(review)
        from normative_claim_migration import migrated_matching
        stored=migrated_matching(stored,review,position_key)
    if review and stage in {'pending', 'dispatched', 'running'}:
        from normative_local_checks import LOCAL_RULES_VERSION
        if review.get('local_rules_version', 0) < LOCAL_RULES_VERSION:
            output['Статус аудита'] = PENDING
            output['Что не так'] = 'Автоматически обновляем основание по новым правилам. Готовый результат сохранён; новый запрос к модели не выполняется.'
            return output
    if not review:
        pending = stage in {'pending', 'dispatched', 'running'} or (enabled and stage is None)
        output['Статус аудита'] = PENDING if pending else REVIEW
        output['Что не так'] = ('Сопоставление выполнено. Проверяем основание по нормативам.' if pending else
            'Нормативная валидация не завершена или недостаточно источников. Расхождение не подтверждено.')
        return output
    if review.get('execution_state') == 'technical_failure':
        output['Статус аудита'] = 'Проверка не выполнена'
        output['Что не так'] = review.get('summary') or 'Технический сбой нормативного этапа; вывод о корректности ВОР не получен.'
        return output
    items = review.get('items') or []
    from normative_workbook_conflicts import conflicts as workbook_conflicts
    internal_conflicts=workbook_conflicts(position,position_key)
    if internal_conflicts:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(finding['message'] for finding in internal_conflicts)
        return output
    arithmetic_errors = [v.get('message') or 'Ошибка арифметики формулы ВОР.' for v in stored.get('workbook_notes') or []
        if v.get('kind') == 'formula-mismatch']
    if arithmetic_errors:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(arithmetic_errors)
        return output
    from normative_workbook_rate import project_consumption_supported,geometric_rate_replay_supported
    if not geometric_rate_replay_supported(position,review):
        output['Статус аудита']=REVIEW
        output['Что не так']='Сохранённое основание геометрического расхода не воспроизводится по текущим буквальным источникам, параметрам и карточкам.'
        return output
    from normative_layer_assembly import single_base_price_replay_supported
    if not single_base_price_replay_supported(position,review):
        output['Статус аудита']=REVIEW
        output['Что не так']='Применение двух сметных расценок к одному основанию не воспроизводится по текущей конструкции ПД, нормативному условию и исходным поправкам толщины.'
        return output
    supported = [v for v in items if source_supported(v) and project_consumption_supported(v,
        review.get('sources',[]),position=position,parameters=review.get('parameter_proposals',[]),items=items)]
    contradictions = [v for v in supported if v.get('required', True)
        and v.get('state') == 'possible_mismatch' and not absence_claim(v)]
    from normative_parent_basis import verified_geometry_dependency
    geometry_dependency = derived and verified_geometry_dependency(review,position)
    from normative_layer_assembly import verified_material_allocation
    allocation_dependency = derived and verified_material_allocation(review,position)
    if allocation_dependency and not _allocation_cards_supported(review,position):
        output['Статус аудита']=REVIEW
        output['Что не так']='Плановое распределение не заменяет проверку расчётного результата, исходной части, арифметики и коэффициента.'
        return output
    # Routing a PD-only derived:parent is not certification. Only the binder's
    # source/proposal-bound proof, rebuilt from literal cards, may close this
    # new route. Preserve the existing normative full-parent route separately.
    confirmed = {claim for v in supported if v.get('state') == 'confirmed' for claim in v['claim_ids']
        if claim != 'derived:parent' or v.get('normative_source_ids') or geometry_dependency or allocation_dependency}
    from normative_workbook_arithmetic import arithmetic_claims
    confirmed.update(arithmetic_claims(review))
    if position is not None:
        from normative_coefficient_policy import accepted_claims
        accepted=accepted_claims(position,review)
        if allocation_dependency and 'derived:rate' not in accepted:
            confirmed.discard('derived:rate')
        confirmed.update(accepted)
    elif allocation_dependency:
        confirmed.discard('derived:rate')
    if geometry_dependency or allocation_dependency:
        confirmed.add('derived:parent')
    if confirmed_comment_aggregate(stored, confirmed):
        confirmed.add('comment')
    needed = required_claims(stored, report['Статус аудита'], derived)
    cards = review.get('parameter_cards') or []
    if review.get('pipeline_version', 1) >= 3:
        from normative_parameters import numeric_card_keys
        # Revalidate older saved reviews too. A source ID alone does not prove
        # the model's numerical interpretation or a mismatched thickness.
        contradictions = [v for v in contradictions if not numeric_card_keys(v['claim_ids']) or all(
            any(c.get('id') == key and c['state'] == 'possible_mismatch' for c in cards)
            for key in numeric_card_keys(v['claim_ids']))]
    card_gaps = [c for c in cards if c.get('required', True) and c['state'] != 'confirmed']
    def disputed_scope(card):
        key = card.get('id')
        return any(v.get('required', True) and v.get('state') == 'needs_facts' and any(
            claim == 'quantity' if key == 'quantity' else 'thickness' in claim if key == 'thickness' else False
            for claim in v.get('claim_ids', [])) for v in items)
    card_conflicts = [c for c in card_gaps if c['state'] == 'possible_mismatch' and not disputed_scope(c)]
    if card_conflicts and 'normative:applicability' in confirmed:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(c['label']+': '+c['reason'] for c in card_conflicts)
        return output
    from normative_curb_quantity import verified_mismatch as verified_curb_mismatch
    independent_quantity=[item for item in contradictions if item.get('claim_ids')==['quantity']
        and verified_curb_mismatch(position,review,item=item)]
    if independent_quantity:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(item['explanation'] for item in independent_quantity)
    elif contradictions and (review.get('pipeline_version',1) < 3 or 'normative:applicability' in confirmed):
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(v['explanation'] for v in contradictions)
    elif items and needed <= confirmed and not card_gaps and all(
            v.get('state') == 'confirmed' or (v.get('state') == 'needs_facts'
                and v.get('claim_ids') and set(v['claim_ids']) <= confirmed)
            or v.get('required') is False for v in items):
        output['Статус аудита'] = 'ОК'
        output['Что не так'] = 'Основание работы подтверждено по ПД и нормативам. ' + next(
            (v['explanation'] for v in supported if v.get('topic') == 'Основание работы подтверждено'), review.get('summary', ''))
    else:
        output['Статус аудита'] = REVIEW
        uncertain = [v['explanation'] for v in items if v.get('state') == 'needs_facts'
            and v.get('required') is not False
            and not (v.get('claim_ids') and set(v['claim_ids']) <= confirmed)]
        output['Что не так'] = (' '.join(uncertain) or (review.get('summary') if not items else '')
            or 'Недостаточно нормативных оснований для окончательного вывода.')
        if needed - confirmed:
            output['Что не так'] += ' Не все условия подтверждены; расхождение не доказано.'
        if card_gaps:
            output['Что не так'] = ' '.join(c['label']+': '+c['reason'] for c in card_gaps)
    return output


def apply_parent_verdicts(rows, *, enabled, running, verified_dependencies=()):
    """A derived material cannot be confirmed before its parent work basis."""
    if not enabled:
        return rows
    by_index = {r.position_index: r for r in rows}
    for row in rows:
        if not row.related_work or row.queue in {'fix', 'missing'}:
            continue
        if row.position_index in verified_dependencies:
            continue
        parent = by_index.get(row.related_work['index'])
        if (parent is None and running) or (parent is not None and parent.queue == 'pending'):
            row.queue, row.report_status = 'pending', PENDING
            row.problem = f"Расчётный материал работы №{row.related_work['number']}. Ожидаем проверку основания связанной работы."
        elif row.queue == 'ok' and (parent is None or parent.queue != 'ok'):
            row.queue, row.report_status = 'review', REVIEW
            row.problem = f"Расчёт связан с работой №{row.related_work['number']}, но её основание ещё не подтверждено."
    return rows


def finalize_state(result, stored, position_key, *, derived=False, position=None, relation=None):
    """Persist the same validated status that the row publisher computes.

    Previously every new agent result was stored as needs_facts even when all
    required claims and numeric cards passed. This uses the existing guards;
    it cannot promote an unsupported model assertion.
    """
    output = deepcopy(result)
    from normative_source_roles import unread_visual_source
    if any(unread_visual_source(source) for source in output.get('sources', [])):
        # Keep persisted assertion/card states as conservative as the published
        # view, even for a direct caller that skipped main item validation.
        output = readable_review(output)
    if position is not None:
        from normative_workbook_conflicts import conflicts,PROOF
        output['evidence_trace']=[event for event in output.get('evidence_trace',[]) if event.get('kind')!=PROOF]
        output['evidence_trace'].extend(conflicts(position,position_key))
    report = publish({'Статус аудита':'ОК' if stored.get('status')=='ОК' else REVIEW},
        stored,position_key,{'stage':'done','review':dict(result,position_key=position_key)},
        enabled=True,derived=derived,position=position)
    output['state'] = 'confirmed' if report['Статус аудита']=='ОК' else 'possible_mismatch' if report['Статус аудита']==FIX else 'needs_facts'
    output['requires_engineer_review'] = output['state']!='confirmed'
    if position is not None:
        from normative_evidence_plan import VERSION, build_work_plan, position_data
        output['evidence_trace'] = [e for e in output.get('evidence_trace', []) if e.get('kind') != VERSION]
        output['evidence_trace'].append(build_work_plan(position_data(position), output,
            stored, derived=derived, relation=relation))
    return output
