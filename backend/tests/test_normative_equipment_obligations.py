"""Machine names are advisory; technical conditions still need evidence."""
from copy import deepcopy
from typing import Literal

import pytest

from audit_models import VorPosition
from normative_advisory_scope import partition
from normative_claim_migration import apply_review_migration, normalize_matching
from normative_position_check import validate_items
from workspace_models import NormativePositionCheckItem

NAME = 'attribute:comment:equipment_names'
FUNCTION = 'method:comment:equipment:1'
CONDITION = 'attribute:comment:condition'


def position(machine='Hamm HD130', tail=''):
    return VorPosition(index=33, work_name='Устройство основания бульдозером мощностью 79 кВт',
        volume='2843', unit='м2', position_type='Работа', comment=
        'Устройство основания производится бульдозером мощностью 79 кВт\n'
        f'Уплотнение основания производится тандемным виброкатком {machine}{tail}.')


def frozen(state='review_required'):
    return {'checks': [dict(claim_id=key, kind='attribute', state=state, required=True, facts=[])
        for key in (NAME, CONDITION)]}


def item(claims, state: Literal['confirmed', 'needs_facts', 'possible_mismatch']='needs_facts'):
    return NormativePositionCheckItem(topic='Машина', claim_ids=claims, state=state,
        required=True, explanation='Проверка технических требований.',
        project_source_ids=[], normative_source_ids=[])


@pytest.mark.parametrize('machine', ['Hamm HD130', 'Volvo DD110', 'BOMAG BW161AD'])
def test_named_machine_is_advisory_but_power_and_function_still_required(machine):
    old = frozen()
    before = deepcopy(old)
    normalized = normalize_matching(position(machine), old)
    assert old == before
    checks = {c['claim_id']: c for c in normalized['checks']}
    assert checks[NAME]['required'] is False
    assert checks[NAME]['state'] == 'not_applicable'
    assert checks[CONDITION] == old['checks'][1]
    assert checks[FUNCTION]['required'] and checks[FUNCTION]['state'] == 'not_found'


@pytest.mark.parametrize('tail', [' массой 13 т', ' только этой модели', ' шириной 2 м',
    ' при температуре 20 градусов', ' выполнять проходы дважды'])
def test_constraints_and_unknown_qualifiers_are_not_retired(tail):
    old = frozen()
    normalized = normalize_matching(position(tail=tail), old)
    assert normalized['checks'][0] == old['checks'][0]


def test_split_preserves_function_obligation_and_never_confirms_it():
    p = position()
    checks = normalize_matching(p, frozen())['checks']
    original = item([NAME, FUNCTION])
    parts = partition(original, p, checks, [NAME, FUNCTION, CONDITION])
    assert [(part.claim_ids, advisory) for part, advisory in parts] == [([FUNCTION], False), ([NAME], True)]
    assert all(part.state == 'needs_facts' for part, _ in parts)
    rows = validate_items([original], [], [NAME, FUNCTION, CONDITION], matching_checks=checks, position=p)
    by_claim = {row['claim_ids'][0]: row for row in rows}
    assert by_claim[NAME]['required'] is False
    assert by_claim[FUNCTION]['required'] and by_claim[FUNCTION]['state'] == 'needs_facts'
    assert original.claim_ids == [NAME, FUNCTION] and original.required


def test_mixed_norm_applicability_cannot_use_advisory_to_bypass_sources():
    p = position()
    checks = normalize_matching(p, frozen())['checks']
    rows = validate_items([item([NAME, 'normative:applicability'], 'confirmed')], [],
        [NAME, 'normative:applicability'], matching_checks=checks, position=p)
    required = next(row for row in rows if row['claim_ids'] == ['normative:applicability'])
    assert required['required'] and required['state'] == 'needs_facts'


@pytest.mark.parametrize('state', ['contradiction', 'source_conflict'])
def test_matching_conflicts_are_preserved(state):
    old = frozen(state)
    assert normalize_matching(position(), old)['checks'][:2] == old['checks']


def test_negative_model_conclusion_remains_required_after_source_downgrade():
    p = position()
    trace = []
    rows = validate_items([item([NAME], 'possible_mismatch')], [], [NAME, FUNCTION, CONDITION],
        evidence_trace=trace, matching_checks=normalize_matching(p, frozen())['checks'], position=p)
    assert rows[0]['state'] == 'needs_facts' and rows[0]['required']
    review = dict(items=rows, evidence_trace=trace)
    apply_review_migration(p, review)
    assert review['items'] == rows
    assert review['items'][0]['required']


def test_nonnegative_migration_is_advisory_without_confirming_function():
    review = dict(items=[item([NAME, FUNCTION]).model_dump()])
    apply_review_migration(position(), review)
    assert all(row['state'] == 'needs_facts' for row in review['items'])
    by_claim = {row['claim_ids'][0]: row for row in review['items']}
    assert by_claim[FUNCTION]['required'] and not by_claim[NAME]['required']
