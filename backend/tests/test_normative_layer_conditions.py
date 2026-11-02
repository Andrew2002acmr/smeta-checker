from copy import deepcopy
from types import SimpleNamespace
from normative_layer_conditions import layer_condition_proof


def fixture():
    p=SimpleNamespace(work_name='Нижний слой основания из ЩПС',formula='',position_type='Работа',
        comment='При толщине однослойного основания более 15 см его нормировать в два слоя')
    group=dict(height='26',first='13',second='13')
    scope='Нижний слой основания из ЩПС h=26 см'
    result=dict(parameter_cards=[dict(id='thickness',state='confirmed',found_value='26',found_unit='см',source_ids=['p'])],
        parameter_proposals=[dict(parameter='thickness',scope_state='verified',source_id='p',
            value='26',unit='см',quote='h=26 см',scope_quote=scope)],
        items=[dict(state='confirmed',claim_ids=['normative:applicability'],project_source_ids=['p'],normative_source_ids=['n'])],
        sources=[dict(id='n',kind='normative',text='При толщине нижнего слоя основания более 15 см его нормировать в два слоя.'),
            dict(id='p',kind='project',origin='native',text=scope,version_id='pd-pin')])
    return p,result,group


def test_condition_uses_cited_threshold_not_a_hardcoded_collection_or_value():
    p,result,group=fixture()
    proof=layer_condition_proof(p,result,group)
    assert proof and set(proof['claim_ids'])=={'basis:comment:consumption:2', 'geometry:comment:thickness:1'}
    assert '15 см' in proof['explanation'] and '13 + 13 = 26' in proof['explanation']
    assert proof['project_source_ids']==['p'] and proof['normative_source_ids']==['n']


def test_missing_project_or_normative_confirmation_cannot_close_the_rule():
    p,result,group=fixture()
    unknown=deepcopy(result)
    unknown['parameter_cards'][0]['state']='needs_facts'
    assert layer_condition_proof(p,unknown,group) is None
    unknown=deepcopy(result)
    unknown['items'][0]['state']='needs_facts'
    assert layer_condition_proof(p,unknown,group) is None
    result['items'][0]['project_source_ids']=[]
    assert layer_condition_proof(p,result,group) is None


def test_wrong_layer_unmet_threshold_or_wrong_sum_cannot_close_the_rule():
    p,result,group=fixture()
    result['sources'][0]['text']='При толщине верхнего слоя основания более 15 см его нормировать в два слоя.'
    assert layer_condition_proof(p,result,group) is None
    result['sources'][0]['text']='При толщине нижнего слоя основания более 30 см его нормировать в два слоя.'
    assert layer_condition_proof(p,result,group) is None
    result['sources'][0]['text']='При толщине нижнего слоя основания более 15 см его нормировать в два слоя.'
    result['parameter_cards'][0]['found_value']='20'
    assert layer_condition_proof(p,result,group) is None


def test_different_comment_threshold_or_operator_is_not_silently_confirmed():
    p,result,group=fixture()
    result['sources'][0]['text']='При толщине нижнего слоя основания более 18 см его нормировать в два слоя.'
    assert layer_condition_proof(p,result,group) is None
    result['sources'][0]['text']='При толщине нижнего слоя основания не менее 15 см его нормировать в два слоя.'
    assert layer_condition_proof(p,result,group) is None


def test_threshold_matching_uses_physical_units():
    p,result,group=fixture()
    result['sources'][0]['text']='При толщине нижнего слоя основания более 0,15 м его нормировать в два слоя.'
    proof=layer_condition_proof(p,result,group)
    assert proof and 'geometry:comment:thickness:1' in proof['claim_ids']


def test_mixed_child_rate_does_not_reopen_an_independently_proved_work_condition():
    from normative_position_check import CheckedItem, validate_items
    from normative_verdict import publish
    p,result,group=fixture()
    mixed=CheckedItem(topic='Слои и расход дочерней смеси',state='needs_facts',
        explanation='Для дочернего материала требуется основание расхода.',
        claim_ids=['basis:comment:consumption:2','related:1:rate'],
        project_source_ids=['p'],normative_source_ids=['n'])
    separated=validate_items([mixed],result['sources'])
    assert len(separated)==2
    assert all(i['state']=='needs_facts' for i in separated)
    assert next(i for i in separated if i['required'])['claim_ids']==['basis:comment:consumption:2']
    assert next(i for i in separated if not i['required'])['claim_ids']==['related:1:rate']
    proof=layer_condition_proof(p,result,group)
    assert proof
    result.update(pipeline_version=4,position_key='key',state='needs_facts',summary='')
    result['items'] += separated
    stored=dict(checks=[dict(claim_id='basis:comment:consumption:2',state='needs_facts',required=True)])
    assert publish({'Статус аудита':'ОК'},stored,'key',{'review':result},enabled=True)['Статус аудита']!='ОК'
    result['items'].append(proof)
    assert publish({'Статус аудита':'ОК'},stored,'key',{'review':result},enabled=True)['Статус аудита']=='ОК'
    assert mixed.state=='needs_facts' and len(mixed.claim_ids)==2


def test_mixed_positive_or_negative_verdict_is_not_copied_to_another_claim_owner():
    from normative_position_check import CheckedItem, validate_items
    sources=[dict(id='p',kind='project'),dict(id='n',kind='normative')]
    for state in ('confirmed','possible_mismatch'):
        mixed=CheckedItem(topic='Смешанный вывод',state=state,explanation='Общий вывод без разделения оснований.',
            claim_ids=['normative:applicability','related:1:rate'],project_source_ids=['p'],normative_source_ids=['n'])
        split=validate_items([mixed],sources)
        assert len(split)==2 and all(item['state']=='needs_facts' for item in split)
        assert [item['required'] for item in split]==[True,False]
