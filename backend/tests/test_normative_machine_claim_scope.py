"""Trade names never erase literal machine requirements or their conflicts."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from audit_predicates import technical_specs, typed_paver_comment
from human_basis import position_key
from normative_claim_migration import (
    VERSION, apply_review_migration, migrated_matching, normalize_matching,
)
from normative_position_check import AgentAnswer, CheckedItem, answer_feedback, validate_items


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/machine-pilot74-frozen.json').read_text(encoding='utf8'))


def position():
    return VorPosition.model_validate(fixture()['position'])


def specs(p):
    return technical_specs(p.work_name, p.comment, p.position_type, [], p.formula)


def plan(checks):
    return {c['claim_id'] for c in checks} | {'normative:applicability', 'quantity', 'unit', 'presence', 'method'}


@pytest.mark.parametrize('size', ['1', '2', '3'])
def test_literal_class_survives_brand_split_as_a_required_function(size):
    p = position()
    p.work_name = p.work_name.replace('3 типоразмера', size + ' типоразмера')
    p.comment = p.comment.replace('3 типоразмера', size + ' типоразмера')
    parsed = specs(p)
    brand = next(s for s in parsed if s.claim_id == 'attribute:comment:condition')
    method = next(s for s in parsed if s.claim_id == 'method:comment:equipment:1')
    assert not brand.required and brand.predicate == 'advisory'
    assert method.required and method.expected_text == 'Укладка а/укладчиком ' + size + ' типоразмера'
    assert 'Vögele' not in method.expected_text and '1600' not in method.expected_text


@pytest.mark.parametrize('comment', [
    'Укладка а/укладчиком Vögele SUPER 1600 3 типоразмера мощностью 79 кВт',
    'Укладка а/укладчиком Vögele SUPER 1600 3 типоразмера шириной 6 м',
    'Укладка а/укладчиком Vögele SUPER 1600 3 типоразмера\nШирина не менее 6 м',
    'Укладка а/укладчиком Vögele SUPER 1600 не 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 2-3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 2- 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 2– 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 2‑ 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 2 или 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 только 3 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 4 типоразмера',
    'Укладка а/укладчиком Vögele SUPER 1600 3 типоразмера выполнять проходы дважды',
])
def test_limits_ranges_negation_and_unknown_tail_remain_whole_required_conditions(comment):
    p = position()
    p.comment = comment
    assert typed_paver_comment(p.work_name, comment) is None
    condition = next(s for s in specs(p) if s.claim_id == 'attribute:comment:condition')
    assert condition.required and condition.expected_text == comment
    migrated = normalize_matching(p, {'checks': []})
    assert any(c['claim_id'] == condition.claim_id and c['required'] for c in migrated['checks'])


def test_actual63_work_class3_comment_class2_is_not_retired_or_downgraded():
    p = VorPosition.model_validate(fixture()['conflict63'])
    assert typed_paver_comment(p.work_name, p.comment) is None
    condition = next(s for s in specs(p) if s.claim_id == 'attribute:comment:condition')
    assert condition.required and '2 типоразмера' in condition.expected_text
    stored = {'checks': [dict(claim_id=condition.claim_id, state='contradiction', required=True)]}
    assert normalize_matching(p, stored) == stored
    item = dict(topic='Conflict', state='possible_mismatch', required=True,
        claim_ids=[condition.claim_id], project_source_ids=['p'], normative_source_ids=['n'],
        explanation='Work class3 differs from comment class2')
    review = {'items': [deepcopy(item)]}
    apply_review_migration(p, review)
    assert review['items'] == [item]
    assert migrated_matching(stored, review, position_key(p)) == stored


def test_typed_brand_migration_preserves_prior_conflicts_and_adds_unproved_function():
    p = position()
    old = dict(claim_id='attribute:comment:condition', state='source_conflict', required=True)
    checks = normalize_matching(p, {'checks': [old]})['checks']
    assert checks[0] == old
    assert checks[1]['claim_id'] == 'method:comment:equipment:1'
    assert checks[1]['required'] and checks[1]['state'] == 'not_found'
    item = dict(topic='Conflict', state='possible_mismatch', required=True,
        claim_ids=['attribute:comment:condition'], project_source_ids=['p'], normative_source_ids=['n'])
    review = {'items': [deepcopy(item)]}
    apply_review_migration(p, review)
    assert review['items'] == [item]


@pytest.mark.parametrize('extra', [[], ['related:75:rate']])
def test_original_possible_mismatch_is_not_retired_after_a_source_downgrade(extra):
    p = position()
    item = CheckedItem(topic='Class conflict', state='possible_mismatch', required=True,
        claim_ids=['attribute:comment:condition', *extra], project_source_ids=[], normative_source_ids=[],
        explanation='The stated class may conflict with the selected equipment.')
    trace = []
    rows = validate_items([item], [], {'attribute:comment:condition', *extra}, position=p,
        evidence_trace=trace)
    assert rows[0]['state'] == 'needs_facts'
    review = dict(items=deepcopy(rows), evidence_trace=trace)
    apply_review_migration(p, review)
    assert review['items'] == rows and review['items'][0]['required']
    # The trace is an exact current-position record, not a prose heuristic.
    assert any(e.get('original_state') == 'possible_mismatch' and e.get('position_key') == position_key(p)
        for e in trace)


def test_actual74_brand_migration_is_pinned_and_cannot_confirm_the_new_class_claim():
    f = fixture()
    p = position()
    review = {'items': deepcopy(f['items'])}
    stored = {'checks': f['checks']}
    untouched = deepcopy(stored)
    apply_review_migration(p, review)
    migrated = migrated_matching(stored, review, position_key(p))
    assert stored == untouched
    checks = {c['claim_id']: c for c in migrated['checks']}
    assert checks['attribute:comment:condition']['required'] is False
    assert checks['method:comment:equipment:1']['state'] == 'not_found'
    assert checks['method:comment:equipment:1']['required']
    assert not any(i['state'] == 'confirmed' and 'method:comment:equipment:1' in i['claim_ids']
        for i in review['items'])
    assert migrated_matching(stored, review, 'wrong-position') == stored
    event = next(e for e in review['evidence_trace'] if e['kind'] == VERSION)
    event['inputs']['comment'] = event['inputs']['comment'].replace('3 типоразмера', '2 типоразмера')
    assert migrated_matching(stored, review, position_key(p)) == stored


def validate(items, *, checks=None, p=None, known=None, replaying=False):
    f = fixture()
    checks = f['checks'] if checks is None else checks
    return validate_items([CheckedItem.model_validate(i) for i in items], f['sources'],
        None if replaying else (plan(checks) if known is None else known),
        matching_checks=checks, position=p or position(), replaying=replaying,
        scope_claims=plan(checks) if replaying else None)


def test_fresh_mixed_presence_keeps_project_only_evidence_but_legacy_state_never_promoted():
    raw = deepcopy(fixture()['items'][0])
    assert raw['state'] == 'needs_facts'
    old = validate([raw], replaying=True)
    assert old[0]['claim_ids'] == ['presence'] and old[0]['state'] == 'needs_facts'
    assert old[0]['required']
    assert old[1]['claim_ids'] == ['attribute:name:unparsed'] and not old[1]['required']
    fresh = deepcopy(raw)
    fresh['state'] = 'confirmed'  # A new model proposal, never a recovered legacy conclusion.
    fresh['explanation'] = 'Project explicitly describes this work.'
    result = validate([fresh])
    assert result[0]['state'] == 'confirmed' and result[0]['claim_ids'] == ['presence']
    assert result[0]['project_source_ids'] == fresh['project_source_ids']
    assert not result[0]['normative_source_ids']
    assert not result[1]['required']


@pytest.mark.parametrize('change', ['required', 'reason', 'conflict', 'missing', 'duplicate', 'local_parser', 'unknown', 'mismatch'])
def test_unreproducible_unknown_or_conflicting_items_are_not_partitioned(change):
    f = fixture()
    p = position()
    item = deepcopy(f['items'][0])
    item['state'] = 'confirmed'
    checks = deepcopy(f['checks'])
    advisory = next(c for c in checks if c['claim_id'] == 'attribute:name:unparsed')
    if change == 'required':
        advisory['required'] = True
    elif change == 'reason':
        advisory['reasons'] = ['invented']
    elif change == 'conflict':
        advisory['state'] = 'contradiction'
    elif change == 'missing':
        checks.remove(advisory)
    elif change == 'duplicate':
        checks.append(deepcopy(advisory))
    elif change == 'local_parser':
        p.work_name = 'Устройство покрытия'
    elif change == 'unknown':
        item['claim_ids'].append('invented:claim')
    elif change == 'mismatch':
        item['state'] = 'possible_mismatch'
    result = validate([item], checks=checks, p=p)
    assert len(result) == 1 and result[0]['required']
    assert result[0]['state'] == 'needs_facts'


def test_required_attribute_and_missing_project_sources_keep_source_gate():
    f = fixture()
    item = deepcopy(f['items'][0])
    item['state'] = 'confirmed'
    item['claim_ids'] = ['presence', 'attribute:comment:condition', 'attribute:name:unparsed']
    result = validate([item])
    assert result[0]['claim_ids'] == ['presence', 'attribute:comment:condition']
    assert result[0]['required'] and result[0]['state'] == 'needs_facts'
    item['claim_ids'] = ['presence', 'attribute:name:unparsed']
    item['project_source_ids'] = []
    assert validate([item])[0]['state'] == 'needs_facts'


def test_feedback_and_final_validation_use_the_same_local_advisory_partition():
    f = fixture()
    item = deepcopy(f['items'][0])
    item['state'] = 'confirmed'
    answer = AgentAnswer(summary='', items=[CheckedItem.model_validate(item)])
    original = answer.model_dump()
    feedback = answer_feedback(answer, f['sources'], SimpleNamespace(parameter_evidence={}),
        position=position(), matching_checks=f['checks'], known_claims=plan(f['checks']))
    assert feedback == [] and answer.model_dump() == original
    assert validate([item])[0]['state'] == 'confirmed'
