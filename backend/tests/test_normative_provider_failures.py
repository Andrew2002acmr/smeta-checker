"""Compatible API error envelopes remain technical failures, never VOR issues."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import httpx
import openai
from openai.types.chat import ChatCompletion

from audit_models import VorPosition
from normative_context import (
    ModelEmptyResponseError, ModelProviderResponseError, ModelOutputLimitError,
    Plan, _ask,
)
from normative_agent import MAX_ROUNDS, run as run_agent
from normative_position_check import AgentAnswer, check
from normative_verdict import publish
from workspace_models import NormativePositionCheckRequest


class Trace:
    trace_id = None

    def __init__(self):
        self.updates = []

    @contextmanager
    def stage(self, *args, **kwargs):
        yield SimpleNamespace(update=lambda **values:self.updates.append(values))

    def finish(self, *args, **kwargs):
        pass

    def url(self):
        return None


def fixture(response):
    create = Mock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
    credentials = SimpleNamespace(base_url='https://routerai.ru/api/v1',
        model_name='test/model', provider='test', api_key='test-key')
    stats = dict(requests=0, usage={}, warnings=[])
    return client, credentials, stats, Trace()


@pytest.mark.parametrize('code,expected', [(503,503), ('502',502), ('api-key-secret',None),
                                         (True,None), (700,None)])
def test_http_200_provider_error_is_typed_and_does_not_expose_body(code, expected):
    response=ChatCompletion.model_construct(choices=None, id=None, model='test/model',
        error={'code':code, 'message':'upstream error contains api-key-secret and private document'},
        usage={'prompt_tokens':10, 'completion_tokens':0, 'total_tokens':10})
    client, credentials, stats, trace=fixture(response)
    with pytest.raises(ModelProviderResponseError) as caught:
        _ask(client, credentials, '', {}, Plan, trace, 'normative-evidence-plan', stats)
    assert caught.value.code==expected
    assert stats['requests']==1 and client.chat.completions.create.call_count==1
    assert stats['usage']['total']==10
    assert stats['responses'][0]['error']==dict(kind='ModelProviderResponseError', code=expected)
    public=json.dumps([stats, trace.updates, str(caught.value)], ensure_ascii=False)
    assert 'api-key-secret' not in public and 'private document' not in public
    assert 'ошибку в теле ответа' in stats['warnings'][0]


@pytest.mark.parametrize('response', [SimpleNamespace(choices=None),
    SimpleNamespace(choices=[]),
    SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=' \n '))])])
def test_empty_chat_response_has_its_own_safe_cause(response):
    client, credentials, stats, trace=fixture(response)
    with pytest.raises(ModelEmptyResponseError, match='пустой ответ'):
        _ask(client, credentials, '', {}, Plan, trace, 'normative-evidence-plan', stats)
    assert stats['responses'][0]['error']['kind']=='ModelEmptyResponseError'
    assert stats['requests']==1 and not stats['usage']


def test_explicit_error_wins_over_apparently_usable_completion():
    response=SimpleNamespace(error={'code':503, 'message':'secret'}, choices=[
        SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"interpretation":"OK","queries":["x"]}'))])
    client, credentials, stats, trace=fixture(response)
    with pytest.raises(ModelProviderResponseError):
        _ask(client, credentials, '', {}, Plan, trace, 'normative-evidence-plan', stats)
    assert 'text' not in stats['responses'][0]


def test_empty_truncated_response_keeps_token_limit_diagnostic():
    response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content=''))])
    client, credentials, stats, trace=fixture(response)
    with pytest.raises(ModelOutputLimitError):
        _ask(client, credentials, '', {}, Plan, trace, 'normative-evidence-plan', stats)
    assert stats['responses'][0]['finish_reason']=='length'


def position_review(*, direct_pair=False):
    response=SimpleNamespace(choices=None, error={'code':503, 'message':'secret upstream 502'})
    client, credentials, _, trace=fixture(response)
    settings=SimpleNamespace(llm_credentials=lambda:credentials, allowed_llm_hosts=('routerai.ru',),
        langfuse_public_key='', langfuse_secret_key='', langfuse_base_url='', langfuse_environment='test',
        telemetry_redact_pdf=True, telemetry_mask_prompts=True)
    repo=Mock()
    repo.save_normative_review.side_effect=lambda run,index,kind,payload,actor:dict(id='saved',payload=payload)
    context=SimpleNamespace(settings=settings, repository=repo, client_factory=lambda **_:client)
    position=VorPosition(index=0, work_name='Основание', volume='20', unit='м2',
                         formula='=10*2', position_type='Работа')
    facts=SimpleNamespace(model_dump=lambda:{})
    record=dict(id='run', vor_version_id='vor', config={},
                indexed_results=[dict(position_index=0,result=dict(status='ОК'))])
    original=deepcopy(record)
    kb=SimpleNamespace(profile='roads', identity={'release':'test'})
    tools=SimpleNamespace(index=None, version=None, events=[], coverage={}, preferred=[], quantity_candidates=lambda *a,**k:[])

    parsed = None
    if direct_pair:
        from test_normative_quantity_sum_repair import setup
        _, tools, parsed, _ = setup()
        tools.coverage = {}
        tools.index = None
        tools.quantity_candidates = lambda *a, **k: []

    def ask_failure(ask, client, credentials, instructions, inputs, schema, trace, stats, tools, **kwargs):
        if parsed is not None:
            return parsed.model_copy(deep=True)
        return ask(client, credentials, instructions, inputs, schema, trace, 'normative-evidence-plan', stats)

    with ExitStack() as stack:
        patches={
            'analysis_replay.replay_normative':dict(return_value=None),
            'analysis_replay.store_normative':dict(side_effect=lambda *args:args[-1]),
            'normative_layer_reuse.reuse_layer_check':dict(return_value=None),
            'normative_service.project_context':dict(return_value=(position,facts,{},[])),
            'normative_service.profile_for':dict(return_value='roads'),
            'normative_service.corpus':dict(return_value=kb),
            'normative_service.position_key':dict(return_value='key'),
            'workspace_service.inputs_for_run':dict(return_value=[position]),
            'normative_preflight.preflight_candidates':dict(return_value=[]),
            'normative_position_check.EvidenceTools':dict(return_value=tools),
            'normative_position_check.project_sources':dict(return_value=tools.sources if direct_pair else []),
            'normative_position_check.TracingRun':dict(return_value=SimpleNamespace(position=lambda **_:trace,close=lambda:None)),
            'normative_position_check.run_agent':dict(side_effect=ask_failure),
            'normative_parameters.enrich':dict(side_effect=(lambda position,result,*_:result) if direct_pair else AssertionError('A technical failure is not a missing quantity')),
            'normative_parent_basis.bind_parent_basis':dict(side_effect=(lambda context,record,result,*args,**kwargs:result) if direct_pair else AssertionError('Do not bind failed response')),
        }
        for target, kwargs in patches.items():
            stack.enter_context(patch(target,**kwargs))
        saved=check(context, record, 0, NormativePositionCheckRequest(), 'test')
    return saved, record, original, client


def test_position_failure_survives_finalization_without_engineering_gap_cards():
    saved, record, original, client = position_review()
    review=saved['payload']['position_check']
    assert review['execution_state']=='technical_failure'
    assert review['decision_origin'] is None
    assert review['state']=='needs_facts' and review['items']==[]
    assert not review.get('parameter_cards')
    assert 'код 503' in review['summary'] and 'secret' not in json.dumps(saved)
    assert 'Нет структурированного подтверждения' not in review['summary']
    assert review['telemetry']['requests']==1 and client.close.call_count==1
    published=publish({'Статус аудита':'ОК','Что не так':''}, record['indexed_results'][0]['result'],
        'key', {'stage':'done','review':dict(review,position_key='key')}, enabled=True)
    assert published['Статус аудита']=='Проверка не выполнена'
    assert published['Что не так']==review['summary']
    assert record==original



def test_live_v5_postprocessing_preserves_the_models_two_direct_readings():
    with patch('normative_quantity_sum_repair.repair_quantity_sum',
            side_effect=AssertionError('V5 cannot choose a total for the model')) as repair:
        saved, _, _, _ = position_review(direct_pair=True)
    review = saved['payload']['position_check']
    assert review['execution_state'] == 'complete', review['summary']
    assert review['decision_origin'] == 'model_with_validated_sources'
    quantities = [p for p in review['parameter_proposals'] if p['parameter'] == 'quantity']
    assert len(quantities) == 2
    assert all(p['operation'] == 'direct' for p in quantities)
    repair.assert_not_called()
    assert not any(e.get('kind') == 'repaired_physical_quantity_pair_v1'
        for e in review['evidence_trace'])

def agent_response(answer=None, *, code=None):
    if code is not None:
        return SimpleNamespace(choices=None, error={'code':code, 'message':'private-upstream-secret'},
            usage={'prompt_tokens':10, 'completion_tokens':0, 'total_tokens':10})
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
        message=SimpleNamespace(content=json.dumps(answer or {'summary':'Done', 'items':[], 'parameters':[]})))],
        usage={'prompt_tokens':20, 'completion_tokens':5, 'total_tokens':25})


def agent_fixture(responses):
    client, credentials, stats, trace = fixture(None)
    client.chat.completions.create.side_effect = responses
    tools = SimpleNamespace(sources=[], preferred=[], coverage={}, events=[], table_search_done=True,
        execute=Mock(return_value={'found':True}))
    def run():
        return run_agent(_ask, client, credentials, '', {}, AgentAnswer, trace, stats, tools)
    return run, client.chat.completions.create, stats, tools


@pytest.mark.parametrize('code', [408,429,500,502,503,504])
def test_agent_retries_transient_error_envelope_within_remaining_steps(code):
    run, create, stats, tools = agent_fixture([agent_response(code=code), agent_response()])
    assert run().summary == 'Done'
    assert create.call_count == stats['requests'] == 2
    assert stats['usage'] == {'input':30, 'output':5, 'total':35}
    assert tools.execute.call_count == 0
    retry_input = json.loads(create.call_args_list[1].kwargs['messages'][1]['content'])
    assert retry_input['remaining_steps'] == MAX_ROUNDS-2
    assert retry_input['answer_validation']
    assert 'private-upstream-secret' not in json.dumps(stats)


@pytest.mark.parametrize('code', [400,401,403,404,422,501,'unknown-secret'])
def test_agent_does_not_retry_permanent_or_unknown_error_envelope(code):
    run, create, stats, _ = agent_fixture([agent_response(code=code), agent_response()])
    with pytest.raises(ModelProviderResponseError):
        run()
    assert create.call_count == stats['requests'] == 1


def test_agent_does_not_retry_empty_response():
    run, create, stats, _ = agent_fixture([SimpleNamespace(choices=[]), agent_response()])
    with pytest.raises(ModelEmptyResponseError):
        run()
    assert create.call_count == stats['requests'] == 1


def test_envelope_and_http_failures_share_three_call_budget():
    http_error = openai.APIStatusError('private-http-secret',
        response=httpx.Response(502, request=httpx.Request('POST','https://example.invalid')), body={})
    run, create, stats, _ = agent_fixture([
        agent_response(code=503), http_error, agent_response(code=502), agent_response()])
    with pytest.raises(ModelProviderResponseError) as caught:
        run()
    assert caught.value.code == 502
    assert create.call_count == stats['requests'] == MAX_ROUNDS == 3
    assert stats['usage'] == {'input':20, 'output':0, 'total':20}
    assert 'private-' not in json.dumps(stats)


def test_retry_keeps_prior_tool_results_without_repeating_acquisition():
    navigation = {'summary':'Read source', 'items':[], 'parameters':[],
        'requests':[{'tool':'read_page','page':7}]}
    run, create, stats, tools = agent_fixture([
        agent_response(navigation), agent_response(code=503), agent_response()])
    assert run().summary == 'Done'
    assert create.call_count == stats['requests'] == MAX_ROUNDS == 3
    assert tools.execute.call_count == 1
    final_input = json.loads(create.call_args_list[2].kwargs['messages'][1]['content'])
    assert final_input['remaining_steps'] == 0
    assert final_input['tool_results'][0]['request']['page'] == 7
    assert final_input['tool_results'][0]['result'] == {'found':True}


def test_failed_planning_calls_do_not_turn_first_success_into_a_final_review():
    navigation={'summary':'Read source','items':[], 'parameters':[],
        'requests':[{'tool':'read_page','page':7}]}
    run,create,stats,tools=agent_fixture([
        agent_response(code=503),agent_response(code=503),agent_response(navigation)])
    with pytest.raises(TimeoutError,match='acquisition budget'):
        run()
    assert stats['requests']==3
    tools.execute.assert_not_called()
    # Navigation remains explicit even though transport attempts exhausted
    # the paid budget; the source read was not claimed as completed.
    for call in create.call_args_list:
        assert 'ТОЛЬКО выбери' in call.kwargs['messages'][0]['content']


def test_too_many_sources_feedback_exposes_only_limit_and_repairs_within_budget():
    navigation = {'summary':'Read source', 'items':[], 'parameters':[],
        'requests':[{'tool':'read_page','page':7}]}
    item = dict(topic='Applicable norm', state='needs_facts', explanation='private-input-text',
        claim_ids=['normative:applicability'], project_source_ids=[],
        normative_source_ids=[f'private-source-{index}' for index in range(9)])
    invalid = dict(summary='Check', items=[item], parameters=[])
    repaired = deepcopy(invalid)
    repaired['items'][0]['normative_source_ids'] = item['normative_source_ids'][:4]
    run, create, stats, tools = agent_fixture([
        agent_response(navigation), agent_response(invalid), agent_response(repaired)])
    result = run()
    assert result.items[0].normative_source_ids == repaired['items'][0]['normative_source_ids']
    assert create.call_count == stats['requests'] == MAX_ROUNDS == 3
    assert tools.execute.call_count == 1
    final_input = json.loads(create.call_args_list[2].kwargs['messages'][1]['content'])
    assert final_input['answer_validation'] == [
        'Ошибка формата: items.0.normative_source_ids too_long (max_length=8)']
    assert final_input['remaining_steps'] == 0
    assert 'private-' not in json.dumps(final_input['answer_validation'])
