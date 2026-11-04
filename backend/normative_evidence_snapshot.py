"""Content-addressed agent evidence, independent of subsequent verdict mutation."""
from copy import deepcopy

from analysis_replay import digest, enabled, normative_identity, put


def snapshot_answer(answer, sources, binding):
    ids = [source['id'] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source IDs in agent evidence')
    registry = set(ids)
    references = set()
    for item in answer.get('items', []):
        references.update(item.get('project_source_ids', []))
        references.update(item.get('normative_source_ids', []))
    for parameter in answer.get('parameters', []):
        references.add(parameter['source_id'])
        references.update(parameter.get('operand_source_ids', []))
    # Malformed model proposals remain evidence too: retain them for current
    # validators, explicitly marking unresolved handles instead of repairing IDs.
    content = deepcopy(dict(schema=1, phase='agent_answer_before_position_validation',
        binding=binding, answer=answer, sources=sources,
        unresolved_source_ids=sorted(references-registry)))
    return dict(content=content, sha256=digest(content))


def store_answer(context, record, index, request, answer, sources):
    if not enabled(context):
        return None
    binding = normative_identity(context, record, index, request)
    if binding is None:
        return None
    snapshot = snapshot_answer(answer, sources, binding)
    identity = dict(schema=1, stage='normative_agent_evidence',
        project_id=str(record['project_id']), binding_sha256=digest(binding),
        evidence_sha256=snapshot['sha256'])
    saved = put(context, identity, snapshot)
    if saved != snapshot:
        raise ValueError('Stored evidence differs from content-addressed snapshot')
    from normative_evidence_cache import source_contract
    return dict(stage=identity['stage'], key=digest(identity), sha256=snapshot['sha256'],
        source_contract=deepcopy(source_contract()))


def read_answer(snapshot, expected_binding, *, allow_validator_change=False):
    """Return a fresh copy only after integrity and exact binding validation.

    This does not grant an OK or bypass any current validator. Cross-version
    compatibility and automatic replay are deliberately separate decisions.
    """
    content = snapshot['content']
    if snapshot['sha256'] != digest(content):
        raise ValueError('Evidence integrity check failed')
    actual_binding = deepcopy(content['binding'])
    expected = deepcopy(expected_binding)
    if allow_validator_change:
        actual_binding.pop('algorithm', None)
        expected.pop('algorithm', None)
    if content['schema'] != 1 or actual_binding != expected:
        raise ValueError('Evidence belongs to different inputs')
    return deepcopy(content)


def revalidate_answer(context, record, index, row, actor):
    """Explicit local replay from original evidence through current guards.

    Only the top-level normative algorithm may differ. PDF preparation,
    matching facts, human revisions, related rows, KB and model stay pinned.
    No automatic cache hit or new provider request is introduced here.
    """
    from workspace_models import NormativePositionCheckRequest
    from normative_position_check import AgentAnswer, validate_agent_saved
    from normative_thickness_adjustment import proposal_events
    reference = row['payload']['position_check']['coverage']['agent_evidence_snapshot']
    artifact = context.repository.analysis_artifact(record['project_id'], reference['key'])
    if artifact is None or artifact['identity']['stage'] != 'normative_agent_evidence':
        raise ValueError('Original agent evidence is unavailable')
    snapshot = artifact['payload']
    if snapshot['sha256'] != reference['sha256']:
        raise ValueError('Original agent evidence reference mismatch')
    request = NormativePositionCheckRequest(**snapshot['content']['binding']['inputs']['request'])
    binding = normative_identity(context, record, index, request)
    if binding is None:
        raise ValueError('Current evidence binding is unavailable')
    content = read_answer(snapshot, binding, allow_validator_change=True)
    parsed = AgentAnswer.model_validate(content['answer'])
    if parsed.requests:
        raise ValueError('Original agent answer requires unfinished source reads')
    restored = deepcopy(row)
    previous = restored['payload']['position_check']
    # Drop old decisions and derived parameter cards. Preserve only independent
    # preparation proofs; derive all normative decisions again from raw items.
    upstream = [event for event in previous.get('evidence_trace', [])
        if event.get('kind') in {'claim-parser-corrections-v1', 'workbook_arithmetic_proof_v1'}]
    restored['payload']['position_check'] = dict(
        summary=parsed.summary, items=[item.model_dump() for item in parsed.items],
        parameter_proposals=[parameter.model_dump() for parameter in parsed.parameters],
        sources=content['sources'], pipeline_version=previous['pipeline_version'],
        execution_state='complete', state='needs_facts',
        related_positions=deepcopy(previous.get('related_positions', [])),
        coverage=dict(agent_evidence_snapshot=deepcopy(reference),
            **{key:deepcopy(previous['coverage'][key]) for key in
                ('evidence_followup','evidence_followup_failure','coefficient_policy') if previous.get('coverage',{}).get(key)}),
        evidence_trace=upstream + proposal_events(parsed.adjustments) + [dict(
            kind='original_agent_evidence_replay', key=reference['key'],
            evidence_sha256=reference['sha256'], original_review_id=str(row['id']),
            original_algorithm=content['binding']['algorithm'], current_algorithm=binding['algorithm'])])
    return validate_agent_saved(context, record, index, restored, actor, register=False)
