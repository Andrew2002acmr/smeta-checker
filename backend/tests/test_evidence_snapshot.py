from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from normative_evidence_snapshot import read_answer, snapshot_answer, store_answer


def sample():
    answer = dict(items=[dict(state='confirmed', project_source_ids=['cell'], normative_source_ids=[])],
        parameters=[dict(source_id='cell', operand_source_ids=[])])
    return answer, [dict(id='cell', text='4388,24', rects=[[1, 2, 3, 4]])], dict(pdf='v1', kb='k1', rules='r1')


def test_later_validation_and_registry_mutation_cannot_destroy_original_answer():
    answer, sources, binding = sample()
    original = deepcopy((answer, sources))
    snapshot = snapshot_answer(answer, sources, binding)
    answer['items'][0]['state'] = 'needs_facts'
    sources[0]['text'] = 'different'
    restored = read_answer(snapshot, binding)
    assert (restored['answer'], restored['sources']) == original
    restored['answer']['items'].clear()
    assert snapshot['content']['answer']['items']


@pytest.mark.parametrize('field', ['pdf', 'kb', 'rules'])
def test_changed_input_or_rules_requires_explicit_compatibility_decision(field):
    answer, sources, binding = sample()
    snapshot = snapshot_answer(answer, sources, binding)
    with pytest.raises(ValueError, match='different inputs'):
        read_answer(snapshot, {**binding, field: 'changed'})


def test_tampering_and_duplicate_source_handles_are_rejected():
    answer, sources, binding = sample()
    snapshot = snapshot_answer(answer, sources, binding)
    snapshot['content']['sources'][0]['text'] = '106'
    with pytest.raises(ValueError, match='integrity'):
        read_answer(snapshot, binding)
    with pytest.raises(ValueError, match='Duplicate source'):
        snapshot_answer(answer, sources * 2, binding)


def test_bad_model_reference_is_preserved_for_validator_without_inventing_source():
    answer, sources, binding = sample()
    answer['parameters'][0]['source_id'] = 'missing'
    snapshot = snapshot_answer(answer, sources, binding)
    assert snapshot['content']['unresolved_source_ids'] == ['missing']
    assert snapshot['content']['answer'] == answer


def test_persistence_is_bound_and_never_overwrites_a_different_raw_answer():
    answer, sources, binding = sample()
    context = SimpleNamespace(repository=SimpleNamespace(supports_analysis_replay=True))
    identities = []
    def save(context, identity, value):
        identities.append(identity)
        return deepcopy(value)
    with patch('normative_evidence_snapshot.normative_identity', return_value=binding), patch('normative_evidence_snapshot.put', side_effect=save):
        first = store_answer(context, {'project_id':'project'}, 0, None, answer, sources)
        answer['items'][0]['state'] = 'needs_facts'
        second = store_answer(context, {'project_id':'project'}, 0, None, answer, sources)
    assert first['key'] != second['key']
    assert identities[0]['binding_sha256'] == identities[1]['binding_sha256']


def test_explicit_replay_allows_only_top_level_validator_version_change():
    answer, sources, _ = sample()
    binding = dict(algorithm='old', inputs=dict(pdf='v1', vor='v1', kb='k1', human_revision='h1'))
    snapshot = snapshot_answer(answer, sources, binding)
    current = {**binding, 'algorithm':'new'}
    with pytest.raises(ValueError, match='different inputs'):
        read_answer(snapshot, current)
    assert read_answer(snapshot, current, allow_validator_change=True)['answer'] == answer
    for field in binding['inputs']:
        changed = deepcopy(current)
        changed['inputs'][field] = 'changed'
        with pytest.raises(ValueError, match='different inputs'):
            read_answer(snapshot, changed, allow_validator_change=True)


def test_original_items_replace_old_demotion_but_current_validator_decides():
    from normative_evidence_snapshot import revalidate_answer
    binding = dict(algorithm='old', inputs=dict(request=dict(question='check', pages=[])))
    answer = dict(summary='Original',items=[dict(topic='Work', state='confirmed', explanation='Original source',
        claim_ids=['presence'],project_source_ids=['p'],normative_source_ids=[])], parameters=[], requests=[])
    snapshot = snapshot_answer(answer, [dict(id='p',kind='project',text='literal')], binding)
    reference = dict(key='key',sha256=snapshot['sha256'])
    row = dict(id='previous',payload=dict(position_check=dict(pipeline_version=4,
        coverage=dict(agent_evidence_snapshot=reference), parameter_cards=[dict(state='confirmed')],
        items=[dict(state='needs_facts')], evidence_trace=[dict(kind='old_normative_verdict')])) )
    original = deepcopy(row)
    context = SimpleNamespace(repository=SimpleNamespace(analysis_artifact=lambda *args:dict(
        identity=dict(stage='normative_agent_evidence'),payload=snapshot)))
    def current_validator(context, record, index, restored, actor, *, register):
        check = restored['payload']['position_check']
        assert check['items'][0]['state'] == 'confirmed'
        assert 'parameter_cards' not in check
        assert not any(e['kind']=='old_normative_verdict' for e in check['evidence_trace'])
        assert not register
        return 'current-guard-result'
    with patch('normative_evidence_snapshot.normative_identity',return_value={**binding,'algorithm':'new'}),\
            patch('normative_position_check.validate_agent_saved',side_effect=current_validator):
        assert revalidate_answer(context,{'project_id':'project'},0,row,'test') == 'current-guard-result'
    assert row == original
