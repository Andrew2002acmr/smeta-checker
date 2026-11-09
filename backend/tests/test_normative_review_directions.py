"""A directional conclusion must preserve blocking evidence gates."""
import pytest

from audit_models import VorPosition
from normative_review_directions import assess, direction
from audit_core import build_claim_specs


def fixture():
    position = VorPosition(index=0, number='1', work_name='Устройство основания из щебня',
                           volume='50', unit='м2', position_type='Работа')
    source = dict(id='p', kind='project', version_id='v1')
    quantity = dict(id='quantity', state='confirmed', role='project_value', label='Количество',
                    source_ids=['p'], found_unit='м2', found_value='50', reason='Проверенная площадь')
    result = dict(state='needs_facts', execution_state='complete', sources=[source],
                  parameter_cards=[quantity], items=[])
    return position, result


def test_quantity_project_result_is_independent_of_open_normative_condition():
    position, result = fixture()
    audit = assess(position, result, {}, {'actions': []})
    assert audit['project_state'] == 'confirmed'
    assert audit['directions']['normative']['state'] == 'needs_facts'
    assert audit['overall_state'] == 'needs_facts'


@pytest.mark.parametrize('case', ['foreign_scope', 'foreign_material', 'unit', 'thickness',
                                 'bad_rounding', 'parent', 'norm_condition'])
def test_policy_does_not_hide_project_gaps_or_mismatches(case):
    position, result = fixture()
    matching = {}
    if case == 'foreign_scope':
        result['parameter_cards'][0]['state'] = 'needs_facts'
    elif case == 'foreign_material':
        matching['checks'] = [dict(claim_id='attribute:name:material', state='contradiction', required=True)]
    elif case == 'unit':
        result['parameter_cards'][0]['found_unit'] = 'шт'
    elif case == 'thickness':
        result['parameter_cards'].append(dict(result['parameter_cards'][0], id='thickness', state='possible_mismatch'))
    elif case == 'bad_rounding':
        result['parameter_cards'][0]['state'] = 'possible_mismatch'
    elif case == 'parent':
        result['parameter_cards'][0]['found_unit'] = 'м3'
    else:
        result['sources'].append(dict(id='n', kind='normative'))
        result['items'].append(dict(claim_ids=['normative:applicability'], state='possible_mismatch',
            project_source_ids=['p'], normative_source_ids=['n'], explanation='Нарушено обязательное условие'))
    audit = assess(position, result, matching, {'actions': []}, derived=case=='parent')
    if case == 'norm_condition':
        assert audit['directions']['normative']['state'] == 'possible_mismatch'
    else:
        assert audit['project_state'] != 'confirmed'
    assert audit['overall_state'] != 'confirmed'


def test_optional_item_cannot_change_mandatory_project_claim():
    position, result = fixture()
    result['parameter_cards'][0]['state'] = 'needs_facts'
    result['items'].append(dict(claim_ids=['quantity'], state='confirmed', required=False,
                               project_source_ids=['p'], normative_source_ids=[]))
    audit = assess(position, result, {}, {'actions': []})
    assert audit['project_state'] == 'needs_facts'


def test_isolated_equipment_clause_is_normative_but_mixed_note_stays_project():
    from audit_models import ClaimKind, ClaimSpec

    key = 'attribute:comment:separate_notes'
    text = 'Устройство основания производится бульдозером мощностью 79 кВт'
    spec = ClaimSpec(claim_id=key, kind=ClaimKind.ATTRIBUTE, expected_text=text)
    assert direction(key, spec) == 'normative'
    assert direction(key, spec.model_copy(update={'expected_text': text+'; щебень М800'})) == 'project'
    position, _ = fixture()
    assert all(direction(s.claim_id, s)=='project' for s in build_claim_specs(position) if s.claim_id=='presence')
