"""Ambiguous native definition scope is not a normative applicability proof."""
from copy import deepcopy

import pytest

from normative_position_check import CheckedItem,validate_items


@pytest.mark.parametrize('state',['confirmed','possible_mismatch'])
def test_ambiguous_normative_definition_cannot_establish_applicability_or_incompatibility(state):
    sources=[dict(id='p',kind='project',origin='native'),
        dict(id='n',kind='normative',origin='native_definition_ambiguous')]
    item=CheckedItem(topic='Применимость',state=state,claim_ids=['normative:applicability'],
        project_source_ids=['p'],normative_source_ids=['n'],explanation='Выбрана норма.')
    before=deepcopy(sources)
    output=validate_items([item],sources)
    assert output[0]['state']=='needs_facts'
    assert output[0]['normative_source_ids']==['n']
    assert 'неоднозначно' in output[0]['explanation']
    assert sources==before and item.state==state


def test_ambiguity_is_scoped_to_referenced_definition_and_not_literal_project_measurements():
    sources=[dict(id='p',kind='project',origin='native'),
        dict(id='n',kind='normative',origin='native_definition_ambiguous'),
        dict(id='good',kind='normative',origin='native_definition_reviewed')]
    applicability=CheckedItem(topic='Применимость',state='confirmed',claim_ids=['normative:applicability'],
        project_source_ids=['p'],normative_source_ids=['good'],explanation='Другая однозначная норма.')
    unit=CheckedItem(topic='Единица',state='confirmed',claim_ids=['unit'],
        project_source_ids=['p'],normative_source_ids=['n'],explanation='Буквальный измеритель.')
    assert all(i['state']=='confirmed' for i in validate_items([applicability,unit],sources))


def test_numeric_bridge_cannot_restore_ambiguous_norm_applicability():
    from normative_parameters import enrich
    from normative_quantity_bridge import TOPIC
    from test_normative_quantity_bridge import fixture,published
    position,result=fixture()
    result['sources'][1]['origin']='native_definition_ambiguous'
    result['items']=validate_items([CheckedItem(**i) for i in result['items']],result['sources'])
    checked=enrich(position,result,{position.index:position},{})
    assert next(c for c in checked['parameter_cards'] if c['role']=='unit_scale')['state']=='confirmed'
    assert next(i for i in checked['items'] if i['topic']==TOPIC)['claim_ids']==['quantity']
    assert next(i for i in checked['items'] if 'normative:applicability' in i['claim_ids'])['state']=='needs_facts'
    assert published(position,result)[0]['Статус аудита']!='ОК'
