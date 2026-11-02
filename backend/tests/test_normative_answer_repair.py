"""Repair evidence acquisition while calls remain; never relax publication gates."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from normative_agent import EvidenceRequest, model_source_aliases, run
from normative_conditional_selection import feedback, repair_candidates
from normative_position_check import AgentAnswer, answer_feedback, position_search_queries, validate_items
from test_normative_agent import toolkit
from test_normative_conditional_selection import actual_lower_layer_case, add_adjustment


def item(claim='method', *, state='confirmed', project=('p',), normative=('n',), explanation='Способ подтверждается.'):
    return dict(topic='Проверка', state=state, claim_ids=[claim], explanation=explanation,
        project_source_ids=list(project), normative_source_ids=list(normative))


def sources():
    return [dict(id='p',kind='project',version_id='pdf',page=50,origin='native',text=
        'Верхний слой основания — асфальтобетон А32От, h=10 см.\n'
        'Средний слой основания — Щебеночно-песчаная смесь фр. 0/63, h=20 см'),
        dict(id='n',kind='normative',source_id='norm-pdf',origin='native',text='Измеритель: 1000 м2')]


def test_generic_audit_instruction_is_not_a_normative_query_but_custom_question_is_kept():
    from analysis_replay import DEFAULT_QUESTION
    position,_=actual_lower_layer_case()
    assert position_search_queries(position,DEFAULT_QUESTION)==[position.comment,position.work_name]
    custom='Проверь норму посева трав 47-01-047-01 и состав газонной смеси'
    assert position_search_queries(position,custom)==[position.comment,position.work_name,custom]
    # Exact comparison only: a user may extend the default instruction with a
    # meaningful request. Neither the question nor the source object is edited.
    extended=DEFAULT_QUESTION+' Проверь 47-01-047-01.'
    assert position_search_queries(position,extended)[-1]==extended


@pytest.mark.parametrize('claim',['quantity','unit','presence','geometry:comment:thickness:1'])
def test_project_only_claims_do_not_need_normative_references(claim):
    answer=AgentAnswer(summary='Проверка',items=[item(claim,normative=())])
    assert answer_feedback(answer,sources(),toolkit())==[]


@pytest.mark.parametrize('claim',['method','normative:applicability','basis:comment:consumption:2'])
@pytest.mark.parametrize('project,normative',[((),('n',)),(('n',),('p',)),(('invented',),('n',))])
def test_missing_or_wrong_kind_references_are_feedback_and_still_block_publication(claim,project,normative):
    answer=AgentAnswer(summary='Проверка',items=[item(claim,project=project,normative=normative)])
    original=deepcopy(answer)
    assert any('правильного типа' in issue for issue in answer_feedback(answer,sources(),toolkit()))
    assert validate_items(answer.items,sources())[0]['state']=='needs_facts'
    assert answer==original


def test_genuinely_unknown_method_does_not_force_another_call_for_missing_refs():
    answer=AgentAnswer(summary='Неизвестно',items=[item(state='needs_facts',project=(),normative=())])
    assert answer_feedback(answer,sources(),toolkit())==[]


def thickness_answer(value='10',scope='Верхний слой основания — асфальтобетон А32От, h=10 см'):
    return AgentAnswer(summary='Толщина',items=[],parameters=[dict(parameter='thickness',source_id='p',
        value=value,unit='см',quote='h='+value+' см',scope_quote=scope,scope_state='verified')])


def test_literal_wrong_layer_is_reported_before_final_card_without_relocating_quote():
    answer=thickness_answer()
    original=deepcopy(answer)
    tools=toolkit()
    issues=answer_feedback(answer,sources(),tools,work_scope='Средний слой основания из ЩПС фр. 0/63 h=10 см',height_cm=20)
    assert any('другому материалу' in issue for issue in issues)
    assert tools.invalid_project_parameter_kinds=={'thickness'}
    assert tools.parameter_evidence=={} and answer==original


def test_full_project_height_survives_technological_split_and_other_layers_on_same_page():
    answer=thickness_answer('20','Средний слой основания — Щебеночно-песчаная смесь фр. 0/63, h=20 см')
    tools=toolkit()
    assert answer_feedback(answer,sources(),tools,
        work_scope='Средний слой основания из ЩПС фр. 0/63 h=10 см',height_cm=20)==[]
    assert tools.parameter_evidence['thickness']['value']=='20'
    assert not tools.invalid_project_parameter_kinds


@pytest.mark.parametrize('claim,expected,explanation',[
    ('method:comment:equipment:1','Уплотнение тандемным виброкатком','Эквивалентность марки Hamm HD130 не доказана.'),
    ('basis:comment:consumption:2','При толщине более 18 см нормировать в два слоя','Коэффициент дочерней строки 1,26 не подтверждён.'),
])
def test_exact_claim_reminder_does_not_promote_unknown_result(claim,expected,explanation):
    answer=AgentAnswer(summary='Проверка',items=[item(claim,state='needs_facts',explanation=explanation)])
    original=deepcopy(answer)
    issues=answer_feedback(answer,sources(),toolkit(),claim_definitions=[dict(claim_id=claim,expected_text=expected)])
    assert len(issues)==1 and expected in issues[0]
    assert answer==original and answer.items[0].state=='needs_facts'


def conditional_case():
    position,result=actual_lower_layer_case()
    base=deepcopy(result['sources'][2])
    base.update(id='allowed',label=base['label'].replace('-02','-06'),
        text=base['text'].replace('-02','-06').replace('12 см','15 см'))
    result['sources'].append(base)
    return position,result


def test_conditional_repair_hints_keep_same_pdf_literal_measure_and_unresolved_applicability():
    position,result=conditional_case()
    original=deepcopy(result)
    hints=repair_candidates(position,result,{0:position},{})
    assert len(hints)==1 and hints[0]['project_fact_confirmed']
    base=hints[0]['allowed_base_candidates'][0]
    assert base['source_id']=='allowed' and base['code']=='27-04-003-06'
    assert base['literal_measure']['quote']=='Измеритель: 1000 м2'
    assert base['literal_measure']['scope_state']=='unresolved' and not hints[0]['scope_verified']
    assert result==original
    sent,_=model_source_aliases(dict(conditional_repair_candidates=hints),result['sources'])
    assert sent['conditional_repair_candidates'][0]['selected_source_ids'][0].startswith('s')
    assert sent['conditional_repair_candidates'][0]['allowed_base_candidates'][0]['source_id'].startswith('s')


@pytest.mark.parametrize('change',['missing_measure','other_pdf','uncertain_definition'])
def test_unusable_allowed_base_never_gets_repair_hint(change):
    position,result=conditional_case()
    base=result['sources'][-1]
    if change=='missing_measure':
        base['text']=base['text'].replace('Измеритель: 1000 м2','')
    elif change=='other_pdf':
        base['source_id']='another-pdf'
    else:
        base['origin']='native_definition_ambiguous'
    assert repair_candidates(position,result,{0:position},{})==[]


@pytest.mark.parametrize('other_family',[False,True])
def test_repair_adjustment_needs_literal_reference_and_same_family(other_family):
    position,result=conditional_case()
    add_adjustment(result)
    if other_family:
        result['sources'][-1]['label']=result['sources'][-1]['label'].replace('ГЭСН','ФЕР')
    hints=repair_candidates(position,result,{0:position},{})
    adjustments=hints[0]['allowed_base_candidates'][0]['adjustments']
    assert ([a['source_id'] for a in adjustments]==['adjustment']) if not other_family else adjustments==[]


def repair_run_case(change='eligible'):
    position,snapshot=conditional_case()
    tools=toolkit()
    tools.sources=deepcopy(snapshot['sources'])
    tools.table_search_done=True
    snapshot['sources']=tools.sources
    good=[deepcopy(p) for p in snapshot['parameter_proposals'] if p['parameter']!='quantity']
    bad=deepcopy(good)
    bad[0].update(source_id='selected',value='12',quote='нижний слой толщиной 12 см',
        scope_quote='27-04-003-02 двухслойных, нижний слой толщиной 12 см')
    answer_items=[dict(snapshot['items'][0])]
    answer_items[0].pop('required')
    invalid=AgentAnswer(summary='Неверный источник толщины',items=answer_items,parameters=bad)
    corrected=AgentAnswer(summary='Толщина ПД исправлена',items=answer_items,parameters=good)
    fixed=deepcopy(corrected)
    fixed.summary='Норма исправлена'
    fixed.parameters[1].source_id='allowed'
    fixed.items[0].normative_source_ids=['allowed','clause']
    if change=='old_condition':
        invalid=deepcopy(corrected)
    elif change=='missing_measure':
        tools.sources[-1]['text']=tools.sources[-1]['text'].replace('Измеритель: 1000 м2','')
    elif change=='unproven_pd':
        tools.sources[0]['origin']='ocr'
    elif change=='unknown_scope':
        corrected.parameters[0].scope_state='unresolved'
    elif change=='still_invalid':
        corrected=deepcopy(invalid)
    def verify(answer):
        def condition(current):
            result={**snapshot,'items':validate_items(current.items,tools.sources),
                'parameter_proposals':[p.model_dump() for p in current.parameters]}
            tools.conditional_repair_candidates=repair_candidates(position,result,{0:position},{})
            return feedback(position,result,{0:position},{})
        return answer_feedback(answer,tools.sources,tools,owner_unit='м2',work_scope=position.work_name,
            height_cm=24,conditional_feedback=condition)
    navigation=AgentAnswer(summary='Чтение',items=[],requests=[EvidenceRequest(tool='read_source',source_id='p50')])
    return tools,verify,[navigation,invalid,corrected,fixed]


def test_new_condition_after_corrected_project_thickness_gets_only_one_source_only_repair():
    tools,verify,answers=repair_run_case()
    ask=Mock(side_effect=answers)
    stats={'queries':[]}
    result=run(ask,None,None,'',{},AgentAnswer,None,stats,tools,verify_answer=verify)
    assert ask.call_count==4 and result.summary=='Норма исправлена'
    supplied=ask.call_args.args[3]
    assert supplied['remaining_steps']==0 and supplied['conditional_repair_candidates']
    assert 'никаких новых поисков' in ask.call_args.args[2].casefold()
    assert len([e for e in tools.events if e.get('kind')=='late_conditional_repair'])==1
    assert len(stats['queries'])==1  # Only the original read_source.


@pytest.mark.parametrize('change',['old_condition','missing_measure','unproven_pd','unknown_scope','still_invalid'])
def test_no_extra_round_without_a_new_proved_condition_and_available_measure(change):
    tools,verify,answers=repair_run_case(change)
    ask=Mock(side_effect=answers)
    run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,verify_answer=verify)
    assert ask.call_count<=3
    assert not any(e.get('kind')=='late_conditional_repair' for e in tools.events)


def test_provider_failure_on_extra_repair_has_no_fifth_call():
    import httpx
    import openai
    tools,verify,answers=repair_run_case()
    error=openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
    ask=Mock(side_effect=[*answers[:3],error])
    with pytest.raises(openai.APIConnectionError):
        run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,verify_answer=verify)
    assert ask.call_count==4
