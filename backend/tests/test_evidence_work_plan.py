from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import pytest

from normative_evidence_plan import build_work_plan, pending_actions, project_query
from normative_retrieval import DocumentSearch, document_search
from normative_agent import EvidenceRequest
from test_normative_agent import toolkit


def test_all_search_results_accessible_beyond_initial_five_and_large_irrelevant_pdf():
    texts = ['Описание без искомого свойства'] * 3000 + [
        f'Ведомость площади покрытий. Участок {i}. Всего 10,20 м2.' for i in range(17)]
    index = DocumentSearch(texts)
    offset, pages = 0, []
    while offset is not None:
        result = index.search_page('покрытие', tables=True, offset=offset)
        pages.extend(h['page'] for h in result['hits'])
        assert result['absence_proven'] is False
        offset = result['next_offset']
    assert len(pages) == len(set(pages)) == 17
    assert min(pages) > 3000
    assert index.search_page('несуществующий термин')['total_matches'] == 0


def test_search_reuses_index_but_not_after_document_changes():
    original = ['Ведомость площадей. Всего 10 м2.']
    a = document_search(original)
    assert document_search(list(original)) is a
    original[0] = 'Ведомость объемов. Всего 99 м3.'
    assert document_search(original) is not a
    assert a.texts[0].endswith('10 м2.')


def test_repeated_tool_query_does_not_read_pages_again_and_cursor_advances():
    tools = toolkit()
    tools.index.texts = ['Ведомость площадей. Всего 12 м2.'] * 12
    with patch.object(tools, '_page', side_effect=lambda page, offset: dict(page=page)) as read:
        first = tools.execute(EvidenceRequest(tool='search_tables', query='площадь'))
        count = read.call_count
        repeated = tools.execute(EvidenceRequest(tool='search_tables', query='площадь'))
        assert repeated['repeated_query'] and read.call_count == count
        second = tools.execute(EvidenceRequest(tool='search_tables', query='площадь',
            offset=first['search']['next_offset']))
    assert {p['page'] for p in first['pages']}.isdisjoint(p['page'] for p in second['pages'])
    assert len([e for e in tools.events if e.get('kind') == 'project_retrieval_page_v1']) == 2


def test_missing_review_evidence_does_not_become_missing_document():
    position = dict(work_name='Покрытие', unit='м2')
    review = dict(items=[], sources=[], parameter_cards=[dict(id='quantity', state='needs_facts')])
    plan = build_work_plan(position, review, {})
    assert plan['project_search']['state'] == 'not_measured'
    assert not plan['policy']['manual_missing_document_established']
    assert next(a for a in plan['actions'] if a['key'] == 'card:quantity')['action'] == 'retrieve_project'
    assert review == dict(items=[], sources=[], parameter_cards=[dict(id='quantity', state='needs_facts')])


def test_retry_requires_changed_evidence_not_new_model_wording():
    review = dict(items=[], sources=[], parameter_cards=[dict(id='quantity', state='needs_facts', reason='нет')])
    plan = build_work_plan(dict(work_name='Покрытие', unit='м2'), review, {})
    tried = [a['retry_fingerprint'] for a in plan['actions']]
    review['parameter_cards'][0]['reason'] = 'модель повторила другими словами'
    repeated = build_work_plan(dict(work_name='Покрытие', unit='м2'), review, {})
    assert pending_actions(repeated, tried) == []
    review['parameter_proposals'] = [dict(parameter='quantity', source_id='p', value='5', unit='м2')]
    changed = build_work_plan(dict(work_name='Покрытие', unit='м2'), review, {})
    assert [a['key'] for a in pending_actions(changed, tried)] == ['card:quantity']


def test_material_query_uses_identity_and_section_and_ignores_expected_amount():
    position = dict(work_name='Устройство покрытия асфальтоукладчиком', volume='123456',
        formula='123456*0.1', position_source=dict(sections=[dict(text='Остановки')]))
    query = project_query(position, [dict(work='А22Нт')])
    assert 'Остановки' in query and 'А22Нт' in query and '123456' not in query
    other = deepcopy(position)
    other['volume'] = '999999'
    assert project_query(other, [dict(work='А22Нт')]) == query


def test_parent_dependency_and_unbound_items_remain_explicit_obligations():
    review = dict(sources=[], items=[dict(claim_ids=[], state='needs_facts'),
        dict(claim_ids=['derived:parent'], state='needs_facts')])
    plan = build_work_plan(dict(work_name='Смесь', unit='т'), review, {}, derived=True,
        relation=dict(parent=dict(index=45)))
    parent = next(a for a in plan['actions'] if a['key'] == 'claim:derived:parent')
    assert parent['dependency_position'] == 45
    assert any(a['key'].startswith('unbound_item:') for a in plan['actions'])


def test_finalize_plan_supports_old_simple_position_objects():
    from normative_evidence_plan import position_data
    assert position_data(SimpleNamespace(work_name='Текст', unit='т', position_source=None))['unit'] == 'т'


def test_agent_does_not_spend_another_call_on_same_exhausted_search():
    from normative_agent import run
    from normative_position_check import AgentAnswer
    tools = toolkit()
    request = EvidenceRequest(tool='search_tables', query='ведомость')
    tools.execute(request)
    ask = Mock(return_value=AgentAnswer(summary='Повтор', items=[], requests=[request]))
    with pytest.raises(TimeoutError, match='no progress'):
        run(ask, None, None, '', {}, AgentAnswer, None, {'queries': []}, tools)
    assert ask.call_count == 1


def test_agent_automatically_advances_repeated_query_to_unread_candidates():
    from normative_agent import run
    from normative_position_check import AgentAnswer
    tools = toolkit()
    tools.index.texts = ['Ведомость площадей. Всего 12 м2.'] * 7
    request = EvidenceRequest(tool='search_tables', query='площадь')
    with patch.object(tools, '_page', side_effect=lambda page, offset: dict(page=page)):
        tools.execute(request)
        ask = Mock(side_effect=[AgentAnswer(summary='Продолжить', items=[], requests=[request]),
            AgentAnswer(summary='Результат', items=[])])
        run(ask, None, None, '', {}, AgentAnswer, None, {'queries': []}, tools)
    history = [e for e in tools.events if e.get('request')]
    assert history[0]['request']['offset'] == 5
    assert [p['page'] for p in history[0]['result']['pages']] == [6, 7]
