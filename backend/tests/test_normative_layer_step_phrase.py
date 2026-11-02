"""The literal 'thickness of the pavement layer' must retain its exact step."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from audit_models import VorPosition
from normative_thickness_adjustment import PROPOSAL, apply_guard, findings
from normative_thickness_rules import adjustment_rule, source_definition


FROZEN = json.loads((Path(__file__).parent / 'fixtures' /
    'thickness-layer-step37-frozen.json').read_text(encoding='utf-8'))


def case():
    result = deepcopy(FROZEN['position_check'])
    position = VorPosition.model_validate(FROZEN['position'])
    sources = {source['id']: source for source in result['sources']}
    event = next(event for event in result['evidence_trace'] if event.get('kind') == PROPOSAL)
    return position, result, sources, event


def test_actual37_saved_literal_step_is_valid_without_promoting_saved_items():
    position, result, sources, event = case()
    original = deepcopy(result)
    rule = adjustment_rule(source_definition(sources['n5']), source_definition(sources['n2']))
    assert rule and rule['step'] == Decimal('0.005')
    assert rule['allow_positive'] and rule['allow_negative']
    assert event['signed_steps'] == 2
    assert findings(position, result, {}, {}, cards=result['parameter_cards']) == []
    assert apply_guard(position, result, {}, {}, cards=result['parameter_cards']) is False
    assert result == original
    assert result['state'] == 'needs_facts'  # Replaying a degraded item is a separate operation.


@pytest.mark.parametrize('phrase', ['слоя покрытия', 'слоя\nпокрытия',
    'слоя основания', 'покрытия', 'слоя'])
def test_exact_known_layer_noun_phrase_retains_numeric_step(phrase):
    _, _, sources, _ = case()
    base = source_definition(sources['n2'])
    step = source_definition(sources['n5'])
    step['text'] = step['text'].replace('слоя покрытия на 0,5', phrase + ' на 0,5')
    rule = adjustment_rule(step, base)
    assert rule and rule['step'] == Decimal('0.005')


@pytest.mark.parametrize('phrase', ['слоя покрытия основания', 'слоя смеси',
    'покрытия или основания', 'слоя при укладке покрытия', 'не слоя покрытия'])
def test_arbitrary_ambiguous_or_negative_layer_phrase_is_not_inferred(phrase):
    _, _, sources, _ = case()
    step = source_definition(sources['n5'])
    step['text'] = step['text'].replace('слоя покрытия на 0,5', phrase + ' на 0,5')
    assert adjustment_rule(step, source_definition(sources['n2'])) is None


@pytest.mark.parametrize('change', ['wrong_pdf', 'wrong_family', 'wrong_version',
    'wrong_reference', 'wrong_measure', 'wrong_step', 'wrong_quote', 'conflicting_proposal',
    'negated_direction', 'extra_condition', 'square_unit', 'superscript_unit'])
def test_expanded_phrase_does_not_bypass_existing_binding_guards(change):
    position, result, sources, event = case()
    if change == 'wrong_pdf':
        sources['n5']['source_id'] = 'other-pdf'
    elif change == 'wrong_family':
        sources['n5']['label'] = sources['n5']['label'].replace('ГЭСН', 'ФЕР')
    elif change == 'wrong_version':
        sources['n5']['version_id'] = 'other-edition'
    elif change == 'wrong_reference':
        sources['n5']['text'] = sources['n5']['text'].replace('к норме 27-07-006-01', 'к норме 27-07-006-09')
        event['adjustment_quote'] = event['adjustment_quote'].replace('к норме 27-07-006-01', 'к норме 27-07-006-09')
    elif change == 'wrong_measure':
        sources['n5']['text'] = sources['n5']['text'].replace('Измеритель: 1000 м2', 'Измеритель: 1000 м3')
    elif change == 'wrong_step':
        event['signed_steps'] = 1
    elif change == 'wrong_quote':
        event['base_quote'] = 'толщина слоя 5 см'
    elif change == 'conflicting_proposal':
        result['evidence_trace'].append({**deepcopy(event), 'signed_steps': 3})
    elif change == 'negated_direction':
        sources['n5']['text'] = sources['n5']['text'].replace('добавлять или исключать', 'не добавлять и не исключать')
        event['adjustment_quote'] = event['adjustment_quote'].replace('добавлять или исключать', 'не добавлять и не исключать')
    elif change == 'extra_condition':
        sources['n5']['text'] = sources['n5']['text'].replace('на 0,5 см', 'на 0,5 см при условии согласования')
        event['adjustment_quote'] = event['adjustment_quote'].replace('на 0,5 см', 'на 0,5 см при условии согласования')
    else:
        unit = 'см2' if change == 'square_unit' else 'см²'
        sources['n5']['text'] = sources['n5']['text'].replace('на 0,5 см', 'на 0,5 ' + unit)
        event['adjustment_quote'] = event['adjustment_quote'].replace('на 0,5 см', 'на 0,5 ' + unit)
    original = deepcopy(result)
    found = findings(position, result, {}, {}, cards=result['parameter_cards'])
    assert found and found[0]['reason'] == 'missing_or_invalid_adjustment'
    assert result == original
