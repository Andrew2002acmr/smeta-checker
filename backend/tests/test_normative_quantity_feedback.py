"""Bad literal extraction gets bounded repair; unknown engineering scope stays unknown."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from normative_agent import EvidenceRequest, run
from normative_position_check import AgentAnswer, answer_feedback
from test_normative_agent import sid, toolkit


def quantity(source_id, value, quote, *, scope_state='unresolved', scope_quote=None, **extra):
    return AgentAnswer(summary='Extracted fact', items=[], parameters=[dict(
        parameter='quantity', source_id=source_id, value=value, unit='м2', quote=quote,
        scope_quote=quote if scope_quote is None else scope_quote, scope_state=scope_state, **extra)])


def prepared_tools():
    tools=toolkit()
    tools.execute(EvidenceRequest(tool='read_table',table_id='t1'))
    tools.table_search_done=True
    return tools


def sum_answer(tools, *, value=None, scope_state='unresolved'):
    result=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=[sid('c1'),sid('c2')],unit='м2'))
    answer=quantity(result['source_id'],value or result['value'],result['equation'],scope_state=scope_state,
        operation='sum',operand_source_ids=result['operands'])
    return answer,result


@pytest.mark.parametrize('scope_state',['verified','unresolved'])
def test_invented_direct_quote_is_reported_without_changing_source_value_or_scope(scope_state):
    tools=prepared_tools()
    candidate=quantity(sid('c1'),'12.34','Площадь 12,34 м2',scope_state=scope_state)
    original=deepcopy(candidate)
    issues=answer_feedback(candidate,tools.sources,tools)
    assert len(issues)==1 and 'число/цитата' in issues[0]
    assert candidate==original and tools.parameter_evidence=={}


def test_literal_source_with_wrong_physical_unit_is_not_retained():
    tools=prepared_tools()
    source=next(source for source in tools.sources if source['id']==sid('c1'))
    candidate=quantity(sid('c1'),'12.34',source['text'])
    candidate.parameters[0].unit='м3'
    issues=answer_feedback(candidate,tools.sources,tools)
    assert len(issues)==1 and 'единица' in issues[0]
    assert tools.parameter_evidence=={}


@pytest.mark.parametrize('scope_state',['verified','unresolved'])
def test_density_unit_in_header_cannot_override_verified_physical_column_unit(scope_state):
    tools=prepared_tools()
    source=next(source for source in tools.sources if source['id']==sid('c1'))
    source['text']='Материал; плотность 2,4 т/м3\nЕдиница физического столбца: м2\n12,34'
    source.update(unit_verified=True,unit='м2')
    candidate=quantity(sid('c1'),'12.34',source['text'],scope_state=scope_state)
    candidate.parameters[0].unit='м3'
    original=deepcopy(candidate)
    issues=answer_feedback(candidate,tools.sources,tools)
    assert len(issues)==1 and 'единицей физического столбца' in issues[0]
    assert candidate==original and tools.parameter_evidence=={}


@pytest.mark.parametrize('scope_quote',['Левый съезд 12,34 м2','Площадь, м2'])
def test_nonliteral_or_incomplete_scope_citation_is_reported_but_not_relocated(scope_quote):
    tools=prepared_tools()
    source=next(source for source in tools.sources if source['id']==sid('c1'))
    candidate=quantity(sid('c1'),'12.34',source['text'],scope_quote=scope_quote)
    original=deepcopy(candidate)
    assert 'scope_quote' in answer_feedback(candidate,tools.sources,tools)[0]
    assert candidate==original and tools.parameter_evidence=={}


@pytest.mark.parametrize('scope_quote',['',None])
def test_valid_direct_literal_with_unknown_scope_is_not_promoted_or_needlessly_retried(scope_quote):
    tools=prepared_tools()
    source=next(source for source in tools.sources if source['id']==sid('c1'))
    candidate=quantity(sid('c1'),'12.34',source['text'],scope_quote=scope_quote)
    assert answer_feedback(candidate,tools.sources,tools)==[]
    assert candidate.parameters[0].scope_state=='unresolved'
    assert candidate.items==[] and tools.parameter_evidence=={}


def test_honestly_missing_source_remains_unresolved_without_forcing_fabrication():
    tools=prepared_tools()
    candidate=quantity('','12.34','',scope_quote='')
    assert answer_feedback(candidate,tools.sources,tools)==[]
    assert candidate.parameters[0].scope_state=='unresolved'
    assert tools.parameter_evidence=={}


@pytest.mark.parametrize('scope_state',['verified','unresolved'])
def test_verified_sum_is_canonicalized_from_legitimate_rounding_without_changing_scope(scope_state):
    tools=prepared_tools()
    # Precision is deliberately different from the production example.
    tools.cells[sid('c1')]['cell']['text']='123.456'
    tools.cells[sid('c2')]['cell']['text']='7.891'
    candidate,result=sum_answer(tools,value='131.35',scope_state=scope_state)
    assert result['value']=='131.347'
    issues=answer_feedback(candidate,tools.sources,tools)
    assert issues == []
    assert candidate.parameters[0].value == '131.347'
    assert candidate.parameters[0].scope_state == scope_state
    assert any(e['kind'] == 'verified_sum_rounding' for e in tools.events)

    wrong, _ = sum_answer(tools, value='131.36', scope_state=scope_state)
    assert any('точной сумме' in issue for issue in answer_feedback(wrong, tools.sources, tools))
    assert wrong.parameters[0].value == '131.36'


def test_same_exact_sum_with_unresolved_scope_does_not_confirm_any_claim():
    tools=prepared_tools()
    candidate,_=sum_answer(tools)
    assert answer_feedback(candidate,tools.sources,tools)==[]
    assert candidate.items==[] and candidate.parameters[0].scope_state=='unresolved'
    assert tools.parameter_evidence=={}


def test_sum_source_for_different_operands_is_not_silently_relocated():
    tools=prepared_tools()
    candidate,_=sum_answer(tools,scope_state='verified')
    candidate.parameters[0].source_id=sid('c1')
    original=deepcopy(candidate)
    assert 'source_id' in answer_feedback(candidate,tools.sources,tools)[0]
    assert candidate==original and tools.parameter_evidence=={}


@pytest.mark.parametrize('operands',[[sid('c1'),sid('c1')],['invented',sid('c2')]])
def test_invalid_physical_cells_stay_invalid_and_cannot_generate_retained_proof(operands):
    tools=prepared_tools()
    candidate,_=sum_answer(tools,scope_state='verified')
    candidate.parameters[0].operand_source_ids=operands
    assert 'Сумма:' in answer_feedback(candidate,tools.sources,tools)[0]
    assert tools.parameter_evidence=={}


def test_new_bad_sum_citation_is_reported_after_accurate_arithmetic():
    tools=prepared_tools()
    candidate,_=sum_answer(tools)
    candidate.parameters[0].quote='Итого 69.12 м2'
    assert 'число/цитата' in answer_feedback(candidate,tools.sources,tools)[0]
    assert tools.parameter_evidence=={}


def test_navigation_then_bad_quote_gets_available_third_round_without_scope_promotion():
    tools=prepared_tools()
    source=next(source for source in tools.sources if source['id']==sid('c1'))
    bad=quantity(sid('c1'),'12.34','Площадь 12,34 м2')
    corrected=quantity(sid('c1'),'12.34',source['text'])
    ask=Mock(side_effect=[AgentAnswer(summary='Read selected cells',items=[],
        requests=[EvidenceRequest(tool='read_table',table_id='t1')]),bad,corrected])
    result=run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,
        verify_answer=lambda answer:answer_feedback(answer,tools.sources,tools))
    assert ask.call_count==3
    assert ask.call_args.args[3]['remaining_steps']==0
    assert 'число/цитата' in ask.call_args.args[3]['answer_validation'][0]
    assert result.parameters[0].scope_state=='unresolved' and result.items==[]
    assert tools.parameter_evidence=={}


def test_bad_citation_at_final_budget_is_never_rewritten_or_retained():
    tools=prepared_tools()
    bad=quantity(sid('c1'),'12.34','Площадь 12,34 м2',scope_state='verified')
    ask=Mock(side_effect=[bad.model_copy(deep=True) for _ in range(3)])
    result=run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,
        verify_answer=lambda answer:answer_feedback(answer,tools.sources,tools))
    assert ask.call_count==3 and result.parameters[0].quote==bad.parameters[0].quote
    assert tools.parameter_evidence=={}


def test_conditional_feedback_hook_adds_diagnostics_without_modifying_evidence():
    tools=prepared_tools()
    answer=AgentAnswer(summary='No numeric candidates',items=[])
    hook=Mock(return_value=['Selected norm requires unresolved design condition'])
    assert answer_feedback(answer,tools.sources,tools,conditional_feedback=hook)==hook.return_value
    hook.assert_called_once_with(answer)
    assert tools.parameter_evidence=={}
