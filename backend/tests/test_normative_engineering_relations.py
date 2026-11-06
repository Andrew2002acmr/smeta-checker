"""Engineering navigation preserves uncertainty and exact version boundaries."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from analysis_replay import digest
from audit_models import VorPosition
import normative_engineering_relations as relations


def fixture():
    record = dict(project_id='project', vor_version_id='vor', config=dict(pdf_versions={'pd.pdf': 'pdf'}))
    source = dict(id='p1:quantity', kind='project', version_id='pdf', page=1,
                  label='ПД', text='ПК 1+00 — устройство основания: 400 шт.', origin='native')
    position = VorPosition(index=0, work_name='Устройство основания', unit='шт.', volume='500',
        position_source=dict(position_id='position-0', document_sha256='a' * 64, format='xlsx', sheet='ВОР', row=5,
                             header_row=1, sections=[dict(address='A2', text='Конструкция №1')]))
    fact = dict(id='independent-project-observation', parameter='quantity', value='400', unit='шт.',
                source_id=source['id'], version_id='pdf', literal_state='verified',
                subject_binding_state='verified', calculation_state='not_applicable',
                comparison_state='possible_mismatch', owner_position=0,
                proposed_owner_position=0, scope_quote='ПК 1+00 — устройство основания')
    ledger = dict(facts=[fact], source_hashes={source['id']: digest(source)})
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=[source])
    return record, source, position, ledger, graph


class Repository:
    supports_analysis_replay = True

    def __init__(self):
        self.saved = {}

    def save_analysis_artifact(self, project_id, identity, payload):
        self.saved[digest(identity)] = dict(identity=deepcopy(identity), payload=deepcopy(payload))
        return payload

    def analysis_artifact(self, project_id, key):
        return self.saved.get(key)

    def engineering_relation_artifacts(self, project_id, vor_version_id, pdf_versions):
        return [v for v in self.saved.values() if v['identity']['project_id'] == project_id
                and v['identity']['stage'] == relations.STAGE
                and v['identity']['vor_version_id'] == vor_version_id
                and v['identity']['pdf_versions'] == sorted(pdf_versions)]


def test_workbook_structure_remains_candidate_despite_equal_names_and_quantities():
    record, source, position, _, _ = fixture()
    other = position.model_copy(update={'index': 1, 'volume': '400'})
    graph = relations.build(record, {0: position, 1: other}, sources=[source])
    assert len([e for e in graph['entities'] if e['kind'] == 'Work']) == 2
    assert all(r['state'] == 'candidate' for r in graph['relations'])
    assert not any(e['kind'] == 'Fact' for e in graph['entities'])


def test_verified_project_fact_and_binding_survive_vor_mismatch():
    _, source, _, ledger, graph = fixture()
    fact = next(e for e in graph['entities'] if e['kind'] == 'Fact')['fact']
    assert fact['value'] == '400'
    assert fact['literal_state'] == 'verified'
    assert 'comparison_state' not in fact
    assert 'subject_binding_state' not in fact
    assert all(r['state'] == 'verified' for r in graph['relations'] if r['kind'] in {'HAS_QUANTITY', 'CORRESPONDS_TO'})
    assert graph['sources'] == [source]
    assert ledger['facts'][0]['comparison_state'] == 'possible_mismatch'


def test_unbound_literal_is_retained_but_does_not_verify_scope():
    record, source, position, ledger, _ = fixture()
    ledger['facts'][0].update(subject_binding_state='unresolved', owner_position=None)
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=[source])
    assert next(r for r in graph['relations'] if r['kind'] == 'SUPPORTED_BY')['state'] == 'verified'
    assert all(r['state'] == 'candidate' for r in graph['relations'] if r['kind'] in {'HAS_QUANTITY', 'CORRESPONDS_TO'})


@pytest.mark.parametrize('change', ['source_text', 'source_version', 'ledger_hash'])
def test_altered_or_unpinned_source_cannot_create_project_fact(change):
    record, source, position, ledger, _ = fixture()
    if change == 'source_text':
        source['text'] = '999'
    elif change == 'source_version':
        source['version_id'] = 'new-pdf'
        ledger['source_hashes'][source['id']] = digest(source)
    else:
        ledger['source_hashes'][source['id']] = 'broken'
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=[source])
    assert not any(e['kind'] == 'Fact' for e in graph['entities'])


def test_store_load_integrity_and_exact_document_pins():
    record, _, _, _, graph = fixture()
    context = SimpleNamespace(repository=Repository())
    receipt = relations.store(context, record, graph)
    assert relations.load(context, record, receipt['key']) == graph
    assert relations.load_for_run(context, record)['sources'] == graph['sources']
    stale = deepcopy(record)
    stale['config']['pdf_versions']['pd.pdf'] = 'new-pdf'
    assert relations.load_for_run(context, stale)['entities'] == []
    with pytest.raises(ValueError, match='pins differ'):
        relations.load(context, stale, receipt['key'])
    context.repository.saved[receipt['key']]['payload']['entities'][0]['label'] = 'Corrupted'
    with pytest.raises(ValueError, match='integrity'):
        relations.load(context, record, receipt['key'])


def test_prototype_graph_contract_is_preserved_but_not_loaded_as_current():
    record,_,_,_,graph=fixture()
    context=SimpleNamespace(repository=Repository())
    current=relations.store(context,record,graph)
    old=deepcopy(graph)
    old['schema']=1
    identity=dict(schema=1,stage='engineering_relations_v1',project_id=record['project_id'],
        vor_version_id=record['vor_version_id'],pdf_versions=['pdf'],graph_sha256=digest(old))
    context.repository.save_analysis_artifact(record['project_id'],identity,old)
    merged=relations.load_for_run(context,record)
    assert merged['entities']==graph['entities']
    assert len(context.repository.saved)==2
    assert relations.load(context,record,current['key'])==graph
    with pytest.raises(ValueError,match='integrity'):
        relations.load(context,record,digest(identity))


def test_merge_conflicting_proofs_never_chooses_a_confirmed_relation_by_order():
    record, _, _, _, graph = fixture()
    context = SimpleNamespace(repository=Repository())
    relations.store(context, record, graph)
    changed = deepcopy(graph)
    edge = next(r for r in changed['relations'] if r['kind'] == 'HAS_QUANTITY')
    edge.update(state='candidate', reason='Принадлежность оспорена новым доказательством.')
    relations.store(context, record, changed)
    merged = relations.load_for_run(context, record)
    result = next(r for r in merged['relations'] if r['id'] == edge['id'])
    assert result['state'] == 'candidate'
    assert len(result['conflicting_variants']) == 2
    assert result['id'] in merged['conflicts']
    assert len(context.repository.saved) == 2  # Old immutable result is preserved.
    context.repository.saved = dict(reversed(list(context.repository.saved.items())))
    assert relations.load_for_run(context, record) == merged


def test_ambiguous_source_handle_is_not_returned_as_a_readable_source():
    record, source, position, ledger, graph = fixture()
    context = SimpleNamespace(repository=Repository())
    relations.store(context, record, graph)
    changed_source = dict(source, text=source['text'] + '\nДругая выдержка')
    changed_ledger = deepcopy(ledger)
    changed_ledger['source_hashes'][source['id']] = digest(changed_source)
    changed = relations.build(record, {0: position}, ledgers=[changed_ledger], sources=[changed_source])
    relations.store(context, record, changed)
    merged = relations.load_for_run(context, record)
    assert merged['source_conflicts'] == [source['id']]
    assert merged['sources'] == []
    for e in merged['entities']:
        if e.get('source_id') == source['id']:
            assert 'read_request' not in e


def test_construction_navigation_is_paginated_and_includes_facts_sources_and_open_links():
    _, _, _, _, graph = fixture()
    construction = next(e for e in graph['entities'] if e['kind'] == 'Construction')
    offset, found = 0, []
    while True:
        page = relations.related(graph, construction['id'], offset, limit=1)
        assert len(json.dumps(page, ensure_ascii=False)) <= 3500
        found.extend(page['items'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    kinds = {item['entity']['kind'] for item in found}
    kinds.update(e['kind'] for item in found for e in item['evidence'])
    assert {'Work', 'Fact', 'Evidence', 'ProjectArea'} <= kinds
    assert any(item['relation']['state'] == 'candidate' for item in found)
    assert len({item['relation']['id'] for item in found}) == len(found)


def test_search_is_stable_bounded_discovery_and_unknown_id_fails():
    _, _, _, _, graph = fixture()
    first = relations.search(graph, 'основания', limit=1)
    assert first['discovery_only'] is True
    assert len(first['items']) == 1
    second = relations.search(graph, 'основания', offset=first['next_offset'], limit=1)
    assert second['items'][0]['id'] != first['items'][0]['id']
    assert first == relations.search(graph, 'основания', limit=1)
    with pytest.raises(KeyError):
        relations.related(graph, 'unknown')


def test_related_kind_filter_paginates_only_matching_entities_without_changing_ids():
    _, _, _, _, graph=fixture()
    construction=next(e for e in graph['entities']if e['kind']=='Construction')
    def collect(kind):
        rows,offset=[],0
        while True:
            page=relations.related(graph,construction['id'],offset,limit=1,entity_kind=kind)
            assert page['total']>=len(page['items'])
            rows.extend(page['items'])
            if page['next_offset']is None:
                return rows,page['total']
            assert page['next_offset']==offset+len(page['items'])
            offset=page['next_offset']
    all_rows,_=collect('')
    for kind in ('Fact','Work','Material','ProjectArea','Evidence'):
        actual,total=collect(kind)
        expected=[row for row in all_rows if row['entity']['kind']==kind]
        assert [row['relation']['id']for row in actual]==[row['relation']['id']for row in expected]
        assert total==len(expected)


def test_related_path_requires_every_ancestral_relation_to_be_verified():
    _,_,_,_,graph=fixture()
    construction=next(e for e in graph['entities']if e['kind']=='Construction')
    rows=relations.related(graph,construction['id'],entity_kind='Fact')['items']
    assert rows and rows[0]['relation']['state']=='verified'
    assert rows[0]['path_state']=='unresolved'
    assert rows[0]['unverified_relation_ids']
    fact=next(e for e in graph['entities']if e['kind']=='Fact')
    direct=relations.related(graph,fact['id'],entity_kind='Evidence')['items']
    assert direct and direct[0]['path_state']=='verified'
    assert direct[0]['unverified_relation_ids']==[]


def test_related_uses_verified_detour_instead_of_shorter_candidate_path():
    def entity(name, kind='Work'):
        return dict(id=name, label=name, kind=kind, version_id='pd', origin='project')

    def edge(name, left, right, state):
        return dict(id=name, source=left, target=right, evidence_ids=[], state=state)

    edges = [
        edge('shortcut', 'start', 'subject', 'candidate'),
        edge('first', 'start', 'bound', 'verified'),
        edge('second', 'bound', 'subject', 'verified'),
        edge('evidence', 'subject', 'proof', 'verified'),
    ]
    graph = dict(entities=[entity('start'), entity('bound'), entity('subject'), entity('proof', 'Evidence')],
                 relations=edges)
    page = relations.related(graph, 'start', entity_kind='Evidence')
    assert len(page['items']) == 1
    assert page['items'][0]['via_relation_ids'] == ['first', 'second', 'evidence']
    assert page['items'][0]['path_state'] == 'verified'
    graph['relations'] = list(reversed(edges))
    assert relations.related(graph, 'start', entity_kind='Evidence') == page


def test_calculated_fact_exposes_pinned_operands_and_invalidates_on_source_conflict():
    record, source, position, ledger, _ = fixture()
    source.update(origin='calculated_from_cells', numeric_verified=True)
    operands = [dict(source, id=f'part:{index}', origin='native', text=text,
                     unit='шт.', unit_verified=True)
                for index, text in enumerate(('ПК 1+00\n100', 'ПК 2+00\n300'))]
    all_sources = [source, *operands]
    ledger['facts'][0].update(operation='sum', calculation_state='verified',
                              operand_source_ids=[part['id'] for part in operands])
    ledger['source_hashes'] = {s['id']: digest(s) for s in all_sources}
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=all_sources)
    context = SimpleNamespace(repository=Repository())
    relations.store(context, record, graph)
    fact = next(e for e in graph['entities'] if e['kind'] == 'Fact')
    by_id = {e['id']: e for e in graph['entities']}
    edges = [e for e in graph['relations'] if e['kind'] == 'DERIVED_FROM']
    assert {by_id[e['target']]['read_request']['source_id'] for e in edges} == {'part:0', 'part:1'}
    assert all(e['state'] == 'verified' and e['source'] == fact['id'] for e in edges)

    broken = deepcopy(graph)
    del broken['ledger_proofs'][0]['source_hashes']['part:1']
    with pytest.raises(ValueError, match='lacks matching validated ledger proof'):
        relations.store(context, record, broken)

    operands[0]['text'] = 'Другое прочтение контекста\n100'
    ledger['source_hashes'][operands[0]['id']] = digest(operands[0])
    changed = relations.build(record, {0: position}, ledgers=[ledger], sources=all_sources)
    relations.store(context, record, changed)
    merged = relations.load_for_run(context, record)
    assert merged['source_conflicts'] == ['part:0']
    assert merged['invalidated_sum_sources'] == [source['id']]
    assert all(e['state'] != 'verified' for e in merged['relations'])
    assert {s['id'] for s in merged['sources']} == {'part:1'}


def test_rejects_forged_verified_workbook_edge():
    record, _, _, _, graph = fixture()
    edge = next(e for e in graph['relations'] if e['method'] == 'workbook_section')
    edge['state'] = 'verified'
    with pytest.raises(ValueError, match='requires documentary evidence'):
        relations.store(SimpleNamespace(repository=Repository()), record, graph)


def test_verified_relation_requires_matching_binding_proof_not_just_a_source_handle():
    record, _, _, _, graph = fixture()
    graph['ledger_proofs'][0]['facts'][0]['subject_binding_state'] = 'unresolved'
    with pytest.raises(ValueError, match='lacks matching validated ledger proof'):
        relations.store(SimpleNamespace(repository=Repository()), record, graph)


def test_modified_project_value_cannot_borrow_a_verified_relation():
    record, _, _, _, graph = fixture()
    next(e for e in graph['entities'] if e['kind'] == 'Fact')['fact']['value'] = '999'
    with pytest.raises(ValueError, match='lacks matching validated ledger proof'):
        relations.store(SimpleNamespace(repository=Repository()), record, graph)


def test_comparison_only_changes_do_not_invalidate_project_observation():
    record, source, position, ledger, graph = fixture()
    context = SimpleNamespace(repository=Repository())
    relations.store(context, record, graph)
    changed_ledger = deepcopy(ledger)
    changed_ledger['facts'][0]['comparison_state'] = 'confirmed'
    changed = relations.build(record, {0: position}, ledgers=[changed_ledger], sources=[source])
    relations.store(context, record, changed)
    merged = relations.load_for_run(context, record)
    assert merged['conflicts'] == []
    assert next(r for r in merged['relations'] if r['kind'] == 'HAS_QUANTITY')['state'] == 'verified'
    assert len([e for e in merged['entities'] if e['kind'] == 'Fact']) == 1


def test_different_valid_citation_excerpts_do_not_split_the_same_project_observation():
    record, source, position, ledger, _ = fixture()
    first = ledger['facts'][0]
    first['quote'] = '400 шт.'
    second = dict(first, quote='ПК 1+00 — устройство основания: 400 шт.')
    ledger['facts'].append(second)
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=[source])
    assert len([e for e in graph['entities'] if e['kind'] == 'Fact']) == 1
    assert len(graph['ledger_proofs'][0]['facts']) == 2
    relations.store(SimpleNamespace(repository=Repository()), record, graph)


def test_russian_material_type_uses_existing_structural_ownership_as_candidate():
    record, _, position, _, _ = fixture()
    work = position.model_copy(update={'position_type': 'Работа'})
    material = position.model_copy(update={'index': 1, 'work_name': 'Шпалы', 'position_type': 'Материал',
        'position_source': position.position_source.model_copy(update={'row': 6, 'position_id': 'position-1'})})
    graph = relations.build(record, {0: work, 1: material})
    entities = {e['id']: e for e in graph['entities']}
    edge = next(r for r in graph['relations'] if r['kind'] == 'USES_MATERIAL')
    assert entities[edge['source']]['kind'] == 'Work'
    assert entities[edge['target']]['kind'] == 'Material'
    assert edge['state'] == 'candidate'


def test_content_bound_alias_reuses_the_same_observation_without_false_conflicts():
    from normative_source_identity import shared_source
    record, source, position, ledger, graph = fixture()
    alias = shared_source(source, 3)
    assert alias is not None
    adapted = deepcopy(ledger)
    adapted['facts'][0]['source_id'] = alias['id']
    adapted['source_hashes'] = {alias['id']: digest(alias)}
    alias_graph = relations.build(record, {0: position}, ledgers=[adapted], sources=[alias])
    assert alias_graph['entities'] == graph['entities']
    assert alias_graph['relations'] == graph['relations']
    assert alias_graph['sources'] == [source]
    assert alias_graph['source_aliases'][alias['id']]['canonical_id'] == source['id']
    context = SimpleNamespace(repository=Repository())
    relations.store(context, record, graph)
    relations.store(context, record, alias_graph)
    assert relations.load_for_run(context, record)['conflicts'] == []


def test_two_physical_child_rows_share_one_addressed_parent_without_losing_evidence():
    from normative_agent import EvidenceTools, EvidenceRequest
    from test_normative_paired_scope import fixture as parent_fixture
    prepared, table, _, _ = parent_fixture()
    for cell in deepcopy([c for c in table['cells'] if c['row'] == 2]):
        cell.update(row=3, cell_id=cell['cell_id'] + '-second',
                    bbox=[cell['bbox'][0], 30, cell['bbox'][2], 40])
        if cell['column'] == 0:
            cell['text'] = '10.3'
        table['cells'].append(cell)
    table['row_count'] = 4
    prepared.index.metadata['page_geometry']['0']['height'] = 40
    tools = EvidenceTools.from_prepared(prepared.index, 'v')
    tools.execute(EvidenceRequest(tool='read_table', page=1, table_id='table', row_ids=[2, 3], column_ids=[2, 3]))
    sources = [s for s in tools.sources if s.get('paired_parent_context')]
    assert len(sources) == 2
    facts = [dict(id='quantity-' + str(i), value='114', unit='шт', parameter='quantity',
        source_id=s['id'], literal_state='verified', subject_binding_state='unresolved') for i, s in enumerate(sources)]
    record = dict(project_id='project', vor_version_id='vor', config=dict(pdf_versions={'pd.pdf': 'v'}))
    graph = relations.build(record, {}, ledgers=[dict(facts=facts,
        source_hashes={s['id']: digest(s) for s in sources})], sources=sources)
    construction = next(e for e in graph['entities'] if e['kind'] == 'Construction')
    assert len(construction['evidence_ids']) == 2
    assert len([e for e in graph['entities'] if e['kind'] == 'Construction']) == 1
    assert len([r for r in graph['relations'] if r['kind'] == 'PART_OF']) == 2
    assert all(r['state'] == 'candidate' for r in graph['relations'] if r['kind'] == 'PART_OF')
    materials=[e for e in graph['entities']if e['kind']=='Material']
    assert len(materials)==2 and len({e['id']for e in materials})==2
    assert len({e['label']for e in materials})==1  # Equal names never merge rows.
    assert all(e['classification_state']=='candidate'for e in materials)
    assert all(e['read_request']['column_ids']==[1,2,3]for e in materials)
    links=relations.related(graph,construction['id'],entity_kind='Material')
    assert links['total']==2
    assert links['items'][0]['entity']['classification_state']=='candidate'
    assert links['items'][0]['path_state']=='unresolved'
    read=tools.execute(EvidenceRequest(**materials[0]['read_request']))
    assert {row['column']for row in read['rows']}=={1,2,3}
    assert len(read['row_measurements'])==1
    assert read['row_measurements'][0]['raw_values']=='114/23,53'


def test_action_row_with_material_noun_does_not_become_a_material_entity():
    from normative_agent import EvidenceTools, EvidenceRequest
    from test_normative_paired_scope import fixture as parent_fixture
    prepared,table,_,_=parent_fixture()
    next(c for c in table['cells']if c['row']==2 and c['column']==1)['text']='Укладка шпал ШТУ 03.02'
    tools=EvidenceTools.from_prepared(prepared.index,'v')
    tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='table',row_ids=[2],column_ids=[2,3]))
    source=next(s for s in tools.sources if s.get('paired_parent_context'))
    fact=dict(id='q',value='114',unit='шт',source_id=source['id'],literal_state='verified',subject_binding_state='unresolved')
    record=dict(project_id='p',vor_version_id='v',config=dict(pdf_versions={'pd':'v'}))
    graph=relations.build(record,{},ledgers=[dict(facts=[fact],source_hashes={source['id']:digest(source)})],sources=[source])
    assert not any(e['kind']=='Material'for e in graph['entities'])


def test_input_objects_are_unchanged_and_physical_history_stays_server_side():
    record, source, position, ledger, _ = fixture()
    source['ocr_history'] = ['full server-only record'] * 100
    ledger['source_hashes'][source['id']] = digest(source)
    before = deepcopy((record, source, ledger))
    graph = relations.build(record, {0: position}, ledgers=[ledger], sources=[source])
    assert (record, source, ledger) == before
    assert graph['sources'][0]['ocr_history'] == source['ocr_history']
    assert 'ocr_history' not in json.dumps(relations.search(graph, '400'), ensure_ascii=False)
    assert 'full server-only record' not in json.dumps(relations.related(graph, next(e['id'] for e in graph['entities'] if e['kind'] == 'Fact')), ensure_ascii=False)
