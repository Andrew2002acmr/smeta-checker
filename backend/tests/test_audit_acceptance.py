from copy import deepcopy

import pytest

from acceptance_benchmark import evaluate


def sample():
    export = dict(rows=[dict(index=26, review_id='original', payload=dict(position_check=dict(
        state='needs_facts', parameter_cards=[dict(id='quantity', state='confirmed', found_value='8920.970')],
        items=[dict(state='needs_facts', claim_ids=['normative:width'])])))])
    spec = dict(cases=[dict(id='quantity', index=26, expectations=[
        dict(kind='card', id='quantity', state='confirmed', value='8920.97'),
        dict(kind='state', expected='needs_facts')])])
    return export, spec


def test_partial_confirmation_does_not_require_whole_position_green_or_change_evidence():
    export, spec = sample()
    before = deepcopy(export)
    assert evaluate(export, spec)['acceptance_passed']
    assert export == before


def test_missing_position_is_not_counted_as_pass():
    export, spec = sample()
    export['rows'] = []
    result = evaluate(export, spec)
    assert not result['acceptance_passed']
    assert result['counts'] == {'missing': 1}


def test_green_whole_position_does_not_hide_false_confirmed_aspect():
    export, spec = sample()
    answer = export['rows'][0]['payload']['position_check']
    answer['state'] = 'confirmed'
    answer['items'] = [dict(state='confirmed', claim_ids=['normative:applicability'])]
    spec['cases'][0]['expectations'] = [dict(kind='forbidden_item', claim='normative:applicability', state='confirmed')]
    assert not evaluate(export, spec)['acceptance_passed']


@pytest.mark.parametrize('mutation', ['wrong_value', 'duplicate_card', 'unverified_card'])
def test_matching_status_does_not_hide_wrong_or_ambiguous_numeric_fact(mutation):
    export, spec = sample()
    cards = export['rows'][0]['payload']['position_check']['parameter_cards']
    if mutation == 'wrong_value':
        cards[0]['found_value'] = '106'
    elif mutation == 'duplicate_card':
        cards.append(deepcopy(cards[0]))
    else:
        cards[0]['state'] = 'needs_facts'
    assert not evaluate(export, spec)['acceptance_passed']


def test_duplicate_rows_and_unknown_checks_fail_closed():
    export, spec = sample()
    export['rows'].append(deepcopy(export['rows'][0]))
    with pytest.raises(ValueError, match='Duplicate position'):
        evaluate(export, spec)
    export['rows'].pop()
    spec['cases'][0]['expectations'] = [dict(kind='unsupported')]
    with pytest.raises(ValueError, match='Unknown expectation'):
        evaluate(export, spec)
