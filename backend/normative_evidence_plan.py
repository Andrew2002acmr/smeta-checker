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
        actions.append(dict(key=key, family=family, state=state, action=action,
            dependency_position=dependency, source_ids=refs, unreadable_source_ids=unreadable,
            model_delivery=delivery,
            read_requests=[dict(tool='read_source',source_id=sid,
                offset=delivery['next_offset_by_source'].get(sid, 0)) for sid in unread]
                if action == 'read_registered_source' else [],
            proposal_count=len(proposals), reason=(card or item or {}).get('reason') or
                (item or {}).get('explanation', ''),
            retry_fingerprint=_identity([scope, key, numeric, available, proposals,
                delivery['source_states'],delivery['next_offset_by_source']])))
        if key.startswith('claim:'):
            from normative_verification_contract import contract
            claim = key.removeprefix('claim:')
            actions[-1]['verification'] = contract(claim, semantic_specs.get(claim))
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
    return dict(kind=VERSION, schema_revision=3, scope=scope, actions=actions,
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
