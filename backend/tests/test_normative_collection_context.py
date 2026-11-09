"""Pinned, read-only reverse lookup; fixtures retain literal source bytes."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import normative_collection_context as context


FIXTURE = Path(__file__).parent / 'fixtures/collection-74-native.json'


@pytest.fixture
def corpus():
    data = json.loads(FIXTURE.read_text(encoding='utf-8'))
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE sources(id TEXT,designation TEXT,kind TEXT,pages INTEGER,sha256 TEXT,blob_path TEXT,metadata_json TEXT)')
    source = data['source']
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?)', tuple(source[key] for key in
        ('id', 'designation', 'kind', 'pages', 'sha256', 'blob_path', 'metadata_json')))
    db.execute('CREATE TABLE clauses(id TEXT,source_id TEXT,text TEXT)')
    entities = {item['id']: deepcopy(item) for item in [*data['norms'], *data['clauses']]}
    for item in data['clauses']:
        db.execute('INSERT INTO clauses VALUES(?,?,?)', (item['id'], item['source_id'], item['text']))
    kb = SimpleNamespace(identity=deepcopy(data['provenance']['kb_identity']),
        db=db, candidates=deepcopy(data['norms']), entities=entities, source=source,
        connect=lambda: nullcontext(db), calls=[])

    def entity(kind, key):
        value = deepcopy(kb.entities[key])
        assert value['entity_kind'] == kind
        kb.calls.append((kind, key))
        return value

    kb.entity = entity
    yield kb
    db.close()


def lookup(kb, candidates=None):
    return context.collection_context(kb, kb.candidates if candidates is None else candidates,
                                      expected_pin=deepcopy(kb.identity))


def add_clause(kb, item):
    kb.entities[item['id']] = deepcopy(item)
    kb.db.execute('INSERT INTO clauses VALUES(?,?,?)', (item['id'], item['source_id'], item['text']))


def only_clause(kb, *, text, label='1.27.35', **changes):
    item = deepcopy(next(value for value in kb.entities.values() if value.get('label') == 'page-3-offset-0'))
    item.update(id='test-clause', text=text, label=label, **changes)
    kb.db.execute('DELETE FROM clauses')
    add_clause(kb, item)
    return item


def test_actual74_delivers_full35_37_with_limits_project_consumption_and_no_promotion(corpus):
    before = deepcopy(corpus.candidates)
    result = lookup(corpus)
    assert result['reason'] == 'literal_collection_context'
    assert len(result['sources']) == 2
    chunk = next(row for row in result['context']['literal_chunks']
                 if any(m['label'] == '1.27.35' for m in row['matched_paragraphs']))
    original = corpus.entities['clause-d7fb04d617811bd6b4ffa2e8e065774e']
    assert chunk['quote'] == original['text']
    assert hashlib.sha256(chunk['quote'].encode()).hexdigest() == original['fixture_text_sha256']
    assert [m['label'] for m in chunk['matched_paragraphs']] == ['1.27.35', '1.27.37']
    assert 'состав смеси подбирать индивидуально' in chunk['quote']
    assert 'принимать на основании проектных данных' in chunk['quote']
    assert 'при толщине слоя до 8 см' in chunk['quote']
    assert 'толщине слоя более 8 см его устройство нормировать в два слоя' in chunk['quote']
    assert corpus.candidates == before
    assert result['context']['coverage_complete'] is False
    assert all(card['automatic_application_allowed'] is False for card in result['sources'])
    assert not any('state' in card for card in result['sources'])
    assert all(card['origin'] == 'extracted_native_unreviewed' for card in result['sources'])
    assert all(card['source_sha256'] == corpus.source['sha256'] for card in result['sources'])
    assert all(card['kb_identity'] == corpus.identity for card in result['sources'])
    assert result['sources'][0]['citations'] == original['citations']
    assert 'amendments' in result['sources'][0]['source_provenance']['edition']


def test_same_table_reference_is_navigation_not_a_new_work_obligation(corpus):
    result = lookup(corpus)
    gutter = next(row for row in result['context']['literal_chunks']
                  if any(m['label'] == '1.27.74' for m in row['matched_paragraphs']))
    assert 'асфальтобетонных лотков' in gutter['quote']
    assert '27-06-030-02 применяется одновременно' in gutter['quote']
    assert 'не создаёт обязанность' in result['context']['instruction']
    assert 'required' not in json.dumps(result, ensure_ascii=False)


def test_exact_clause_overlap_is_deduplicated_without_cutting_parent_chunk(corpus):
    item = corpus.entities['clause-d7fb04d617811bd6b4ffa2e8e065774e']
    start = item['text'].index('1.27.35.')
    end = item['text'].index('1.27.36.')
    duplicate = deepcopy(item)
    duplicate.update(id='duplicate-35', label='1.27.35', text=item['text'][start:end].strip())
    add_clause(corpus, duplicate)
    result = lookup(corpus)
    assert len(result['sources']) == 2
    assert all(card['clause_id'] != 'duplicate-35' for card in result['sources'])


@pytest.mark.parametrize('text', [
    '1.27.35. Норма 27-06-031-010 применяется при условии проекта.',
    '1.27.35. Таблица 27-06-0310 применяется при условии проекта.',
    '1.27.35. Норма 27-06-031-02 применяется при условии проекта.',
    '1.27.35. Норма ФЕР 27-06-031-01 применяется при условии проекта.',
    '1.47.35. Норма 27-06-031-01 применяется при условии проекта.',
])
def test_similar_codes_other_norm_other_family_and_other_collection_do_not_match(corpus, text):
    only_clause(corpus, text=text)
    assert lookup(corpus)['sources'] == []


def test_wrong_pdf_exact_same_code_is_not_returned(corpus):
    only_clause(corpus, text='1.27.35. Норма 27-06-031-01 применяется при условии проекта.',
                source_id='other-pdf')
    assert lookup(corpus)['sources'] == []


@pytest.mark.parametrize('change', [dict(source_kind='FER'), dict(status='ocr_unreviewed'),
    dict(designation='another collection'), dict(citations=[]),
    dict(citations=[dict(source_id='other', physical_page=3)]),
    dict(citations=[dict(source_id='pdf-6aa5203d913dd5eb1538a4c2', physical_page=58)])])
def test_wrong_source_status_citation_or_table_page_cannot_be_technical_context(corpus, change):
    only_clause(corpus, text='1.27.35. Норма 27-06-031-01 применяется при условии проекта.', **change)
    assert lookup(corpus)['sources'] == []


@pytest.mark.parametrize('text', [
    '1.27.35. Норма 27-06-031-01 применяется при условии',
    '1.27.35. Норма 27-06-031-01 применяется. Последующие условия не прочитаны.',
    'Допускается норма 27-06-031-01.\n1.27.36. Другой пункт.',
])
def test_window_end_or_missing_paragraph_start_never_proves_complete_condition(corpus, text):
    only_clause(corpus, text=text, label='page-3-offset-0')
    assert lookup(corpus)['sources'] == []


def test_entity_preview_must_equal_full_immutable_database_chunk(corpus):
    for item in corpus.entities.values():
        if item['entity_kind'] == 'clause':
            item['text'] = item['text'][:len(item['text']) // 2]
    result = lookup(corpus)
    assert result['sources'] == []
    assert any(value.startswith('clause_text_changed:') for value in result['warnings'])


@pytest.mark.parametrize('field', ['source_id', 'code', 'family', 'table_code'])
def test_candidate_identity_must_equal_canonical_norm(corpus, field):
    supplied = deepcopy(corpus.candidates)
    for candidate in supplied:
        candidate[field] = 'other'
    assert lookup(corpus, supplied)['sources'] == []


def test_output_preserves_existing_twelve_slot_catalog(corpus):
    candidates = [*corpus.candidates, *[dict(id=f'c{i}', entity_kind='clause') for i in range(10)]]
    before = deepcopy(candidates)
    assert lookup(corpus, candidates)['sources']
    assert candidates == before and len(candidates) == 12
    assert lookup(corpus, [*candidates, dict(id='overflow')])['reason'] == 'candidate_budget_exceeded'


def test_three_pdf_groups_cannot_bypass_two_source_bound(corpus):
    candidates = deepcopy(corpus.candidates[:1])
    for number in (2, 3):
        norm = deepcopy(candidates[0])
        norm.update(id=f'norm-pdf-{number}', source_id=f'pdf-{number}')
        corpus.entities[norm['id']] = norm
        candidates.append(deepcopy(norm))
    result = lookup(corpus, candidates)
    assert result['sources'] == [] and result['reason'] == 'source_budget_exceeded'
    assert result['context']['coverage_complete'] is False


def test_ranked_route_does_not_lose_technical_text_to_property_or_collection_overflow(corpus):
    alternatives = []
    for number in (2, 3):
        norm = deepcopy(corpus.candidates[0])
        norm.update(id=f'norm-pdf-{number}', source_id=f'pdf-{number}')
        corpus.entities[norm['id']] = norm
        alternatives.append(norm)
    candidates = [*[dict(id=f'property{i}', entity_kind='clause') for i in range(15)],
                  *corpus.candidates, *alternatives]
    result = context.ranked_collection_context(corpus, candidates, expected_pin=corpus.identity)
    assert result['reason'] == 'literal_collection_context'
    assert 'ranked_definitions_omitted:1' in result['warnings']
    assert ('norm', 'norm-pdf-3') not in corpus.calls
    assert result['context']['coverage_complete'] is False
    assert all(source['automatic_application_allowed'] is False for source in result['sources'])


def test_search_tool_returns_addressable_condition_to_definition_navigation(corpus, monkeypatch):
    from normative_agent import EvidenceRequest, EvidenceTools

    corpus.source_index = {corpus.source['id']: corpus.source}
    hits = [dict(norm, missing_facts=[], checks=[], rules=[]) for norm in corpus.candidates]
    monkeypatch.setattr('normative_agent.search_evidence', lambda *args, **kwargs: hits)
    tools = EvidenceTools.__new__(EvidenceTools)
    tools._reset([], corpus, {})
    result = tools.execute(EvidenceRequest(tool='search_norms', query=hits[0]['code']))
    definitions = {hit['source_id'] for hit in result['results']}
    routes = result['technical_context']['routes']
    assert routes and any(match['definition_source_ids'] for route in routes for match in route['matches'])
    assert all(set(match['definition_source_ids']) <= definitions
               for route in routes for match in route['matches'])
    for route in routes:
        source = next(s for s in tools.sources if s['id']==route['source_id'])
        assert source['source_sha256']==corpus.source['sha256']
        assert source['automatic_application_allowed'] is False
        receipt = tools.execute(EvidenceRequest(tool='read_source', source_id=route['source_id']))
        assert receipt['full_length']==len(source['text'])


def test_mixed_families_inside_one_source_are_not_assumed_equivalent(corpus):
    candidate = deepcopy(corpus.candidates[0])
    candidate.update(id='other-family', family='ГЭСНм')
    corpus.entities[candidate['id']] = deepcopy(candidate)
    result = lookup(corpus, [*corpus.candidates, candidate])
    assert result['sources'] == [] and result['reason'] == 'ambiguous_collection_families'


def test_query_overflow_does_not_select_arbitrary_partial_set(corpus, monkeypatch):
    monkeypatch.setattr(context, 'MAX_QUERY_ROWS', 1)
    result = lookup(corpus)
    assert not result['sources']
    assert any(w.startswith('clause_query_budget_exceeded:') for w in result['warnings'])


@pytest.mark.parametrize('constant', ['MAX_CHUNK_CHARS', 'MAX_ADDITION_CHARS', 'MAX_ADDITION_UTF8_BYTES'])
def test_budget_never_slices_conditions(corpus, monkeypatch, constant):
    monkeypatch.setattr(context, constant, 50)
    result = lookup(corpus)
    assert result['sources'] == []
    assert result['reason'] in {'collection_context_budget_exceeded', 'no_complete_literal_context'}


def test_chunk_count_limit_reports_incomplete_coverage(corpus, monkeypatch):
    monkeypatch.setattr(context, 'MAX_CHUNKS_PER_SOURCE', 1)
    result = lookup(corpus)
    assert len(result['sources']) == 1
    assert result['context']['coverage_complete'] is False
    assert any(w.startswith('clause_output_budget_exceeded:') for w in result['warnings'])


def test_wrong_pin_and_midread_pin_change_fail_closed(corpus):
    bad = dict(corpus.identity, sha256='f' * 64)
    assert context.collection_context(corpus, corpus.candidates, expected_pin=bad)['sources'] == []
    original = corpus.entity

    def changed(kind, key):
        value = original(kind, key)
        if kind == 'clause':
            corpus.identity = dict(corpus.identity, sha256='f' * 64)
        return value

    corpus.entity = changed
    assert lookup(corpus)['reason'] == 'corpus_pin_changed'


def test_whole_context_survives_source_compaction_and_aliases(corpus):
    from normative_agent import compact, model_source_aliases
    result = lookup(corpus)
    supplied = compact(dict(collection_context=result['context']), result['sources'],
                       result['preferred_source_ids'])
    assert supplied['collection_context'] == result['context']
    aliased, aliases = model_source_aliases(supplied, result['sources'])
    for before, after in zip(result['context']['literal_chunks'],
                             aliased['collection_context']['literal_chunks'], strict=True):
        assert before['quote'] == after['quote']
        assert aliases[after['source_id']] == before['source_id']


def test_fixture_bytes_and_metadata_are_verifiable_without_external_kb():
    data = json.loads(FIXTURE.read_text(encoding='utf-8'))
    assert data['source']['sha256'] == data['provenance']['source_pdf_sha256']
    assert data['source']['blob_path'] == 'blobs/' + data['source']['sha256'] + '.pdf'
    for item in data['clauses']:
        assert hashlib.sha256(item['text'].encode()).hexdigest() == item['fixture_text_sha256']
