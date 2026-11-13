from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from audit_llm import chat_generation_options
from normative_context import ModelOutputLimitError, Plan, _ask


class Trace:
    @contextmanager
    def stage(self, *args, **kwargs):
        yield SimpleNamespace(update=lambda **_: None)


def invoke(text, finish='stop'):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=text))], usage=SimpleNamespace(prompt_tokens=100,
        completion_tokens=8192, total_tokens=8292,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7000)))
    create = Mock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    credentials = SimpleNamespace(base_url='https://routerai.ru/api/v1', model_name='deepseek/deepseek-v4.1-flash-1017')
    stats = dict(requests=0, usage={}, warnings=[])
    return client, credentials, stats, create


def test_routerai_deepseek_preserves_reasoning_and_reserves_larger_completion_budget():
    client, credentials, stats, create = invoke('{"interpretation":"test","queries":["test"]}')
    result = _ask(client, credentials, '', {}, Plan, Trace(), 'test', stats)
    assert result.queries == ['test']
    assert create.call_args.kwargs['max_tokens'] == 8192
    assert create.call_args.kwargs['extra_body']['reasoning']['effort'] == 'low'
    assert 'reasoning_effort' not in create.call_args.kwargs
    assert stats['usage']['reasoning'] == 7000


@pytest.mark.parametrize('text', ['', '{"interpretation":"truncated","queries":["test"]}'])
def test_truncated_answer_is_technical_failure_even_if_partial_json_parses(text):
    client, credentials, stats, create = invoke(text, 'length')
    with pytest.raises(ModelOutputLimitError, match='8192'):
        _ask(client, credentials, '', {}, Plan, Trace(), 'test', stats)
    assert create.call_count == 1  # Never silently repeat a paid request.
    assert stats['usage']['reasoning'] == 7000
    assert '7000' in stats['warnings'][0]


def test_reasoning_profile_does_not_apply_deepseek_parameters_to_other_models():
    assert chat_generation_options('https://routerai.ru/api/v1', 'other/model') == {}
    assert chat_generation_options('https://anymodel.org/v1', 'ds/deepseek-v4-flash')['reasoning_effort'] == 'none'


def test_source_navigation_does_not_spend_final_validation_reasoning_budget():
    client, credentials, stats, create = invoke('{"interpretation":"read table","queries":["test"]}')
    _ask(client, credentials, '', {}, Plan, Trace(), 'normative-evidence-plan', stats)
    assert create.call_args.kwargs['extra_body']['reasoning']['effort']=='none'


def test_evidence_validation_returns_json_without_unbounded_hidden_reasoning():
    client, credentials, stats, create = invoke('{"interpretation":"check","queries":["test"]}')
    _ask(client, credentials, '', {}, Plan, Trace(), 'normative-evidence-1', stats)
    assert create.call_args.kwargs['extra_body']['reasoning']['effort']=='none'


def test_pinned_decart_model_is_passed_verbatim_without_duplicate_provider_routing():
    client, credentials, stats, create = invoke('{"interpretation":"check","queries":["test"]}')
    credentials.model_name='deepseek/deepseek-v4.1-flash-1017@provider=decart&allow_fallbacks=false'
    _ask(client, credentials, '', {}, Plan, Trace(), 'normative-evidence-1', stats)
    kwargs=create.call_args.kwargs
    assert kwargs['model']==credentials.model_name
    assert kwargs['extra_body']=={'reasoning':{'effort':'none'}}
    assert 'provider' not in kwargs
