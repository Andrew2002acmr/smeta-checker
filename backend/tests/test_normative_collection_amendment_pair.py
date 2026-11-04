"""Actual immutable excerpt pair; no consolidation or applicability promotion."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

import normative_collection_context as context


@pytest.fixture
def corpus():
    fixtures = Path(__file__).parent / 'fixtures'
    base = json.loads((fixtures / 'collection-74-native.json').read_text(encoding='utf8'))
    pair = json.loads((fixtures / 'collection-road102-pair.json').read_text(encoding='utf8'))
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE sources(id TEXT,designation TEXT,kind TEXT,pages INTEGER,sha256 TEXT,blob_path TEXT,metadata_json TEXT)')
    db.execute('CREATE TABLE clauses(id TEXT,source_id TEXT,text TEXT)')
    for source in [base['source'], pair['source']]:
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?)', tuple(source[k] for k in
            ('id', 'designation', 'kind', 'pages', 'sha256', 'blob_path', 'metadata_json')))
    clauses = [*base['clauses'], *pair['clauses']]
    for item in clauses:
        db.execute('INSERT INTO clauses VALUES(?,?,?)', (item['id'], item['source_id'], item['text']))
    entities = {item['id']: deepcopy(item) for item in [*base['norms'], *clauses]}
    kb = SimpleNamespace(identity=pair['kb_identity'], db=db, entities=entities,
        candidates=deepcopy(base['norms']), connect=lambda: nullcontext(db))
    kb.entity = lambda kind, key: deepcopy(entities[key])
    yield kb
    db.close()


def lookup(kb):
    return context.collection_context(kb, kb.candidates, expected_pin=deepcopy(kb.identity))


def amendment_cards(result):
    return [c for c in result['sources'] if c.get('amendment_navigation')]


def metadata(kb):
    return json.loads(kb.db.execute('SELECT metadata_json FROM sources WHERE id=?',
                                  (context.ROAD_ORDER_ID,)).fetchone()[0])


def set_metadata(kb, value):
    kb.db.execute('UPDATE sources SET metadata_json=? WHERE id=?',
                  (json.dumps(value), context.ROAD_ORDER_ID))
    for item in kb.entities.values():
        if item.get('source_id') == context.ROAD_ORDER_ID:
            item['edition'] = deepcopy(value)


def test_actual_pair_whole_literals_old_context_retained_and_no_edition_promotion(corpus):
    before = deepcopy(corpus.candidates)
    result = lookup(corpus)
    cards = amendment_cards(result)
    assert len(cards) == 2
    assert {c['page'] for c in cards} == {222, 243}
    for card in cards:
        assert card['text'] == corpus.entities[card['clause_id']]['text']
        assert card['kb_identity'] == corpus.identity
        assert card['automatic_application_allowed'] is False
        assert card['amendment_navigation']['target_edition_state'] == 'unresolved'
        assert any(x['source_id'] == card['id'] and x['quote'] == card['text']
                   for x in result['context']['literal_chunks'])
    assert any(c['source_sha256'] == context.ROAD_BASE_SHA for c in result['sources'])
    assert cards[0]['text'].count('В случае отсутствия проектных данных') == 2
    assert 'проектных данных норма расхода' in cards[0]['text']
    assert 'Дополнить приложением 27.1' in cards[1]['text']
    assert 'ЩМА-8' in cards[1]['text'] and 'SMA-8' in cards[1]['text']
    assert 'Соответствие' in result['context']['addressed_amendment_instruction']
    assert result['context']['addressed_amendment_navigation']['source_ids'] == [c['id'] for c in cards]
    assert corpus.candidates == before
    assert result['context']['coverage_complete'] is False


@pytest.mark.parametrize('code,expected', [
    ('27-06-029-01', True), ('27-06-030-01', True), ('27-06-031-01', True),
    ('27-06-032-02', True), ('27-06-030-02', False), ('27-06-069-01', False),
    ('27-07-006-01', False), ('27-06-0310-01', False),
])
def test_literal_scopes_do_not_extend_fallback_to_gutter_sidewalk_or_similar_code(corpus, code, expected):
    corpus.candidates = corpus.candidates[:1]
    norm = corpus.candidates[0]
    norm.update(code=code, table_code=code.rsplit('-', 1)[0])
    corpus.entities[norm['id']] = deepcopy(norm)
    assert bool(amendment_cards(lookup(corpus))) is expected


@pytest.mark.parametrize('label,entity_id,page,text_sha', context.ROAD_PAIR)
def test_missing_or_changed_either_half_never_delivers_other_half(corpus, label, entity_id, page, text_sha):
    corpus.db.execute('DELETE FROM clauses WHERE id=?', (entity_id,))
    assert not amendment_cards(lookup(corpus))
    assert any(c['source_sha256'] == context.ROAD_BASE_SHA for c in lookup(corpus)['sources'])


@pytest.mark.parametrize('field,value', [('sha256', 'f' * 64), ('kind', 'FER'),
    ('pages', 763), ('blob_path', 'blobs/other.pdf'), ('designation', 'unrelated edition')])
def test_forged_source_identity_cannot_enable_navigation(corpus, field, value):
    corpus.db.execute(f'UPDATE sources SET {field}=? WHERE id=?', (value, context.ROAD_ORDER_ID))
    assert not amendment_cards(lookup(corpus))


@pytest.mark.parametrize('mutation', ['review', 'review_flag', 'amends', 'supersedes', 'source_sha'])
def test_forged_review_or_proposed_relation_metadata_fails_closed(corpus, mutation):
    value = metadata(corpus)
    if mutation == 'review':
        value['independent_verification']['paragraph']['text'] += ' permissive invention'
    elif mutation == 'review_flag':
        value['independent_verification']['passed'] = False
    elif mutation == 'amends':
        value['amends_source_ids'] = ['unrelated']
    elif mutation == 'supersedes':
        value['supersedes_labels'] = ['27.1']
    else:
        value['sha256'] = 'e' * 64
    set_metadata(corpus, value)
    assert not amendment_cards(lookup(corpus))


def test_literal_mutation_even_with_updated_database_and_metadata_cannot_pass(corpus):
    entity_id = context.ROAD_PAIR[0][1]
    item = corpus.entities[entity_id]
    item['text'] = item['text'].replace('4 см', '8 см')
    corpus.db.execute('UPDATE clauses SET text=? WHERE id=?', (item['text'], entity_id))
    assert not amendment_cards(lookup(corpus))


def test_unrelated_book_same_codes_and_forged_relation_never_followed(corpus):
    source_id = corpus.candidates[0]['source_id']
    corpus.db.execute('UPDATE sources SET sha256=? WHERE id=?', ('b' * 64, source_id))
    assert not amendment_cards(lookup(corpus))


def test_pin_and_budget_cannot_be_bypassed_by_amendment_pair(corpus, monkeypatch):
    assert context.collection_context(corpus, corpus.candidates,
                                     expected_pin=dict(corpus.identity, sha256='e' * 64))['sources'] == []
    monkeypatch.setattr(context, 'MAX_ADDITION_CHARS', 100)
    result = lookup(corpus)
    assert not result['sources']
    assert result['reason'] == 'collection_context_budget_exceeded'


def test_actual_packing_aliases_preserve_complete_pair_and_review_binding(corpus):
    from normative_agent import compact, model_source_aliases
    from normative_context_packing import serialize_context
    result = lookup(corpus)
    inputs = dict(collection_context=result['context'])
    supplied = compact(inputs, result['sources'], result['preferred_source_ids'])
    aliased_raw, reverse = model_source_aliases(supplied, result['sources'])
    aliased = cast(dict[str, Any], aliased_raw)
    assert len(serialize_context(aliased)) <= 58000
    binding = aliased['collection_context']['addressed_amendment_navigation']
    assert binding['amendment_source_sha256'] == context.ROAD_ORDER_SHA
    assert binding['independent_review_sha256'] == context.ROAD_REVIEW_SHA
    assert len(binding['source_ids']) == 2
    for handle in binding['source_ids']:
        sid = reverse[handle]
        card = next(c for c in result['sources'] if c['id'] == sid)
        assert any(c['source_id'] == handle and c['quote'] == card['text']
                   for c in aliased['collection_context']['literal_chunks'])
