"""The measured request must be the exact lossless JSON sent to either API."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from normative_context import Plan, _ask
from normative_context_packing import serialize_context
from test_normative_provider_failures import fixture


def request(data, *, responses=False):
    answer = '{"interpretation":"Проверено","queries":["ГЭСН"]}'
    completion = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=answer),
    )])
    client, credentials, stats, trace = fixture(completion)
    if responses:
        credentials.base_url = 'https://ai.api.cloud.yandex.net/v1'
        credentials.folder = 'synthetic-folder'
        client.responses = SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text=answer)))
    result = _ask(client, credentials, '', data, Plan, trace, 'normative-evidence-plan', stats)
    if responses:
        sent = client.responses.create.call_args.kwargs['input']
        client.chat.completions.create.assert_not_called()
    else:
        sent = client.chat.completions.create.call_args.kwargs['messages'][1]['content']
    assert result.interpretation == 'Проверено'
    assert stats['requests'] == 1
    return sent


@pytest.mark.parametrize('responses', [False, True])
def test_actual_api_content_matches_measurement_and_preserves_every_literal(responses):
    quote = '  ЩПС  h=24 см\nПри толщине >18 см — два слоя; ширина ≤6 м.\t  '
    data = dict(
        sources=[dict(id='n2', text=quote), dict(id='p50', text='12 + 12 = 24')],
        required_validation_claims=['quantity', 'normative:applicability'],
        flags=[False, True, None],
        values=[0, 1.26, '133.35840000000002'],
        path='D:\\документы\\ПД.pdf',
    )
    before = deepcopy(data)
    measured = serialize_context(data)
    sent = request(data, responses=responses)
    assert sent == measured
    assert len(sent.encode('utf-8')) == len(measured.encode('utf-8'))
    assert json.loads(sent) == before == data
    assert json.loads(sent)['sources'][0]['text'] == quote


def test_budget_counts_sent_json_instead_of_removable_formatting_whitespace():
    data = dict(rows=[dict(id=str(index), quote='text') for index in range(1900)])
    assert len(json.dumps(data, ensure_ascii=False)) > 60000
    assert len(serialize_context(data)) < 60000
    assert json.loads(request(data)) == data


def test_actual_content_over_budget_is_rejected_before_any_provider_call():
    client, credentials, stats, trace = fixture(None)
    data = dict(text='Я' * 60000)
    assert len(serialize_context(data)) > 60000
    with pytest.raises(ValueError, match='Context input exceeds character budget'):
        _ask(client, credentials, '', data, Plan, trace, 'normative-evidence-plan', stats)
    client.chat.completions.create.assert_not_called()
    assert stats['requests'] == 0
