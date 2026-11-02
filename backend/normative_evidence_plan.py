"""Executable evidence obligations, separate from engineering verdicts.

A saved model explanation is never evidence that a document is missing.
This plan describes the current review, not the completeness of the project.
"""
import hashlib
import json

VERSION = 'evidence_work_plan_v1'


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
    if claim in {'quantity', 'unit', 'presence'} or claim.startswith('geometry:'):
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


def build_work_plan(position, review, matching, *, derived=False, relation=None):
    """One obligation per claim/card; pin identity to actual evidence and scope.

    Actions cannot confirm a claim. Their consumers must still run the normal
    literal, numeric, scope and normative validators. No source => unsearched,
    not a request to the user for a missing document.
    """
    from normative_verdict import required_claims, source_supported, confirmed_comment_aggregate
    relation = relation or {}
    sources = {s['id']: s for s in review.get('sources', [])}
    items = [i for i in review.get('items', []) if i.get('required', True)]
    confirmed = {claim for item in items if item.get('state') == 'confirmed'
        and source_supported(item) for claim in item.get('claim_ids', [])}
    from normative_workbook_arithmetic import arithmetic_claims
    confirmed.update(arithmetic_claims(review))
    if any(e.get('kind')=='named_coefficient_policy_v1' for e in review.get('evidence_trace',[])):
        from audit_models import VorPosition
        from normative_coefficient_policy import accepted_claims
        confirmed.update(accepted_claims(VorPosition.model_validate(position),review))
    if derived and any(e.get('kind')=='parent_geometry_dependency_v1' for e in review.get('evidence_trace',[])):
        from normative_parent_basis import verified_geometry_dependency
        from audit_models import VorPosition
        if verified_geometry_dependency(review,VorPosition.model_validate(position)):
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
        refs = sorted(set(references))
        available = [sources[sid] for sid in refs if sid in sources]
        unreadable = [s['id'] for s in available if s.get('origin') == 'uncertain_table'
            or (s.get('origin', '').startswith('ocr') and not s.get('numeric_verified'))]
        proposals = [p for p in review.get('parameter_proposals', [])
            if card and p.get('parameter') == card.get('id')]
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
        numeric = {k: card.get(k) for k in ('id', 'state', 'declared_value', 'found_value', 'unit', 'found_unit')} if card else None
        actions.append(dict(key=key, family=family, state=state, action=action,
            dependency_position=dependency, source_ids=refs, unreadable_source_ids=unreadable,
            proposal_count=len(proposals), reason=(card or item or {}).get('reason') or
                (item or {}).get('explanation', ''),
            retry_fingerprint=_identity([scope, key, numeric, available, proposals])))

    for claim in sorted(open_claims):
        relevant = [i for i in items if claim in i.get('claim_ids', []) and i.get('state') != 'confirmed']
        append('claim:' + claim, _family(claim), [sid for i in relevant
            for field in ('project_source_ids', 'normative_source_ids') for sid in i.get(field, [])],
            item=relevant[0] if relevant else None)
    for card in review.get('parameter_cards', []):
        if card.get('required', True) and card.get('state') != 'confirmed':
            family = ('project_scope' if card['id'] in {'quantity', 'thickness'} else
                'formula_operand' if card['id'].startswith('factor:') else 'numeric_basis')
            append('card:' + card['id'], family, card.get('source_ids', []), card=card)
    for index, item in enumerate(items):
        if item.get('state') != 'confirmed' and not item.get('claim_ids'):
            append(f'unbound_item:{index}', 'claim_identity',
                item.get('project_source_ids', []) + item.get('normative_source_ids', []), item=item)
    return dict(kind=VERSION, scope=scope, actions=actions,
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
