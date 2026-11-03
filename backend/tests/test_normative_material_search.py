from types import SimpleNamespace
from unittest.mock import Mock

from normative_material_search import search


def test_material_identifiers_are_searched_independently_without_quantity_operands():
    kb=Mock()
    kb.search.return_value=[dict(id='k',entity_kind='clause',text='Категория K90, марка М1')]
    position=SimpleNamespace(work_name='Смесь М800, количество 98765',
        comment='ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26')
    context,hits=search(kb,position)
    assert len(context['queries'])==3 and len(hits)==1
    assert context['queries'][0]['status']=='no_literal_in_top_candidates'
    assert all('98765' not in q['query'] and '1,26' not in q['query'] for q in context['queries'])
    assert all(c.kwargs['kind']=='GOST' and c.kwargs['facts']=={} for c in kb.search.call_args_list)
    assert context['requires_applicability_check'] is True
    assert context['queries'][0]['query'].endswith('М800')


def test_equal_property_prefix_is_not_a_literal_match_and_no_standard_is_not_guessed():
    kb=Mock()
    kb.search.return_value=[dict(id='m',entity_kind='clause',text='M8000')]
    context,hits=search(kb,SimpleNamespace(work_name='Смесь М800',comment='ГОСТ 123-2020'))
    assert not hits and not context['queries'][0]['candidate_ids']
    kb.reset_mock()
    context,hits=search(kb,SimpleNamespace(work_name='Смесь М800',comment=''))
    assert not context['queries'] and not hits
    kb.search.assert_not_called()


def test_each_property_keeps_a_candidate_before_extra_matches_are_added():
    kb=Mock()
    kb.search.side_effect=[[dict(id=f'{prop}:{i}',entity_kind='clause',text=prop)
        for i in range(3)] for prop in ('М800','К90','М1')]
    _,hits=search(kb,SimpleNamespace(work_name='М800, 12 м2',comment='ГОСТ 123-2020 К90 М1'))
    assert [h['id'] for h in hits][:3]==['М800:0','К90:0','М1:0']
    assert kb.search.call_count==3
