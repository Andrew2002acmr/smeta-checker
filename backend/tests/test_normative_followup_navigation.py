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
