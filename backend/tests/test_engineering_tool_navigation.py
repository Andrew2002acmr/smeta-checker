"""Exercise engineering navigation through the actual EvidenceTools boundary.

No provider or prepared model answer is used in these tests.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

from audit_models import VorPosition
from normative_agent import EvidenceRequest, EvidenceTools, model_result
from normative_engineering_relations import build, load_for_run, store
from normative_engineering_tools import attach
from test_normative_engineering_relations import fixture, Repository


def toolkit(graph=None, *, pages=1):
    text = 'Устройство основания. ' + 'Описание проектной конструкции и участка. ' * 200
    index = SimpleNamespace(texts=tuple(text for _ in range(pages)),
        tables=tuple([] for _ in range(pages)), metadata=dict(document_version_id='pdf', preparation_complete=True),
        pending_ocr=[], errors={}, quality={})
    tools = EvidenceTools.from_prepared(index, 'pdf')
    tools.engineering_graph = graph
    return tools


def large_graph():
    record = dict(project_id='project', vor_version_id='vor', config=dict(pdf_versions={'pd.pdf': 'pdf'}))
    positions = {i: VorPosition(index=i, work_name='Устройство основания ' + str(i) + ' — ' + 'характеристика ' * 30,
        unit='м2', volume='100') for i in range(18)}
    return build(record, positions)


def test_entity_kind_search_and_addressed_read_use_actual_tool_dispatch():
    _, _, _, _, graph = fixture()
    tools = toolkit(graph)
    result = tools.execute(EvidenceRequest(tool='search_project', query='Конструкция', entity_kind='Construction'))
    assert result['engineering_entities']['items']
    assert all(e['kind'] == 'Construction' for e in result['engineering_entities']['items'])
    assert tools.sources == []  # Entity discovery does not preload the archive.
    entity = result['engineering_entities']['items'][0]
    read = tools.execute(EvidenceRequest(tool='read_source', entity_id=entity['id']))
    assert read['entity']['id'] == entity['id']
    assert any(item['entity']['kind'] == 'Work' for item in read['items'])
    assert any(item['relation']['state'] == 'candidate' for item in read['items'])
    assert tools.events[-1]['kind'] == 'engineering_navigation_v1'


def test_explicit_entity_pagination_delivers_every_id_after_model_budgeting():
    graph = large_graph()
    tools = toolkit(graph)
    offset, found = 0, []
    for _ in range(30):
        raw = tools.execute(EvidenceRequest(tool='search_project', query='основания', entity_kind='Work', entity_offset=offset))
        wire = model_result(raw, 4000)
        assert len(json.dumps(wire, ensure_ascii=False)) <= 4000
        page = wire['engineering_entities']
        found.extend(e['id'] for e in page['items'] if 'id' in e)
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert len(found) == len(set(found)) == len(graph['entities'])


def test_mixed_page_entity_search_cannot_skip_entities_when_model_reply_is_bounded():
    graph = large_graph()
    tools = toolkit(graph, pages=7)
    offset, found = 0, []
    for _ in range(30):
        raw = tools.execute(EvidenceRequest(tool='search_project', query='основания', entity_offset=offset))
        assert raw.get('pages') and raw.get('engineering_entities')
        wire = model_result(raw, 4000)
        assert len(json.dumps(wire, ensure_ascii=False)) <= 4000
        # Source search and engineering search are separate cursors. Trimming
        # the page channel must not silently advance the engineering cursor.
        assert 'engineering_entities' in wire
        page = wire['engineering_entities']
        found.extend(e['id'] for e in page['items'] if 'id' in e)
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert len(found) == len(set(found)) == len(graph['entities'])


def test_lazy_read_registers_only_requested_source_and_keeps_its_full_history():
    _, source, _, _, graph = fixture()
    tools = toolkit(graph)
    assert tools.sources == []
    reply = tools.execute(EvidenceRequest(tool='read_source', source_id=source['id']))
    assert reply['source_id'] == source['id']
    assert tools.sources == [source]
    assert source['id'] in tools.preferred
    assert not any(e['id'] in {s['id'] for s in tools.sources} for e in graph['entities'])


def test_lazy_read_fails_closed_on_a_source_registry_identity_conflict():
    _, source, _, _, graph = fixture()
    tools = toolkit(graph)
    tools.sources.append(dict(source, text='Иное значение того же handle'))
    before = deepcopy(tools.sources)
    result = tools.execute(EvidenceRequest(tool='read_source', source_id=source['id']))
    assert result['error'] == 'engineering_source_collision'
    assert tools.sources == before


def test_lazy_read_reapplies_current_server_unit_context_before_identity_comparison(monkeypatch):
    _, source, _, _, graph = fixture()
    tools = toolkit(graph)
    tools.context = SimpleNamespace(repository=SimpleNamespace(supports_analysis_replay=True))
    tools.record = {}
    def attach_current_units(context, record, payload):
        result = deepcopy(payload)
        for item in result['sources']:
            item['confirmed_unit_context'] = dict(unit='шт', state='verified', proof='current-server-context')
        return result
    monkeypatch.setattr('normative_confirmed_units.attach', attach_current_units)
    first = tools.execute(EvidenceRequest(tool='read_source', source_id=source['id']))
    second = tools.execute(EvidenceRequest(tool='read_source', source_id=source['id']))
    assert first['source_id'] == second['source_id'] == source['id']
    assert len(tools.sources) == 1
    assert tools.sources[0]['confirmed_unit_context']['proof'] == 'current-server-context'


def test_unknown_entity_and_source_are_explicit_errors():
    _, _, _, _, graph = fixture()
    tools = toolkit(graph)
    assert tools.execute(EvidenceRequest(tool='read_source', entity_id='engineering:unknown'))['error'] == 'unknown_engineering_entity'
    assert tools.execute(EvidenceRequest(tool='read_source', source_id='missing'))['error'] == 'unknown_source'


def test_attach_is_version_pinned_and_does_not_preload_saved_sources():
    record, source, _, _, graph = fixture()
    context = SimpleNamespace(repository=Repository())
    store(context, record, graph)
    tools = toolkit()
    attach(tools, context, record)
    assert tools.sources == []
    assert tools.engineering_graph['sources'] == [source]
    stale = deepcopy(record)
    stale['config']['pdf_versions']['pd.pdf'] = 'new-pdf'
    other = toolkit()
    attach(other, context, stale)
    assert other.engineering_graph['entities'] == []
    assert other.execute(EvidenceRequest(tool='read_source', source_id=source['id']))['error'] == 'unknown_source'
    assert load_for_run(context, stale)['relations'] == []


def test_adjacency_pagination_survives_the_model_transport_without_losing_ids():
    _, _, _, _, graph = fixture()
    construction = next(e for e in graph['entities'] if e['kind'] == 'Construction')
    tools = toolkit(graph)
    offset, ids = 0, []
    for _ in range(30):
        reply = tools.execute(EvidenceRequest(tool='read_source', entity_id=construction['id'], offset=offset))
        wire = model_result(reply, 4000)
        ids.extend(item['relation']['id'] for item in wire['items'] if 'relation' in item)
        if wire['next_offset'] is None:
            break
        assert wire['next_offset'] > offset
        offset = wire['next_offset']
    from normative_engineering_relations import related
    expected_ids, offset = set(), 0
    while True:
        expected = related(graph, construction['id'], offset, limit=20)
        expected_ids.update(item['relation']['id'] for item in expected['items'])
        if expected['next_offset'] is None:
            break
        offset = expected['next_offset']
    assert set(ids) == expected_ids


def test_main_persistence_boundary_makes_validated_mismatch_facts_addressable_in_fresh_tools():
    from normative_engineering_tools import persist
    from test_normative_fact_states import fixture as fact_fixture
    position, result = fact_fixture('500')
    position = VorPosition(index=position.index, number=position.number, work_name=position.work_name,
                           unit=position.unit, volume=position.volume, position_type=position.position_type)
    record = dict(project_id='project', vor_version_id='vor', config=dict(pdf_versions={'pd.pdf': 'pdf'}))
    context = SimpleNamespace(repository=Repository())
    persist(context, record, position, result, {position.index: position}, {})
    assert result['parameter_cards'][0]['state'] == 'possible_mismatch'
    assert result['coverage']['fact_ledger']['count'] == 1
    assert result['coverage']['engineering_relations']['entities'] > 0
    assert {v['identity']['stage'] for v in context.repository.saved.values()} == {
        'scoped_project_facts', 'engineering_relations_v1'}
    tools = toolkit()
    attach(tools, context, record)
    assert tools.sources == []
    found = tools.execute(EvidenceRequest(tool='search_project', query='400', entity_kind='Fact'))
    fact = found['engineering_entities']['items'][0]
    assert fact['fact']['literal_state'] == 'verified'
    read = tools.execute(EvidenceRequest(tool='read_source', entity_id=fact['id']))
    source = next(e for item in read['items'] for e in item['evidence'] if e.get('source_id'))
    tools.execute(EvidenceRequest(**source['read_request']))
    assert tools.sources[0]['text'] == result['sources'][0]['text']
    assert '400 шт' in tools.sources[0]['text']


def test_archived_union_result_can_be_read_completely_without_becoming_document_evidence():
    tools = toolkit(large_graph(), pages=7)
    original = tools.execute(EvidenceRequest(tool='search_project', query='основания'))
    wire = model_result(original, 4000)
    request = wire['archive_read_request']
    handle = request['source_id']
    assert handle.startswith('tool-result:')
    pieces, offset = [], 0
    while True:
        read = tools.execute(EvidenceRequest(tool='read_source', source_id=handle, offset=offset))
        assert read['navigation_only'] and not read['document_evidence']
        sent = model_result(read, 4000)
        assert len(json.dumps(sent, ensure_ascii=False)) <= 4000
        pieces.append(sent['text'])
        if sent['next_offset'] is None:
            break
        assert sent['next_offset'] > offset
        offset = sent['next_offset']
    restored = json.loads(''.join(pieces))
    assert restored == {k: v for k, v in original.items() if k not in {'result_handle', 'archive_read_request'}}
    assert handle not in {s['id'] for s in tools.sources}
    assert handle not in tools.preferred
    from normative_position_check import AgentAnswer, validate_items
    answer = AgentAnswer(summary='Attempted citation', items=[dict(topic='Количество', state='confirmed',
        claim_ids=['quantity'], explanation='Invalid archive citation', project_source_ids=[handle], normative_source_ids=[])])
    assert validate_items(answer.items, tools.sources)[0]['state'] == 'needs_facts'
    assert tools.execute(EvidenceRequest(tool='read_source', source_id='tool-result:forged'))['error'] == 'unknown_tool_result'


def test_quote_dense_archived_json_retains_every_character_through_model_budgeting():
    tools = toolkit()
    # Real tool rows may contain many short column keys/quotes. The JSON text
    # itself is escaped again when put into the model tool-result envelope.
    data = {'rows': [dict(row=i, column=i % 12, text='"' * 100, source_id='s' + str(i),
                         value='"', unit='"') for i in range(120)]}
    archived = tools._archive_tool_result(data)
    handle = archived['result_handle']
    offset, parts = 0, []
    while True:
        raw = tools.execute(EvidenceRequest(tool='read_source', source_id=handle, offset=offset))
        sent = model_result(raw, 4000)
        assert ' [truncated;' not in sent['text']
        parts.append(sent['text'])
        if sent['next_offset'] is None:
            break
        offset = sent['next_offset']
    assert json.loads(''.join(parts)) == data
