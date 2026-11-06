"""Executable evidence obligations, separate from engineering verdicts.

A saved model explanation is never evidence that a document is missing.
This plan describes the current review, not the completeness of the project.
"""
import hashlib
import json
from typing import Any

VERSION = 'evidence_work_plan_v2'


def source_delivery(references, sources, coverage) -> dict[str, Any]:
    """Exposure of this exact evidence in the latest successful context.

    Packing is not dispatch success; exposure is not comprehension or proof.
    Legacy snapshots cannot establish either and remain explicitly unmeasured.
    """
    from normative_source_registry import source_fingerprint
    completed = [p for p in coverage.get('model_context_packing', [])
        if p.get('response_state') == 'completed' and p.get('delivery_contract_version') == 1]
    latest = completed[-1] if completed else None
    states, offsets = {}, {}
    for sid in references:
        source = sources.get(sid)
        if source is None:
            states[sid] = 'invalid_reference'
        elif latest is None:
            states[sid] = 'not_measured'
        elif (latest.get('source_fingerprints') or {}).get(sid) != source_fingerprint(source):
            states[sid] = 'current_evidence_not_measured'
        elif sid in latest.get('full_text_source_ids', []):
            states[sid] = 'full_text'
        elif sid in latest.get('supplied_source_ids', []):
            states[sid] = 'partial_text'
            offset = (latest.get('next_offset_by_source') or {}).get(sid)
            if type(offset) is int and 0 <= offset < len(source.get('text', '')):
                offsets[sid] = offset
        else:
            states[sid] = 'not_sent'
    return dict(version=1, context_stage=latest.get('stage') if latest else None,
        source_states=states, next_offset_by_source=offsets,
        comprehension_established=False, absence_established=False)


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        default=str).encode()).hexdigest()


def position_data(position):
    if hasattr(position, 'model_dump'):
        return position.model_dump(mode='json')
    result = {key: getattr(position, key, None) for key in ('work_name', 'unit', 'position_source')}
    source = result['position_source']
    if source is not None and hasattr(source, 'model_dump'):
        result['position_source'] = source.model_dump(mode='json')
    elif source is not None and not isinstance(source, dict):
        result['position_source'] = vars(source)
    return result


def project_query(position, materials=()):
    """Literal construction and section; never read volume or formula operands."""
    source = position.get('position_source') or {}
    sections = ' '.join(s.get('text', '') for s in source.get('sections', []))
    # Put material identity before machine-heavy work titles. Truncating the old
    # query at 300 characters could remove the only material designation.
    names = ' '.join(m.get('work', '') for m in materials)
    return (sections[:90] + ' ' + names[:110] + ' ' + position.get('work_name', '')[:180]).strip()[:300]


def _family(claim):
    if claim in {'quantity', 'unit', 'presence', 'thickness', 'input:area'} or claim.startswith('geometry:'):
        return 'project_scope'
    if claim == 'derived:parent':
        return 'parent_basis'
    if claim == 'derived:rate' or claim.startswith(('basis:', 'quantity:consumption')):
        return 'consumption_basis'
    if claim.startswith('normative:'):
        return 'norm_selection'
    if claim.startswith('attribute:'):
        return 'material_or_condition'
    if claim.startswith('method'):
        return 'work_method'
    return 'claim_identity'


def _open_reason(verification, spec, card):
    """Name the missing verification without claiming the source is absent."""
    if card is not None:
        label = card.get('label') or card['id']
        return f'Проверка «{label}» пока не имеет подтверждённого основания в текущем заключении.'
    expected = ' '.join((getattr(spec, 'expected_text', '') or getattr(spec, 'label', '')).split())
    subject = f' «{expected[:240]}»' if expected else ''
    if verification.get('property') == 'equipment_function':
        return (f'Для указанной операции{subject} не привязано доказательство, '
                'что функция или класс машины соответствует условиям применимой нормы и условиям проекта.')
    relation = verification.get('relation')
    if relation == 'same_subject_and_project_value':
        return f'Не привязан факт ПД{subject} о том же участке, конструкции и проверяемом параметре.'
    if relation == 'condition_applies_to_project_facts':
        return (f'Не привязано доказательство применимости нормативного условия{subject} '
                'к проверяемой проектной операции и её характеристикам.')
    if relation == 'operands_units_and_derivation':
        return f'Не установлена проверяемая цепочка исходных величин, единиц и расчёта{subject}.'
    if relation == 'satisfies_project_constraints':
        return (f'Для требования ВОР{subject} не привязано проектное основание '
                'или проверяемое обоснование совместимости с условиями ПД.')
    return f'В текущем заключении нет проверяемой привязки к обязательному требованию{subject}.'


def _appendix_candidate(position, review, sources):
    """Plan from a bound derived candidate; never treat it as certification.

    A conditional result is not a number to find literally in another PDF.
    Its remaining premises still require evidence, including negative project
    coverage where a normative fallback depends on absent project data.
    """
    from analysis_replay import digest
    from audit_models import VorPosition
    from normative_parameters import number, clean
    from normative_verdict import source_supported
    from normative_workbook_rate import analyze

    candidates = [event['appendix_rate_candidate']
        for event in review.get('evidence_trace', [])
        if event.get('kind') == 'workbook_area_rate_v1'
        and isinstance(event.get('appendix_rate_candidate'), dict)]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    try:
        recipe = analyze(VorPosition.model_validate(position))
        roots = candidate.get('root_source_ids')
        hashes = candidate.get('source_sha256')
        conditions = candidate.get('pending_condition_ids')
        if (not recipe or candidate.get('kind') != 'appendix_derived_rate_candidate_v1'
                or candidate.get('conditional_arithmetic_verified') is not True
                or candidate.get('normative_basis_verified') is not False
                or candidate.get('dependent_output_verified') is not False
                or number(candidate.get('rate')) != number(recipe['rate'])
                or clean(candidate.get('unit', '')) != clean(recipe['rate_unit'])
                or not isinstance(roots, list) or not 1 <= len(roots) <= 32
                or not all(isinstance(sid, str) and sid for sid in roots)
                or len(roots) != len(set(roots)) or not isinstance(hashes, dict)
                or set(roots) != set(hashes) or set(roots) - sources.keys()
                or any(digest(sources[sid]) != hashes[sid] for sid in roots)
                or candidate.get('proposals_sha256') != digest(review.get('parameter_proposals', []))
                or candidate.get('selected_applicability_sha256') != digest([
                    item for item in review.get('items', []) if item.get('state') == 'confirmed'
                    and 'normative:applicability' in item.get('claim_ids', []) and source_supported(item)])
                or not isinstance(conditions, list) or not 1 <= len(conditions) <= 12
                or not all(isinstance(item, str) and 0 < len(item) <= 100 for item in conditions)):
            return None
        return candidate
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _plan_appendix_conditions(position, review, actions, sources):
    """Keep independent work tasks; focus only the derived rate's obligations."""
    from analysis_replay import digest
    from normative_parameters import number
    from normative_workbook_rate import project_rate_claim

    candidate = _appendix_candidate(position, review, sources)
    if not candidate:
        return
    for action in actions:
        key = action['key']
        claim = key.removeprefix('claim:') if key.startswith('claim:') else ''
        factor = key.split(':')
        is_rate_card = (len(factor) == 4 and factor[:2] == ['card', 'factor']
            and factor[2] == str(position.get('index'))
            and number(factor[3]) == number(candidate['rate']))
        if not (project_rate_claim(claim) or is_rate_card):
            continue
        refs = sorted(set(action['source_ids']) | set(candidate['root_source_ids']))
        delivery = source_delivery(refs, sources, review.get('coverage') or {})
        unread = [sid for sid, state in delivery['source_states'].items()
            if state in {'partial_text', 'not_sent'}]
        action.update(state='source_derived_candidate_requires_conditions',
            action='read_registered_source' if unread else 'verify_applicability_conditions',
            source_ids=refs, model_delivery=delivery,
            read_requests=[dict(tool='read_source', source_id=sid,
                offset=delivery['next_offset_by_source'].get(sid, 0)) for sid in unread],
            conditional_rate_candidate={key: candidate[key] for key in (
                'kind', 'rate', 'unit', 'expression', 'root_source_ids',
                'pending_condition_ids', 'pending_conditions') if key in candidate},
            reason='Производный расход вычислен по связанным исходным данным; это условный кандидат, '
                'не подтверждение расхода. Не ищи итоговое число буквально в ПД или нормативе. '
                'Проверь оставшиеся условия: ' + '; '.join(candidate['pending_condition_ids']) + '. '
                'Площадь, толщина и арифметика сохраняют собственные проверки; '
                'зависимое количество материала этим кандидатом не подтверждено.')
        action['retry_fingerprint'] = digest([action['retry_fingerprint'], candidate, delivery])


def build_work_plan(position, review, matching, *, derived=False, relation=None) -> dict[str, Any]:
    """One obligation per claim/card; pin identity to actual evidence and scope.

    Actions cannot confirm a claim. Their consumers must still run the normal
    literal, numeric, scope and normative validators. No source => unsearched,
    not a request to the user for a missing document.
    """
    from normative_verdict import required_claims, source_supported, confirmed_comment_aggregate
    relation = relation or {}
    from audit_predicates import technical_specs
    semantic_specs={s.claim_id:s for s in technical_specs(position.get('work_name',''),
        position.get('comment',''),position.get('position_type',''),[],position.get('formula',''))}
    sources = {s['id']: s for s in review.get('sources', [])}
    items = [i for i in review.get('items', []) if i.get('required', True)]
    from normative_parent_basis import verified_geometry_dependency
    from normative_verdict import verified_allocation_dependency
    from audit_models import VorPosition
    current_position = VorPosition.model_validate(position) if derived else None
    geometry_dependency = derived and verified_geometry_dependency(review, current_position)
    allocation_dependency = derived and verified_allocation_dependency(review, current_position)
    confirmed = {claim for item in items if item.get('state') == 'confirmed'
        and source_supported(item) for claim in item.get('claim_ids', [])
        if claim != 'derived:parent' or item.get('normative_source_ids')
            or geometry_dependency or allocation_dependency}
    # Required aspects of the same claim are conjunctive. A model's broad C
    # must not erase a separately recorded N about its applicability. This
    # changes planning only; independent quantity/thickness cards stay intact.
    confirmed.difference_update(claim for item in items if item.get('state') != 'confirmed'
        for claim in item.get('claim_ids', []))
    from normative_workbook_arithmetic import arithmetic_claims
    confirmed.update(arithmetic_claims(review))
    if any(e.get('kind')=='named_coefficient_policy_v1' for e in review.get('evidence_trace',[])):
        from audit_models import VorPosition
        from normative_coefficient_policy import accepted_claims
        confirmed.update(accepted_claims(VorPosition.model_validate(position),review))
    if geometry_dependency or allocation_dependency:
        confirmed.add('derived:parent')
    if confirmed_comment_aggregate(matching, confirmed):
        confirmed.add('comment')
    open_claims = required_claims(matching,
        'ОК' if matching.get('status') == 'ОК' else 'Требуется ручная проверка', derived) - confirmed
    for item in items:
        if item.get('state') != 'confirmed':
            open_claims.update(set(item.get('claim_ids', [])) - confirmed)
    queries = [e for e in review.get('evidence_trace', [])
        if e.get('kind') == 'project_retrieval_page_v1']
    coverage = review.get('coverage') or {}
    source = position.get('position_source') or {}
    scope = dict(document_sha256=source.get('document_sha256'), sheet=source.get('sheet'),
        sections=source.get('sections'), position_id=source.get('position_id'),
        work=position.get('work_name'), unit=position.get('unit'),
        declared_volume=position.get('volume'), formula=position.get('formula'),
        calculation=position.get('calculation'), comment=position.get('comment'))
    actions = []

    def append(key, family, references, *, card=None, item=None):
        parameter = (('quantity' if card['id'] == 'input:area' else card['id'])
            if card and card['id'] in {'quantity','thickness','input:area'} else
            'coefficient' if card and card.get('role') == 'coefficient' else
            'unit_scale' if card and card.get('role') == 'unit_scale' else None)
        # Card handles name obligations; proposal.parameter names fact kinds.
        # Keep candidates of that kind without choosing by the expected value.
        proposals = [p for p in review.get('parameter_proposals', [])
            if parameter is not None and p.get('parameter') == parameter]
        refs = sorted(set(references) | {p['source_id'] for p in proposals if p.get('source_id')})
        available = [sources[sid] for sid in refs if sid in sources]
        unreadable = [s['id'] for s in available if s.get('origin') == 'uncertain_table'
            or (s.get('origin', '').startswith('ocr') and not s.get('numeric_verified'))]
        # These are measurable review states, not guesses based on its prose.
        state = ('source_reference_invalid' if set(refs) - sources.keys() else
            'source_quality_unverified' if unreadable else
            'candidate_requires_validation' if available or proposals else 'no_bound_evidence')
        parent = (relation.get('parent') or {}).get('index')
        dependency = parent if family == 'parent_basis' else None
        action = ('resolve_parent_basis' if dependency is not None else
            'verify_source_quality' if unreadable else
            'validate_candidate' if available or proposals else
            'retrieve_project' if family == 'project_scope' else
            'retrieve_normative_or_project_basis')
        delivery = source_delivery(refs, sources, coverage)
        unread = [sid for sid, status in delivery['source_states'].items()
            if status in {'partial_text', 'not_sent'}]
        if unread and not unreadable and not (set(refs) - sources.keys()) and dependency is None:
            action = 'read_registered_source'
        numeric = {k: card.get(k) for k in ('id', 'state', 'declared_value', 'found_value', 'unit', 'found_unit')} if card else None
        spec, verification = None, {}
        if key.startswith('claim:'):
            from normative_verification_contract import contract
            claim = key.removeprefix('claim:')
            spec = semantic_specs.get(claim)
            verification = contract(claim, spec)
        reason = ((card or item or {}).get('reason') or (item or {}).get('explanation')
                  or _open_reason(verification, spec, card))
        actions.append(dict(key=key, family=family, state=state, action=action,
            dependency_position=dependency, source_ids=refs, unreadable_source_ids=unreadable,
            model_delivery=delivery,
            read_requests=[dict(tool='read_source',source_id=sid,
                offset=delivery['next_offset_by_source'].get(sid, 0)) for sid in unread]
                if action == 'read_registered_source' else [],
            proposal_count=len(proposals), reason=reason,
            retry_fingerprint=_identity([scope, key, numeric, available, proposals,
                delivery['source_states'],delivery['next_offset_by_source']])))
        if verification:
            actions[-1]['verification'] = verification
            # A changed proof obligation is new work even with the same sources.
            actions[-1]['retry_fingerprint'] = _identity([
                actions[-1]['retry_fingerprint'], actions[-1]['verification']])

    for claim in sorted(open_claims):
        relevant = [i for i in items if claim in i.get('claim_ids', []) and i.get('state') != 'confirmed']
        spec=semantic_specs.get(claim)
        family='norm_selection' if spec and spec.property_label=='normative_instruction' else _family(claim)
        append('claim:' + claim, family, [sid for i in relevant
            for field in ('project_source_ids', 'normative_source_ids') for sid in i.get(field, [])],
            item=relevant[0] if relevant else None)
    for card in review.get('parameter_cards', []):
        if card.get('required', True) and card.get('state') != 'confirmed':
            family = ('project_scope' if card['id'] in {'quantity', 'thickness', 'input:area'} else
                'formula_operand' if card['id'].startswith('factor:') else 'numeric_basis')
            append('card:' + card['id'], family, card.get('source_ids', []), card=card)
    for index, item in enumerate(items):
        if item.get('state') != 'confirmed' and not item.get('claim_ids'):
            append(f'unbound_item:{index}', 'claim_identity',
                item.get('project_source_ids', []) + item.get('normative_source_ids', []), item=item)
    _plan_appendix_conditions(position, review, actions, sources)
    return dict(kind=VERSION, schema_revision=4, scope=scope, actions=actions,
        input_fingerprint=_identity([scope, actions]),
        project_search=dict(recorded_queries=len(queries),
            remaining_candidate_queries=sum(e.get('next_offset') is not None for e in queries),
            state='recorded' if queries else 'not_measured',
            pending_pages=coverage.get('pending_ocr_pages', []),
            error_pages=coverage.get('error_pages', []), absence_proven=False),
        policy=dict(retry_only_on_new_evidence=True, confirmation_requires_validation=True,
            manual_missing_document_established=False))


def pending_actions(plan, attempted_fingerprints=()):
    """An unchanged obligation is not another paid attempt at the same answer."""
    attempted = set(attempted_fingerprints)
    return [action for action in plan['actions'] if action['retry_fingerprint'] not in attempted]
