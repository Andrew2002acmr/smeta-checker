"""Wrong norm measures are corrected during acquisition, not accepted as proof."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from normative_agent import run
from normative_position_check import AgentAnswer, answer_feedback


def answer(source_id='n', value='1000', unit='м2', quote='Измеритель: 1000 м2'):
    return AgentAnswer(summary='Candidate', items=[], parameters=[dict(parameter='unit_scale',
        source_id=source_id, value=value, unit=unit, quote=quote, scope_quote=quote, scope_state='verified')])


def tools(sources):
    return SimpleNamespace(sources=sources, preferred=[], events=[], coverage={}, table_search_done=True)


def test_project_measure_cannot_supply_norm_scale_even_with_literal_quote():
    sources=[dict(id='p', kind='project', text='Измеритель: 1000 м2')]
    candidate=answer('p')
    original=deepcopy(candidate)
    toolkit=tools(sources)
    issues=answer_feedback(candidate,sources,toolkit,owner_unit='м2')
    assert len(issues)==1 and 'нормативный источник' in issues[0]
    assert toolkit.parameter_evidence=={}
    assert candidate==original


def test_resource_quantity_is_not_norm_measure():
    sources=[dict(id='n', kind='normative',
        text='Ресурсы нормы; Измеритель: 1000 м2\nСмесь; т: 221.76')]
    toolkit=tools(sources)
    issues=answer_feedback(answer(value='221.76',unit='т',quote='Смесь; т: 221.76'),
        sources,toolkit,owner_unit='м2')
    assert len(issues)==1 and 'не из количества или единицы ресурса' in issues[0]
    assert toolkit.parameter_evidence=={}


def test_material_mass_measure_is_rejected_when_work_owner_is_area():
    sources=[dict(id='n',kind='normative',text='Измеритель: т')]
    toolkit=tools(sources)
    issues=answer_feedback(answer(value='1',unit='т',quote='Измеритель: т'),
        sources,toolkit,owner_unit='м2')
    assert len(issues)==1 and 'normative_work_owner.unit' in issues[0]
    assert toolkit.parameter_evidence=={}


@pytest.mark.parametrize('value,unit,quote,owner_unit',[
    ('1000','м2','Измеритель: 1000 м2','м2'),
    ('1','т','Измеритель: т','т'),
])
def test_literal_compatible_norm_measure_remains_candidate_not_verdict(value,unit,quote,owner_unit):
    sources=[dict(id='n',kind='normative',text=quote)]
    toolkit=tools(sources)
    candidate=answer(value=value,unit=unit,quote=quote)
    assert answer_feedback(candidate,sources,toolkit,owner_unit=owner_unit)==[]
    assert toolkit.parameter_evidence['unit_scale']['source_id']=='n'
    assert candidate.items==[]  # Mechanical feedback does not confirm applicability.


def test_agent_repairs_project_measure_to_norm_measure_in_existing_round_budget():
    sources=[dict(id='p',kind='project',text='Измеритель: 1000 м2'),
        dict(id='n',kind='normative',text='Измеритель: 1000 м2')]
    toolkit=tools(sources)
    ask=Mock(side_effect=[answer('p'),answer('n')])
    result=run(ask,None,None,'',{'normative_work_owner':{'unit':'м2','is_parent':True}},
        AgentAnswer,None,{'queries':[]},toolkit,
        verify_answer=lambda candidate:answer_feedback(candidate,sources,toolkit,owner_unit='м2'))
    assert result.parameters[0].source_id=='n'
    assert ask.call_count==2
    second_input=ask.call_args.args[3]
    assert second_input['remaining_steps']==1
    assert second_input['normative_work_owner']['unit']=='м2'
    assert 'нормативный источник' in second_input['answer_validation'][0]


def test_invalid_measure_is_not_silently_rewritten_or_confirmed_at_budget_end():
    sources=[dict(id='p',kind='project',text='Измеритель: 1000 м2')]
    toolkit=tools(sources)
    ask=Mock(side_effect=[answer('p'),answer('p'),answer('p')])
    result=run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},toolkit,
        verify_answer=lambda candidate:answer_feedback(candidate,sources,toolkit,owner_unit='м2'))
    assert ask.call_count==3
    assert result.parameters[0].source_id=='p'
    assert result.items==[] and toolkit.parameter_evidence=={}
