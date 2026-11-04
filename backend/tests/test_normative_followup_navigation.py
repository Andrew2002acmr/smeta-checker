"""Exercise the real agent loop with a bounded synthetic source read."""
from copy import deepcopy
from unittest.mock import Mock

from normative_agent import EvidenceRequest, run
from normative_agent_planning import navigation
from normative_position_check import AgentAnswer
from tests.test_normative_agent import toolkit


def test_initial_navigation_still_requires_project_construction_and_table_totals():
    prompt = navigation({}, 'PROOF-CONTRACT', 'TOOL-CONTRACT')
    assert prompt.startswith('TOOL-CONTRACT')
    assert 'Приоритет: проектная конструкция' in prompt
    assert 'row_ids' in prompt and 'column_ids' in prompt


def test_prepared_project_literals_can_finish_without_a_mandatory_extra_model_round():
    tools = toolkit()
    inputs = dict(shared_project_facts=[dict(proposal=dict(parameter='quantity', source_id='saved',
        value='12', unit='м2'), engineer_scope_for_current_position='unresolved')])
    ask = Mock(return_value=AgentAnswer(summary='Недостаточно основания применимости', items=[]))
    verify = Mock(return_value=[])
    tools.execute = Mock(side_effect=AssertionError('No redundant acquisition'))
    result = run(ask, None, None, 'ALL-PROOF-GUARDS', inputs, AgentAnswer, None,
        {'queries': []}, tools, verify_answer=verify)
    assert ask.call_count == verify.call_count == 1
    assert result.items == []  # Saved literals did not manufacture a verdict.
    assert 'ALL-PROOF-GUARDS' in ask.call_args.args[2]
    assert 'не перенесены' in ask.call_args.args[2]
    assert ask.call_args.args[3]['shared_project_facts'][0]['engineer_scope_for_current_position'] == 'unresolved'


def test_followup_keeps_proof_contract_without_unconditional_table_priority():
    inputs = dict(evidence_followup=dict(actions=[dict(key='claim:method', family='work_method')]))
    old = deepcopy(inputs)
    prompt = navigation(inputs, 'PROOF-CONTRACT', 'TOOL-CONTRACT')
    assert prompt.startswith('PROOF-CONTRACTTOOL-CONTRACT')
    assert 'evidence_followup.actions' in prompt and 'Повторный OCR запрещён' in prompt
    assert 'Приоритет: проектная конструкция' not in prompt
    assert inputs == old


def test_real_followup_loop_reads_only_requested_norm_and_still_validates_final_answer():
    tools = toolkit()
    tools.execute = Mock(return_value=dict(text='Normative condition', source_ids=[]))
    ask = Mock(side_effect=[AgentAnswer(summary='Читаю условие', items=[],
        requests=[EvidenceRequest(tool='read_source', source_id='norm-condition')]),
        AgentAnswer(summary='Условие требует уточнения', items=[])])
    verify = Mock(return_value=[])
    inputs = dict(evidence_followup=dict(actions=[dict(key='claim:normative:applicability', family='norm_selection')]))
    result = run(ask, None, None, 'PROOF-CONTRACT', inputs, AgentAnswer, None,
        {'queries': []}, tools, verify_answer=verify)
    assert result.summary == 'Условие требует уточнения'
    assert ask.call_count == 2 and verify.call_count == 1
    assert tools.execute.call_count == 1
    assert tools.execute.call_args.args[0].tool == 'read_source'
    first = ask.call_args_list[0].args[2]
    assert 'PROOF-CONTRACT' in first and 'evidence_followup.actions' in first
    assert 'Приоритет: проектная конструкция' not in first
