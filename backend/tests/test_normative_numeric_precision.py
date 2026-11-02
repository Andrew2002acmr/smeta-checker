from copy import deepcopy
from decimal import Decimal

import pytest

from normative_parameters import build_cards, enrich, same_numeric_result
from normative_verdict import readable_review
from test_normative_parameters import position, proposal, source


@pytest.mark.parametrize('left,right', [
    ('133.35840000000002', '133.3584'),
    ('0.30000000000000004', '0.3'),
    ('0.3333333333333333', '0.3333333333333333333333333333'),
    (Decimal('0.00000000013335840000000002'), Decimal('0.0000000001333584')),
])
def test_binary_float_tails_are_equal_in_both_directions(left, right):
    assert same_numeric_result(left, right)
    assert same_numeric_result(right, left)


@pytest.mark.parametrize('left,right', [
    ('133.3584', '89.99676'),
    ('133.3584', '133.359'),
    ('133.3584', '133.358'),
    ('1000000000000', '1000000000001'),
    ('0', '0.000000000000000001'),
    (None, '0'),
    ('NaN', '0'),
])
def test_real_differences_and_invalid_values_are_not_rounded_away(left, right):
    assert not same_numeric_result(left, right)


def calculation_card(p):
    return next(c for c in build_cards(p, dict(sources=[]), {0: p}, {})
                if c['role'] == 'calculation')


def test_screenshot_formula_is_confirmed_but_stale_cache_and_real_mismatch_are_not():
    p = position(volume='133.35840000000002', unit='м3')
    p.formula = '105.84*1.26'
    assert calculation_card(p)['state'] == 'confirmed'
    p.calculation = dict(expression=p.formula, cached_mismatch=True)
    assert calculation_card(p)['state'] == 'needs_facts'
    p.calculation = dict(expression='357.13*0.2*1.26')
    assert calculation_card(p)['state'] == 'possible_mismatch'
    p.calculation = dict(expression='1/0')
    assert calculation_card(p)['state'] == 'needs_facts'


def test_native_project_quantity_accepts_float_tail_without_changing_source():
    p = position(volume='133.35840000000002', unit='м3')
    result = dict(sources=[source(text='Объем 133,3584 м3')], parameter_proposals=[
        proposal('quantity', '133.3584', 'м3', 'Объем 133,3584 м3')])
    original = deepcopy(result)
    assert build_cards(p, result, {0: p}, {})[0]['state'] == 'confirmed'
    assert result == original


def test_revalidation_removes_only_its_own_obsolete_arithmetic_summary():
    p = position(volume='133.35840000000002', unit='м3')
    p.work_name = 'Объем'
    p.formula = 'D1'
    p.calculation = dict(expression='D1', dependencies=[dict(cell='D1', value='133.3584')])
    result = dict(pipeline_version=3, sources=[source(text='Объем 133,3584 м3')],
                  parameter_proposals=[dict(
                      **proposal('quantity', '133.3584', 'м3', 'Объем 133,3584 м3'),
                      scope_state='verified', scope_quote='Объем 133,3584 м3')], items=[],
                  parameter_cards=[dict(id='calculation:0', role='calculation',
                                        label='Арифметика', state='possible_mismatch',
                                        reason='Результат формулы не совпадает с объёмом ВОР.')],
                  summary='Арифметика: Результат формулы не совпадает с объёмом ВОР.')
    original = deepcopy(result)
    updated = enrich(p, result, {0: p}, {})
    assert 'Результат формулы не совпадает' not in updated['summary']
    assert result == original
    assert enrich(p, updated, {0: p}, {}) == updated
    result['summary'] = 'Независимое замечание к составу материала.'
    assert enrich(p, result, {0: p}, {})['summary'] == result['summary']


def test_read_only_view_corrects_old_worker_card_and_keeps_real_conflict_and_history():
    reason = 'Результат формулы не совпадает с объёмом ВОР.'
    card = dict(id='calculation:32', role='calculation', label='Арифметика',
                declared_value='133.35840000000002', found_value='133.3584',
                state='possible_mismatch', reason=reason)
    raw = dict(state='possible_mismatch', items=[], sources=[],
               parameter_cards=[card], summary='Арифметика: '+reason)
    original = deepcopy(raw)
    shown = readable_review(raw)
    assert shown['parameter_cards'][0]['state'] == 'confirmed'
    assert reason not in shown['summary']
    assert readable_review(shown) == shown
    assert raw == original
    raw['parameter_cards'][0]['found_value'] = '89.99676'
    assert readable_review(raw)['parameter_cards'][0]['state'] == 'possible_mismatch'
    raw['parameter_cards'][0].update(state='needs_facts', found_value='133.3584')
    assert readable_review(raw)['parameter_cards'][0]['state'] == 'needs_facts'
