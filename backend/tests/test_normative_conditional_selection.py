"""Reproduce the actual fresh lower-layer review's conflicting norm selection."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch

import pytest

from audit_models import VorPosition,PositionSource
from normative_conditional_selection import selection_findings,feedback,TRACE,TOPIC
from normative_parameters import enrich
from normative_verdict import publish


CLAUSE = '''1.27.18. Затраты на устройство оснований и покрытий из гравийной смеси с добавлением щебня, а также
щебеночно-песчаных определяются по табл. 27-04-003, расход материалов принимать по проекту.
При толщине однослойного основания или покрытия более 18 см его нормировать в два слоя по нормам 27-04-
003-06 и 27-04-003-07 с изменением толщины ±1 см для каждого слоя отдельно по норме 27-04-003-08.
При толщине нижнего слоя основания или покрытия более 18 см его нормировать в два слоя по норме 27-04-003-
06 с изменением толщины ±1 см для каждого слоя отдельно по норме 27-04-003-08.
При толщине верхнего слоя основания или покрытия более 18 см его нормировать в два слоя по норме 27-04-003-
07 с изменением толщины ±1 см для каждого слоя отдельно по норме 27-04-003-08.'''


def actual_lower_layer_case():
    work='Устройство нижнего слоя основания из ЩПС, h = 12 см (двухслойного основания - слой1)'
    position=VorPosition(index=0,number='1',work_name=work,volume='431.61',unit='м2',
        comment='Тех.часть ГЭСН 27 п. 1.27.18. При толщине однослойного основания или покрытия более 18 см его нормировать в два слоя',
        position_type='Работа',formula='',position_source=PositionSource(document_sha256='vor',format='xlsx',
            sheet='S',row=18,header_row=1,position_id='0'))
    scope='Нижний слой основания — Щебёночно-песчаная смесь для оснований фр. 0/31,5 по ГОСТ Р 70458-2022, категории K90, марке M1, h=24 см'
    sources=[dict(id='p50',kind='project',origin='native',version_id='pinned-pdf',page=50,text=scope),
        dict(id='clause',kind='normative',origin='extracted_native_unreviewed',source_id='norm-pdf',page=2,
            label='ГЭСН 27 ФСНБ-2022 · 1.27.18',text=CLAUSE),
        dict(id='selected',kind='normative',origin='extracted_native_unreviewed',source_id='norm-pdf',page=24,
            label='ГЭСН 27 ФСНБ-2022 · 27-04-003-02',text='Таблица ГЭСН 27-04-003 Устройство оснований и покрытий из песчано-гравийных или\n'
                'щебеночно-песчаных смесей\nИзмеритель: 1000 м2\n'
                '27-04-003-02 двухслойных, нижний слой толщиной 12 см')]
    cells=[]
    for index,value in enumerate(('345.77','85.841')):
        text='Основание | Ровик уширения | Щебеночно-песчаная смесь фр. 0/31,5 | h=0,24м | Площадь, м2\n'+value
        cells.append(dict(id=f'agent:p89:cell:{index}',kind='project',origin='ocr',version_id='pinned-pdf',
            page=89,text=text,numeric_verified=True,unit_verified=True,unit='м2'))
    equation='345.77 + 85.841 = 431.611 м2'
    sources += cells+[dict(id='sum',kind='project',origin='calculated_from_cells',version_id='pinned-pdf',
        page=89,text='\n'.join([s['text'] for s in cells]+[equation]),numeric_verified=True,unit_verified=True,unit='м2')]
    result=dict(pipeline_version=4,execution_state='complete',sources=sources,summary='Выбрана норма 27-04-003-02.',
        items=[dict(topic='Нормативное основание',state='confirmed',required=True,
            claim_ids=['normative:applicability'],project_source_ids=['p50'],normative_source_ids=['selected','clause'],
            explanation='Норма 27-04-003-02 двухслойных, нижний слой толщиной 12 см соответствует работе.')],
        parameter_proposals=[dict(parameter='quantity',operation='sum',source_id='sum',value='431.611',unit='м2',
            quote=equation,scope_quote=equation,scope_state='verified',operand_source_ids=[c['id'] for c in cells]),
            dict(parameter='thickness',source_id='p50',value='24',unit='см',quote='h=24 см',scope_quote=scope,scope_state='verified'),
            dict(parameter='unit_scale',source_id='selected',value='1000',unit='м2',quote='Измеритель: 1000 м2',
                scope_quote='Измеритель: 1000 м2',scope_state='verified')],
        related_positions=[dict(index=2,row=20,work=work.replace('слой1','слой2'),quantity='431.61',unit='м2')])
    return position,result


def test_fresh_lower_layer_cannot_be_green_with_base_outside_the_cited_applicable_condition():
    position,result=actual_lower_layer_case()
    original=deepcopy(result)
    checked=enrich(position,result,{position.index:position},{})
    assert next(c for c in checked['parameter_cards'] if c['id']=='quantity')['state']=='confirmed'
    assert next(c for c in checked['parameter_cards'] if c['id']=='thickness')['state']=='confirmed'
    applicable=next(i for i in checked['items'] if 'normative:applicability' in i['claim_ids'])
    assert applicable['state']=='needs_facts'
    report=publish({'Статус аудита':'Требуется ручная проверка'},
        dict(checks=[dict(claim_id='normative:applicability',state='not_found',required=True)]),'key',
        {'stage':'done','review':dict(checked,position_key='key')},enabled=True)
    assert report['Статус аудита']=='Требуется ручная проверка'
    assert not any(i['state']=='possible_mismatch' for i in checked['items'])
    assert result==original


def choose(result,code):
    source=next(s for s in result['sources'] if s['id']=='selected')
    source['label']=source['label'].replace('27-04-003-02',code)
    source['text']=source['text'].replace('27-04-003-02',code)


def findings(position,result):
    return selection_findings(position,result,{position.index:position},{})


def test_explicit_lower_condition_wins_over_generic_single_layer_and_keeps_original_quote():
    position,result=actual_lower_layer_case()
    found=findings(position,result)
    assert len(found)==1 and found[0]['allowed_base_codes']==['27-04-003-06']
    assert found[0]['condition_quote'] in CLAUSE
    assert '27-04-003-\n06' in found[0]['condition_quote']
    assert found[0]['selected_base_codes']==['27-04-003-02']
    choose(result,'27-04-003-06')
    assert findings(position,result)==[]  # No unconditional requirement to also select adjustment 08.


def test_rejected_alternative_reference_is_not_a_structured_selection():
    position,result=actual_lower_layer_case()
    alternative=deepcopy(result['sources'][2])
    alternative['id']='rejected'
    result['sources'].append(alternative)
    choose(result,'27-04-003-06')
    result['items'][0]['normative_source_ids'].append('rejected')
    result['items'][0]['explanation']='02 отвергнута; применима06.'
    assert findings(position,result)==[]


def add_adjustment(result,*,text=None,resource_unit=False):
    source=dict(id='adjustment',kind='normative',origin='extracted_native_unreviewed',source_id='norm-pdf',
        label='ГЭСН 27 · 27-04-003-08',text=text or 'Измеритель: 1000 м2\n27-04-003-08 На каждый 1 см изменения толщины слоя '
            'добавлять или исключать к нормам с 27-04-003-01 по 27-04-003-07')
    result['sources'].append(source)
    unit_id='adjustment'
    if resource_unit:
        resources=dict(source,id='adjustment:resources',label=source['label']+' · ресурсы',
            text='Ресурсы нормы 27-04-003-08; Измеритель: 1000 м2')
        result['sources'].append(resources)
        unit_id=resources['id']
    result['parameter_proposals'].append(dict(parameter='unit_scale',source_id=unit_id,value='1000',unit='м2',
        quote='Измеритель: 1000 м2',scope_quote='Измеритель: 1000 м2',scope_state='verified'))
    result['items'][0]['normative_source_ids'].append(unit_id)


@pytest.mark.parametrize('resource_unit',[False,True])
def test_literal_adjustment_with_own_measure_is_not_a_competing_base(resource_unit):
    position,result=actual_lower_layer_case()
    choose(result,'27-04-003-06')
    add_adjustment(result,resource_unit=resource_unit)
    assert findings(position,result)==[]
    # The same adjustment does not rescue a wrong selected base.
    source=next(s for s in result['sources'] if s['id']=='selected')
    source['label']=source['label'].replace('-06','-02')
    issue=findings(position,result)
    assert issue[0]['selected_base_codes']==['27-04-003-02']
    assert issue[0]['reason']=='base_outside_condition'


@pytest.mark.parametrize('change',['no_definition','wrong_reference','other_pdf','unrelated_norm'])
def test_adjustment_exclusion_requires_literal_scope_and_explicit_selected_base_reference(change):
    position,result=actual_lower_layer_case()
    choose(result,'27-04-003-06')
    add_adjustment(result,resource_unit=True)
    source=next(s for s in result['sources'] if s['id']=='adjustment')
    if change=='no_definition':
        result['sources'].remove(source)
    elif change=='wrong_reference':
        source['text']=source['text'].replace('27-04-003-01 по 27-04-003-07','27-04-003-01 по 27-04-003-05')
    elif change=='other_pdf':
        source['source_id']='another-pdf'
    else:
        source['text']='Измеритель: 1000 м2\n27-04-003-08 Самостоятельное устройство основания.'
    assert findings(position,result)[0]['reason']=='base_selection_ambiguous'


def test_single_layer_clause_conjunction_is_not_treated_as_alternatives():
    position,result=actual_lower_layer_case()
    position.work_name='Устройство однослойного основания из ЩПС, h=24 см'
    result['related_positions']=[]
    source=result['sources'][0]
    source['text']=source['text'].replace('Нижний слой основания','Однослойное основание')
    result['parameter_proposals'][1]['scope_quote']=source['text']
    choose(result,'27-04-003-06')
    issue=findings(position,result)
    assert len(issue)==1 and issue[0]['allowed_base_codes']==['27-04-003-06','27-04-003-07']
    assert issue[0]['reason']=='layered_base_selection_unresolved'
    assert 'каждого слоя' in feedback(position,result,{0:position},{})[0]


@pytest.mark.parametrize('change',['no_selection','two_bases','unbound','other_pdf'])
def test_an_applicable_literal_condition_requires_one_bound_structured_base(change):
    position,result=actual_lower_layer_case()
    choose(result,'27-04-003-06')
    if change=='no_selection':
        result['parameter_proposals']=result['parameter_proposals'][:-1]
    elif change=='two_bases':
        other=deepcopy(result['sources'][2])
        other.update(id='other',label='ГЭСН 27 · 27-04-003-02')
        result['sources'].append(other)
        result['parameter_proposals'].append(dict(result['parameter_proposals'][-1],source_id='other'))
        result['items'][0]['normative_source_ids'].append('other')
    elif change=='unbound':
        result['items'][0]['normative_source_ids']=['clause']
    else:
        result['sources'][2]['source_id']='different-pdf'
    issue=findings(position,result)
    assert len(issue)==1 and issue[0]['reason']=='base_selection_ambiguous'


def test_literal_vor_clause_reference_survives_omission_of_clause_from_model_item():
    position,result=actual_lower_layer_case()
    result['items'][0]['normative_source_ids']=['selected']
    assert len(findings(position,result))==1
    position.comment='Другая работа'
    assert findings(position,result)==[]  # A random retrieved clause is not automatically applicable.


@pytest.mark.parametrize('change',['role','material','unverified','ocr','forged_quote','missing_pin'])
def test_missing_independent_project_scope_cannot_activate_a_condition(change):
    position,result=actual_lower_layer_case()
    source=result['sources'][0]
    proposal=result['parameter_proposals'][1]
    if change=='role':
        source['text']=source['text'].replace('Нижний','Верхний')
        proposal['scope_quote']=source['text']
    elif change=='material':
        result['sources'][1]['text']=CLAUSE.replace('щебеночно-песчаных','асфальтобетонных').replace('гравийной смеси с добавлением щебня','асфальтобетона')
    elif change=='unverified':
        proposal['scope_state']='unresolved'
    elif change=='ocr':
        source['origin']='ocr'
    elif change=='forged_quote':
        proposal['quote']='Несуществующая толщина24см'
    else:
        source.pop('version_id')
    assert findings(position,result)==[]


@pytest.mark.parametrize('thickness,unit,trigger', [('18','см',False),('18.01','см',True),('0.24','м',True),('180','мм',False)])
def test_threshold_is_strict_and_converts_literal_units(thickness,unit,trigger):
    position,result=actual_lower_layer_case()
    proposal=result['parameter_proposals'][1]
    result['sources'][0]['text']=result['sources'][0]['text'].replace('h=24 см',f'h={thickness} {unit}')
    proposal.update(value=thickness,unit=unit,quote=f'h={thickness} {unit}',scope_quote=result['sources'][0]['text'])
    # Here the ordinary validator's independently confirmed thickness is the
    # input; this isolates the condition's unit conversion and strict boundary.
    cards=[dict(id='thickness',state='confirmed',found_value=thickness,found_unit=unit,source_ids=['p50'])]
    assert bool(selection_findings(position,result,{0:position},{},cards=cards)) is trigger


def test_codes_and_threshold_are_taken_from_literal_clause_not_project_constants():
    position,result=actual_lower_layer_case()
    for source in result['sources']:
        source['text']=source['text'].replace('27-04-003','38-09-123').replace('более 18 см','более 21 см')
        source['label']=source.get('label','').replace('27-04-003','38-09-123')
    issue=findings(position,result)
    assert issue[0]['allowed_base_codes']==['38-09-123-06']
    assert issue[0]['selected_base_codes']==['38-09-123-02']
    assert '21 см' in issue[0]['condition_quote']


def test_negated_directive_cannot_be_interpreted_as_positive_norm_selection():
    position,result=actual_lower_layer_case()
    result['sources'][1]['text']=CLAUSE.replace('его нормировать','его не нормировать')
    assert findings(position,result)==[]


def test_guard_is_idempotent_and_never_changes_original_sources_or_geometry():
    position,result=actual_lower_layer_case()
    checked=enrich(position,result,{0:position},{})
    repeated=enrich(position,checked,{0:position},{})
    assert repeated==checked
    assert checked['sources']==result['sources']
    assert len([i for i in checked['items'] if i['topic']==TOPIC])==1
    assert len([e for e in checked['evidence_trace'] if e.get('kind')==TRACE])==1


@pytest.mark.parametrize('repair',[True,False])
def test_real_agent_condition_feedback_repairs_only_within_shared_three_round_budget(repair):
    from normative_agent import run,MAX_ROUNDS
    from normative_position_check import AgentAnswer
    position,result=actual_lower_layer_case()
    bad=AgentAnswer(summary='Выбрана02',items=result['items'],parameters=result['parameter_proposals'])
    good=bad.model_copy(deep=True)
    alternate=deepcopy(result['sources'][2])
    alternate.update(id='allowed',label='ГЭСН 27 · 27-04-003-06')
    result['sources'].append(alternate)
    good.parameters[-1].source_id='allowed'
    good.items[0].normative_source_ids=['allowed','clause']
    navigation=AgentAnswer(summary='Read',items=[],parameters=[],requests=[dict(tool='read_page',page=50)])
    ask=Mock(side_effect=[navigation,bad,good if repair else bad])
    tools=SimpleNamespace(sources=result['sources'],preferred=[],coverage={},events=[],table_search_done=True,
        execute=Mock(return_value={'found':True}))
    def verify(answer):
        frozen=dict(result,items=[i.model_dump() for i in answer.items],parameter_proposals=[p.model_dump() for p in answer.parameters])
        return feedback(position,frozen,{0:position},{})
    answer=run(ask,None,None,'',{},AgentAnswer,None,{},tools,verify_answer=verify)
    assert ask.call_count==MAX_ROUNDS==3 and tools.execute.call_count==1
    assert ask.call_args_list[-1].args[3]['remaining_steps']==0
    issues=ask.call_args_list[-1].args[3]['answer_validation']
    assert '27-04-003-06' in issues[0] and 'не доказанная ошибка ВОР' in issues[0]
    checked=enrich(position,dict(result,items=[i.model_dump() for i in answer.items],
        parameter_proposals=[p.model_dump() for p in answer.parameters]),{0:position},{})
    assert any(e.get('kind')==TRACE for e in checked['evidence_trace']) is not repair


def test_frozen_replay_demotes_conflicting_choice_without_retrieval_or_model():
    from test_normative_frozen_evidence import _invoke
    position,result=actual_lower_layer_case()
    row=dict(id='old',kind='check',payload=dict(position_key='key-0',vor_version_id='vor',kb={'release':'pinned'},
        position_check=result,work_relations={}))
    original=deepcopy(row)
    record=dict(id='run',vor_version_id='vor',config={'pdf_versions':{'ПД':'pinned-pdf'}},
        indexed_results=[dict(position_index=0,result=dict(status='ОК'))])
    repository=Mock()
    repository.save_normative_review.side_effect=lambda run,index,kind,payload,actor:dict(id='new',payload=payload)
    checked=_invoke(position,row,record,SimpleNamespace(repository=repository))['payload']['position_check']
    assert checked['state']=='needs_facts'
    assert checked['telemetry']['requests']==0 and any(e.get('kind')==TRACE for e in checked['evidence_trace'])
    assert row==original


def legacy_green(position,result):
    result=deepcopy(result)
    result['items'].append(dict(topic='Геометрия',state='confirmed',required=True,
        claim_ids=['quantity','unit','geometry:thickness'],project_source_ids=['sum','p50'],
        normative_source_ids=[],explanation='Площадь и толщина установлены по ПД.'))
    with patch('normative_conditional_selection.apply_guard',return_value=False):
        return enrich(position,result,{position.index:position},{})


def test_mixed_old_confirmed_item_loses_only_norm_choice_not_independent_geometry():
    position,result=actual_lower_layer_case()
    result['items'][0]['claim_ids']+=['quantity','unit','geometry:thickness']
    result['items'][0]['project_source_ids'].append('sum')
    checked=enrich(position,result,{0:position},{})
    assert all(c['state']=='confirmed' for c in checked['parameter_cards'] if c['id'] in {'quantity','thickness'})
    assert any(i['state']=='needs_facts' and 'normative:applicability' in i['claim_ids'] for i in checked['items'])
    assert any(i['state']=='confirmed' and {'quantity','geometry:thickness'}<=set(i['claim_ids']) for i in checked['items'])
    result['items'][0]['state']='needs_facts'
    uncertain=enrich(position,result,{0:position},{})
    assert not any(i['state']=='confirmed' and 'quantity' in i['claim_ids'] for i in uncertain['items'])


def test_unresolved_mixed_thickness_cannot_hide_a_literal_applicable_condition():
    position,result=actual_lower_layer_case()
    result['items'].append(dict(topic='Толщина и расход материала',state='needs_facts',required=True,
        claim_ids=['geometry:comment:thickness:1','related:1:rate'],project_source_ids=['p50'],
        normative_source_ids=['selected'],explanation='Толщина24см, расходматериала требует проверки.'))
    checked=enrich(position,result,{0:position},{})
    # A pending norm threshold/resource predicate is independent of the actual
    # project thickness. The norm choice must still be restricted below.
    assert next(c for c in checked['parameter_cards'] if c['id']=='thickness')['state']=='confirmed'
    assert next(i for i in checked['items'] if i['topic']=='Нормативное основание')['state']=='needs_facts'
    finding=next(e for e in checked['evidence_trace'] if e.get('kind')==TRACE)
    assert finding['independent_literal_premise'] is False and finding['allowed_base_codes']==['27-04-003-06']
    assert checked['items'][1]['state']=='needs_facts'  # The premise is used only to restrict applicability.


@pytest.mark.parametrize('change',['scope','fraction','two_values','wrong_quote','wrong_role'])
def test_independent_condition_premise_keeps_literal_and_scope_guards(change):
    position,result=actual_lower_layer_case()
    result['items'].append(dict(topic='Толщина',state='needs_facts',required=True,
        claim_ids=['geometry:thickness'],project_source_ids=['p50'],normative_source_ids=[],explanation='Не проверено.'))
    proposal=result['parameter_proposals'][1]
    if change=='scope':
        proposal['scope_state']='unresolved'
    elif change=='fraction':
        position.work_name=position.work_name.replace('ЩПС','ЩПС фр. 0/63')
    elif change=='two_values':
        other=deepcopy(result['sources'][0])
        other.update(id='other',text=other['text'].replace('24 см','23 см'))
        result['sources'].append(other)
        result['parameter_proposals'].append(dict(proposal,source_id='other',value='23',quote='h=23 см',scope_quote=other['text']))
    elif change=='wrong_quote':
        proposal['quote']='h=24 см другое основание'
    else:
        result['sources'][0]['text']=result['sources'][0]['text'].replace('Нижний','Верхний')
        proposal['scope_quote']=result['sources'][0]['text']
    assert findings(position,result)==[]


def test_layer_reuse_revalidates_old_green_before_copying_applicability():
    from test_normative_layer_reuse import fixture,invoke
    _,_,donor,record,context=fixture()
    a,result=actual_lower_layer_case()
    b=a.model_copy(update={'index':2,'number':'3','work_name':a.work_name.replace('слой1','слой2'),
        'position_source':a.position_source.model_copy(update={'row':20,'position_id':'2'})})
    result['related_positions']=[dict(index=0,row=18,work=a.work_name,quantity=a.volume,unit=a.unit)]
    donor['payload']['position_check']=legacy_green(b,result)
    record['config']['pdf_versions']={'ПД':'pinned-pdf'}
    with patch('normative_conditional_selection.apply_guard',return_value=False):
        assert invoke(a,b,record,context) is not None  # Reproduces the previous zero-call green.
    context.repository.save_normative_review.reset_mock()
    original=deepcopy(donor)
    assert invoke(a,b,record,context) is None
    context.repository.save_normative_review.assert_not_called()
    assert donor==original


def test_parent_reuse_keeps_geometry_when_current_condition_disqualifies_old_green():
    from test_normative_parent_geometry import fixture,invoke
    donor,record,result,positions,context=fixture()
    parent,basis=actual_lower_layer_case()
    positions[0]=parent
    donor['payload']['position_check']=legacy_green(parent,basis)
    record['config']['pdf_versions']={'ПД':'pinned-pdf'}
    original=deepcopy(donor)
    output=invoke(donor,record,result,positions,context)
    proposals=[p['parameter'] for p in output['parameter_proposals']]
    assert 'quantity' in proposals and 'thickness' in proposals
    assert 'unit_scale' not in proposals  # Full normative basis was not inherited.
    assert not any(i['state']=='confirmed' and 'derived:parent' in i['claim_ids'] for i in output['items'])
    assert donor==original
