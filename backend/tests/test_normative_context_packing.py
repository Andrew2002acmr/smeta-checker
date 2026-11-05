"""Real captured late-round budgets and adversarial lossless packing boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from normative_agent import MAX_CONTEXT, PIPELINE_VERSION, compact, model_source_aliases, run
from normative_context_packing import pack_sources, serialize_context, source_order
from normative_embedded_context import literal_paths
from normative_position_check import AgentAnswer
from test_normative_agent import toolkit


CAPTURES = json.loads((Path(__file__).parent/'fixtures'/'context-packing-v10-captures.json').read_text())


def size(value, sources=()):
    return len(serialize_context(model_source_aliases(value, sources)[0]))


@pytest.mark.parametrize('case', CAPTURES, ids=lambda case: case['name'])
def test_actual_context_preserves_metadata_and_complete_work_evidence(case):
    original = deepcopy(case)
    sources = case['sources']
    result = compact(case['metadata'], sources, case['preferred'], planning=case['planning'],
        measure=lambda value: size(value, sources))
    assert size(result, sources) <= MAX_CONTEXT
    metadata = deepcopy(result)
    metadata.pop('sources')
    metadata.pop('archived_source_ids')
    assert metadata == {**case['metadata'], 'pipeline_version': PIPELINE_VERSION}
    assert case == original
    lookup = {s['id']: s for s in sources}
    preview = {s['id']: s for s in result['sources']}
    if case['name'].startswith('actual0'):
        essential = ['n2', 'n2:resources:25', 'n8', 'agent:p50:0',
            'agent:p89:cell:feb8114d3c30a3c1318def97',
            'agent:p89:cell:b10b80c2f6781706ffd1f953',
            'agent:sum:ea3f8bc61fa62ef5f0b6f647']
    else:
        essential = [sid for sid, source in lookup.items()
            if any(code in source.get('label', '') for code in ['27-06-031-01', '27-06-032-01'])]
        assert essential
    for sid in essential:
        assert preview[sid]['text'] == lookup[sid]['text'], sid
        assert preview[sid]['excerpt_truncated'] is False
        assert preview[sid]['next_offset'] is None
    embedded = literal_paths(result, sources)
    assert embedded == literal_paths(case['metadata'], sources)
    assert not set(embedded) & set(result['archived_source_ids'])
    # Complete embedded clauses may have no redundant source preview at all.
    for sid in embedded:
        assert json.dumps(lookup[sid]['text'], ensure_ascii=False) in json.dumps(result, ensure_ascii=False)


def test_serialization_preserves_literal_whitespace_and_catalogue_structure():
    rows = [dict(page=89, table_id='table:exact', rows=42, columns=26, total_rows=[41, 37]),
            dict(page=None, table_id='empty', rows=0, columns=None, total_rows=[])]
    original: dict[str, Any] = dict(coverage=dict(available_tables=rows, table_catalog_truncated=False),
        literal='Пункт 7.  Два пробела;\n новая строка\t табуляция. "точная цитата"')
    result: dict[str, Any] = pack_sources(original, [], limit=10000)
    assert json.loads(serialize_context(result)) == result
    assert result['coverage'] == original['coverage']
    assert result['literal'] == original['literal']
    table = result['coverage']['available_tables'][0]
    assert (table['page'], table['table_id'], table['total_rows']) == (89, 'table:exact', [41, 37])
    assert isinstance(original['coverage']['available_tables'][0], dict)


def test_new_search_clause_is_delivered_before_stale_preferred_norms():
    stale=deepcopy(CAPTURES[0]['sources'][0])
    fresh=dict(id='new-clause',kind='normative',source_id='pdf-new',page=7,
        label='Условия прочитанного документа',origin='native',text='Новое условие. '*100)
    value=dict(tool_results=[dict(request=dict(tool='search_norms',query='новое условие'),
        result=dict(results=[dict(source_id='new-clause')]))])
    sources=[stale,fresh]
    order,_=source_order(value,sources,[stale['id'],fresh['id']],{})
    assert order[0]=='new-clause'
    result=pack_sources(value,sources,[stale['id'],fresh['id']],limit=2600)
    sent=next(s for s in result['sources'] if s['id']=='new-clause')
    assert sent['text']==fresh['text'] and not sent['excerpt_truncated']


def test_explicit_expert_unit_context_survives_source_projection():
    from normative_confirmed_units import binding
    source=dict(id='agent:p9:cell:exact',kind='project',page=9,label='ПД',version_id='pinned',
        text='Собственный столбец\n70',origin='ocr',numeric_verified=True,
        unit_verified=False,unit='',rects=[[0,0,1,1]])
    fact=dict(provenance='explicit_user_confirmation',unit='м',actor='user',
        reason='Пользователь проверил страницу.',binding=binding(source))
    source['confirmed_unit_context']=fact
    result=pack_sources({},[source],limit=4000)
    assert result['sources'][0]['confirmed_unit_context']==fact
    assert result['sources'][0]['text']==source['text']


@pytest.mark.parametrize('rows', [None, [], [None], ['bad'], [dict(page=1)],
    [dict(page=1, table_id='x', rows=1, columns=1, total_rows=[], unknown='preserve')]])
def test_every_catalogue_shape_is_unchanged(rows):
    value = dict(coverage=dict(available_tables=rows))
    original = deepcopy(value)
    result = pack_sources(value, [], limit=10000)
    assert result['coverage'] == original['coverage']


def test_adjustment_only_anchor_brings_exact_base_and_resource_first():
    case = CAPTURES[0]
    value = deepcopy(case['metadata'])
    value['previous_literal_parameters'] = [dict(source_id='n8')]
    order, _ = source_order(value, case['sources'], ['n8'], literal_paths(value, case['sources']))
    assert order.index('n2') < order.index('n8')
    assert order.index('n2:resources:25') < order.index('n8')


def test_explicitly_read_clause_displaces_stale_previews_in_a_full_context():
    case = deepcopy(CAPTURES[0])
    late: dict[str, Any] = dict(id='read-clause',kind='normative',origin='native',page=9,label='Condition',
        text='Only for the specified construction. ' * 55)
    sources = [*case['sources'],late]
    value = case['metadata']
    value['tool_results'] = [dict(request=dict(tool='read_source',source_id='read-clause'),
        result=dict(source_id='read-clause',full_length=len(late['text'])))]
    value['protected_metadata'] = 'm' * 18000
    empty = {**value,'sources':[], 'archived_source_ids':[s['id'] for s in sources]}
    limit = len(serialize_context(empty)) + len(late['text']) + 2000
    result = pack_sources(value,sources,case['preferred'],limit=limit)
    delivered = next(s for s in result['sources'] if s['id']=='read-clause')
    assert delivered['text']==late['text'] and not delivered['excerpt_truncated']
    assert 'read-clause' not in result['archived_source_ids']
    assert result['protected_metadata']==value['protected_metadata']
    assert len(serialize_context(result))<=limit


def test_read_offset_prioritizes_returned_fragment_without_inventing_unknown_sources():
    sources=[dict(id='part',kind='normative',text='Exact fragment.',origin='native')]
    value=dict(tool_results=[dict(request=dict(tool='read_source',source_id='original',offset=700),
        result=dict(source_id='part')),dict(request=dict(tool='read_source',source_id='missing'),result=dict(error='unknown_source'))])
    order,_=source_order(value,sources,[],{})
    assert order==['part']


@pytest.mark.parametrize('change', ['wrong_pdf', 'wrong_code', 'prefix_only'])
def test_resource_group_requires_exact_parent_pdf_and_code(change):
    case = CAPTURES[0]
    sources = deepcopy(case['sources'])
    resource = next(s for s in sources if s['id'] == 'n2:resources:25')
    if change == 'wrong_pdf':
        resource['source_id'] = 'another-pdf'
    elif change == 'wrong_code':
        resource['text'] = resource['text'].replace('Ресурсы нормы 27-04-003-06;', 'Ресурсы нормы 27-04-003-07;')
    else:
        resource['id'] = 'n2-other:resources:25'
    _, critical = source_order(case['metadata'], sources, [], {})
    assert resource['id'] not in critical


def test_planning_keeps_short_norm_tail_and_has_honest_offset_for_long_preview():
    sources = [dict(id='short', kind='normative', text='a'*600+'Только при ширине до 6 м.'),
               dict(id='long', kind='normative', text='b'*2000+'Оговорка в конце.')]
    result = pack_sources({}, sources, limit=10000, planning=True)
    short, long = result['sources']
    assert short['text'] == sources[0]['text'] and short['next_offset'] is None
    assert len(long['text']) == long['next_offset'] == 450
    assert long['excerpt_truncated'] and long['full_length'] == len(sources[1]['text'])


def test_late_metadata_is_retained_while_omissions_and_partial_reads_are_explicit():
    value = dict(required_validation_claims=['method', 'normative_applicability'],
        previous_literal_parameters=[dict(source_id='project', quote='Exact retained fact')],
        answer_validation=['Unknown required condition'], tool_results=[dict(text='z'*2000)])
    sources = [dict(id='project', kind='project', text='p'*3000),
               dict(id='background', kind='normative', text='n'*1000)]
    result = pack_sources(value, sources, limit=3000)
    for key, part in value.items():
        assert result[key] == part
    brief = result['sources'][0]
    assert brief['excerpt_truncated'] and 0 < brief['next_offset'] == len(brief['text']) < 3000
    assert result['archived_source_ids'] == ['background']
    assert size(result) <= 3000


def test_metadata_overflow_stops_before_any_model_call():
    ask = Mock()
    with pytest.raises(ValueError, match='metadata exceeds budget'):
        run(ask, None, None, '', dict(required_validation_claims=['x'*MAX_CONTEXT]),
            AgentAnswer, None, {'queries': []}, toolkit())
    ask.assert_not_called()


def test_named_coefficient_condition_is_delivered_before_unrelated_search_previews():
    sources=[dict(id=f'old-{i}',kind='normative',text='Unrelated search result '*90) for i in range(8)]
    literal='10.2 Коэффициент запаса на уплотнение: ориентировочно 1,25–1,3. '+('Условие материала. '*25)
    sources.append(dict(id='condition',kind='normative',text=literal))
    result=pack_sources(dict(coefficient_condition_sources=[dict(source_id='condition')]),
        sources,limit=2500,planning=True)
    assert result['sources'][0]['id']=='condition'
    assert result['sources'][0]['text']==literal
    assert result['sources'][0]['next_offset'] is None
    assert len(serialize_context(result))<=2500
    ordered,critical=source_order(dict(coefficient_condition_sources=[dict(source_id='missing')]),sources,[],{})
    assert 'missing' not in ordered and 'missing' not in critical


def test_final_runtime_packs_after_coverage_without_catalogue_schema_changes():
    tools = toolkit()
    tools.coverage['available_tables'] = [dict(page=89, table_id='table', rows=42, columns=26, total_rows=[41])]
    original = deepcopy(tools.coverage)
    ask = Mock(return_value=AgentAnswer(summary='No inference', items=[]))
    run(ask, None, None, '', {}, AgentAnswer, None, {'queries': []}, tools)
    supplied = ask.call_args.args[3]
    assert supplied['coverage'] == original
    assert tools.coverage['available_tables'] == original['available_tables']
    assert size(supplied) <= MAX_CONTEXT
