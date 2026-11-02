"""Offline literal-context contract; no provider, project DB or current KB writes."""
from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import normative_methodology_context as method


FIXTURE = Path(__file__).parent / 'fixtures/methodology-571-reviewed.json'
PIN = dict(database='releases/home-e8f3252a898089a3263415d3/knowledge.sqlite',
           sha256='c01b090e738a277811e2dfb4d5cb70b5c8b64b3dfc02d7039c1632550a0defd4',
           release_id='home-e8f3252a898089a3263415d3', profile='roads')
CLAIMS = [dict(claim_id='method:comment:equipment:1', predicate='text',
               expected_text='Уплотнение основания производится тандемным виброкатком')]


@pytest.fixture
def corpus():
    metadata = json.loads(FIXTURE.read_text(encoding='utf-8'))
    texts = {clause['label']: clause['text'] for clause in metadata['reviewed_clauses']}
    entities = {
        key: dict(id=key, entity_kind='clause', source_id=method.SOURCE_ID,
                  source_kind='METHODOLOGY', designation=method.DESIGNATION, label=label,
                  text=texts[label], status=method.REVIEW_STATUS, edition=deepcopy(metadata),
                  citations=[dict(source_id=method.SOURCE_ID, physical_page=page,
                                  source_sha256=method.SOURCE_SHA256, text_provenance=method.TRANSCRIPTION)
                             for page in pages])
        for label, key, pages, _ in method.CLAUSES}
    kb = SimpleNamespace(identity=deepcopy(PIN), entities=entities, reads=[],
        source_index={method.SOURCE_ID: dict(designation=method.DESIGNATION, pages=27)})

    def entity(kind, key):
        assert kind == 'clause'
        kb.reads.append(key)
        return deepcopy(kb.entities[key])

    kb.entity = entity
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE sources(id TEXT,sha256 TEXT,blob_path TEXT,kind TEXT,designation TEXT,pages INTEGER)')
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)', (method.SOURCE_ID, method.SOURCE_SHA256,
               'blobs/' + method.SOURCE_SHA256 + '.pdf', 'METHODOLOGY', method.DESIGNATION, 27))
    kb.db = db
    kb.connect = lambda: nullcontext(db)
    yield kb
    db.close()


def test_reviewed_literals_keep_full_exceptions_and_do_not_mutate_claims(corpus):
    original = deepcopy(CLAIMS)
    entities = deepcopy(corpus.entities)
    result = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    assert result['reason'] == 'reviewed_literal_context'
    assert len(result['sources']) == len(result['context']['literal_clauses']) == 3
    assert result['context']['kb'] == PIN
    assert result['context']['scope'] == 'estimate_norm_application_only'
    assert result['context']['claim_ids'] == [CLAIMS[0]['claim_id']]
    assert result['preferred_source_ids'] == [source['id'] for source in result['sources']]
    clauses = {item['clause']: item for item in result['context']['literal_clauses']}
    assert clauses['7']['physical_pages'] == [3, 4]
    assert 'за исключением случаев' in clauses['5']['quote']
    assert 'соответствующих сборников сметных норм' in clauses['7']['quote']
    assert 'не меняются технологические и организационные схемы' in clauses['22']['quote']
    assert 'не снижает эксплуатационные характеристики' in clauses['22']['quote']
    assert 'безопасность и потребительские свойства' in clauses['22']['quote']
    assert CLAIMS == original and corpus.entities == entities
    assert not any('state' in value or 'confirmed' in value for value in result['sources'])
    assert all(source['kb_identity'] == PIN for source in result['sources'])


@pytest.mark.parametrize('claims', [[], [{'claim_id': 'quantity'}],
    [{'claim_id': 'attribute:comment:equipment_names', 'predicate': 'advisory'}],
    [{'claim_id': 'method:comment:equipment:1', 'predicate': 'advisory'}]])
def test_non_method_or_advisory_claims_do_not_even_read_corpus(corpus, claims):
    assert not method.methodology_context(corpus, claims, expected_pin=PIN)['sources']
    assert corpus.reads == []


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('release_id', 'another-release'),
    ('database', 'releases/another/knowledge.sqlite'), ('profile', 'electrical')])
def test_wrong_expected_corpus_pin_rejects_before_reading(corpus, field, value):
    pin = dict(PIN, **{field: value})
    result = method.methodology_context(corpus, CLAIMS, expected_pin=pin)
    assert result['reason'] == 'corpus_pin_mismatch' and not result['sources']
    assert corpus.reads == []


def test_additive_release_with_identical_source_is_allowed_but_old_corpus_is_not(corpus):
    corpus.identity = dict(PIN, release_id='home-additive', sha256='a' * 64,
                           database='releases/home-additive/knowledge.sqlite')
    result = method.methodology_context(corpus, CLAIMS, expected_pin=deepcopy(corpus.identity))
    assert result['sources'] and result['context']['kb'] == corpus.identity
    corpus.identity = dict(PIN, release_id='home-70e0a1eed7a19f1d4e4b068b',
                           database='releases/home-70e0a1eed7a19f1d4e4b068b/knowledge.sqlite',
                           sha256='4ebc0da4847b939bc3c3f41e22f2071e54ad4642afabead2c9057b70f5ebfd70')
    corpus.source_index.clear()
    assert not method.methodology_context(corpus, CLAIMS, expected_pin=corpus.identity)['sources']


@pytest.mark.parametrize('mutation', ['source_id', 'source_kind', 'designation', 'status', 'text',
    'metadata_sha', 'metadata_marker', 'metadata_review', 'citation_page', 'citation_sha',
    'citation_review', 'missing_clause'])
def test_any_unverified_or_changed_clause_rejects_entire_set(corpus, mutation):
    key = method.CLAUSES[1][1]  # Losing p7 must never leave p22 behind.
    item = corpus.entities[key]
    if mutation in {'source_id', 'source_kind', 'designation'}:
        item[mutation] = 'other'
    elif mutation == 'status':
        item['status'] = 'ocr_unreviewed'
    elif mutation == 'text':
        item['text'] = item['text'].replace('Не допускается', 'Допускается')
    elif mutation == 'metadata_sha':
        item['edition']['sha256'] = '0' * 64
    elif mutation == 'metadata_marker':
        item['edition']['ingestion'] = 'unreviewed'
    elif mutation == 'metadata_review':
        item['edition']['reviewed_clauses'][1]['verification'] = 'model_guess'
    elif mutation == 'citation_page':
        item['citations'][1]['physical_page'] = 5
    elif mutation == 'citation_sha':
        item['citations'][0]['source_sha256'] = '0' * 64
    elif mutation == 'citation_review':
        item['citations'][0]['text_provenance'] = 'ocr'
    else:
        del corpus.entities[key]
    result = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    assert result['sources'] == result['preferred_source_ids'] == []
    assert result['context'] == {}


def test_identity_switch_during_reads_never_returns_mixed_corpus_context(corpus):
    original = corpus.entity

    def switched(kind, key):
        result = original(kind, key)
        corpus.identity = dict(PIN, sha256='f' * 64)
        return result

    corpus.entity = switched
    assert not method.methodology_context(corpus, CLAIMS, expected_pin=PIN)['sources']


@pytest.mark.parametrize('column,value', [('sha256', '0' * 64), ('blob_path', 'blobs/other.pdf'),
                                         ('kind', 'GOST')])
def test_metadata_cannot_hide_reassigned_source_row(corpus, column, value):
    corpus.db.execute('UPDATE sources SET ' + column + '=?', (value,))
    result = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    assert result['reason'] == 'reviewed_source_identity_mismatch'
    assert result['sources'] == [] and corpus.reads == []


def test_literal_context_survives_exhausted_source_budget_and_aliases_resolve(corpus, monkeypatch):
    from normative_agent import compact, model_source_aliases
    from normative_context_packing import serialize_context
    result = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    sources = [dict(id='p', kind='project', text='Проектная конструкция'), *result['sources']]
    inputs = dict(methodology_context=result['context'], untrusted_other_data='x' * 38000)
    # Exhaust the actual measured budget, independently of preview policy. The
    # former fixed 20k reserve must not return to make this test pass.
    metadata = dict(inputs, sources=[], archived_source_ids=['p'], matching_checks=[], pipeline_version=4)
    monkeypatch.setattr('normative_agent.MAX_CONTEXT', len(serialize_context(metadata)))
    supplied = compact(inputs, sources, result['preferred_source_ids'])
    assert supplied['methodology_context'] == result['context']
    assert len(supplied['sources']) == 0  # All three literal clauses still remain in metadata.
    model_input, aliases = model_source_aliases(supplied, sources)
    for original, sent in zip(result['context']['literal_clauses'], model_input['methodology_context']['literal_clauses'], strict=True):
        assert sent['quote'] == original['quote']
        assert aliases[sent['source_id']] == original['source_id']


def test_context_and_duplicate_registry_fit_explicit_budget_no_silent_truncation(corpus, monkeypatch):
    result = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    assert result['sources']
    serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    assert len(serialized) <= method.MAX_ADDITION_CHARS
    assert len(serialized.encode()) <= method.MAX_ADDITION_UTF8_BYTES
    monkeypatch.setattr(method, 'MAX_ADDITION_UTF8_BYTES', 100)
    rejected = method.methodology_context(corpus, CLAIMS, expected_pin=PIN)
    assert rejected['reason'] == 'methodology_context_budget_exceeded'
    assert rejected['sources'] == [] and rejected['context'] == {}


def test_large_claim_list_skips_whole_addition_instead_of_truncating_requirements(corpus):
    claims = [dict(claim_id=f'method:comment:equipment:{index}') for index in range(13)]
    result = method.methodology_context(corpus, claims, expected_pin=PIN)
    assert result['reason'] == 'method_claim_budget_exceeded' and not result['sources']
    assert len(claims) == 13 and corpus.reads == []
