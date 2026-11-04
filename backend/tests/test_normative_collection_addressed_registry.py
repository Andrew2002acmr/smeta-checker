"""Immutable v11 literals replayed in isolated memory; no external corpus required."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

import normative_collection_context as context
from normative_corpus import decode


@pytest.fixture
def corpus():
    fixture = json.loads((Path(__file__).parent / 'fixtures' /
        'collection-road-v11-addressed.json').read_text(encoding='utf8'))
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    for table, value in fixture['tables'].items():
        db.execute(value['schema'])
        for row in value['rows']:
            db.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+
                ','.join('?' for _ in row)+')', tuple(row.values()))
    kb = SimpleNamespace(identity=fixture['identity'], db=db, connect=lambda: nullcontext(db))
    def entity(kind, key):
        table = {'norm':'norms', 'clause':'clauses'}[kind]
        row = db.execute('SELECT e.*, s.designation,s.kind AS source_kind, '
            's.metadata_json AS edition_json FROM '+table+' e JOIN sources s '
            'ON s.id=e.source_id WHERE e.id=?', (key,)).fetchone()
        if not row:
            raise KeyError(key)
        item = decode(row)
        item['entity_kind'] = kind
        if kind == 'norm':
            item['resources'] = []
        return item
    kb.entity = entity
    yield kb
    db.close()


def candidates(kb, codes=('27-06-031-01','27-06-032-01'), source=None):
    source = source or 'pdf-'+context.ROAD_BASE_SHA[:24]
    return [kb.entity('norm', kb.db.execute('SELECT id FROM norms WHERE source_id=? AND code=?',
        (source, code)).fetchone()[0]) for code in codes]


def lookup(kb, **kwargs):
    return context.collection_context(kb, candidates(kb, **kwargs), expected_pin=deepcopy(kb.identity))


def registry(kb):
    return json.loads(kb.db.execute("SELECT value FROM metadata WHERE key='addressed_collection_context_v1'").fetchone()[0])


def assert_closed(kb):
    result = lookup(kb)
    assert result['sources'] == []
    assert result['reason'] == 'addressed_registry_invalid'
    assert result['warnings'] == ['addressed_registry_invalid_no_newest_navigation']


@pytest.mark.parametrize('source', ['pdf-'+context.ROAD_BASE_SHA[:24], context.ROAD_378_ID])
def test_real_v11_atomic_latest_pair_original_appendix_and_full_definitions(corpus, source):
    result = lookup(corpus, source=source)
    assert result['reason'] == 'literal_collection_context'
    cards = result['sources']
    record = registry(corpus)['registry']
    source69 = next(s['source_id'] for s in record['sources'] if s['key']=='69')
    current = [c for c in cards if c.get('amendment_navigation', {}).get('kind') == 'addressed_literal']
    assert {c['clause_id'] for c in current} == {c['id'] for c in record['clauses']
        if c['source_id']==source69 and c['label'] in {'1.27.35','1.27.37'}} | {context.ROAD_PAIR[1][1]}
    for card in cards:
        kind = 'norm' if 'norm_id' in card else 'clause'
        assert card['text'] == corpus.entity(kind, card[kind+'_id'])['text']
        assert card['automatic_application_allowed'] is False
    defs = [c for c in cards if c.get('norm_id')]
    assert len(defs) == (0 if source==context.ROAD_378_ID else 2)
    if source==context.ROAD_378_ID:
        assert len(result['context']['addressed_amendment_navigation']['definition_equivalences'])==2
    assert all(c['source_id']==context.ROAD_378_ID and 'Состав работ:' in c['text'] for c in defs)
    assert all('resources' not in c for c in defs)
    assert all(c['origin']=='extracted_native_unreviewed' for c in defs)
    nav = result['context']['addressed_amendment_navigation']
    assert nav['last_reviewed_addressed_date']=='2025-02-07'
    assert nav['resources_aliased'] is False
    assert nav['target_edition_state']=='not_fully_consolidated'
    assert result['context']['coverage_complete'] is False
    # 747 is retained in the registry/database, but never selected as current.
    assert not any(c['source_id']==next(s['source_id'] for s in record['sources'] if s['key']=='747') for c in cards)
    raw = json.dumps(result, ensure_ascii=False, separators=(',',':'))
    assert len(raw)<=20000 and len(raw.encode())<=35000


def test_omits_only_entire_carriers_with_all_matched_labels_replaced(corpus):
    result = lookup(corpus)
    omitted = result['context']['addressed_amendment_navigation']['omitted_historical_clause_ids']
    assert omitted
    for key in omitted:
        matches = context._matches(corpus.entity('clause', key), candidates(corpus))
        assert matches and all(m['label'] in {'1.27.35','1.27.37'} for m in matches)
    # Independent 1.27.74 retains its whole literal carrier.
    assert any('1.27.74.' in c['text'] for c in result['sources'])
    # Add a separate matched paragraph to an otherwise omittable carrier.
    key = omitted[0]
    corpus.db.execute('UPDATE clauses SET text=text||? WHERE id=?',
        ('\n1.27.999. Для норм табл. 27-06-031 требуется отдельное условие.\n1.27.1000. Следующий пункт.', key))
    result = lookup(corpus, codes=('27-06-031-01',))
    if result['sources']:
        assert key not in result['context']['addressed_amendment_navigation']['omitted_historical_clause_ids']
        assert any(c.get('clause_id')==key and '1.27.999.' in c['text'] for c in result['sources'])
    else:
        assert result['reason']=='collection_context_budget_exceeded'


@pytest.mark.parametrize('mutation', [
    'registry_digest','registry_body','source_sha','source_pages','source_kind','source_metadata',
    'old_source_sha','clause_text','clause_status','clause_citations','missing35','missing37',
    'appendix_text','appendix_source','definition_text','definition_citations','definition_unit',
    'definition_composition','definition_attributes','old_definition',
])
def test_actual_rows_and_native_proof_fail_closed_without_legacy_fallback(corpus, mutation):
    wrapper = registry(corpus)
    r = wrapper['registry']
    source = r['sources'][0]['source_id']
    clause = next(c['id'] for c in r['clauses'] if c['label']=='1.27.35' and
        c['source_id']==next(s['source_id'] for s in r['sources'] if s['key']=='69'))
    definition = r['norm_definitions'][1]['id']
    if mutation.startswith('registry'):
        if mutation=='registry_digest':
            wrapper['registry_sha256']='f'*64
        else:
            wrapper['registry']['sources'][0]['verified']=True
        corpus.db.execute("UPDATE metadata SET value=? WHERE key='addressed_collection_context_v1'", (json.dumps(wrapper),))
    elif mutation.startswith('source_') or mutation=='old_source_sha':
        if mutation=='old_source_sha':
            source='pdf-'+context.ROAD_BASE_SHA[:24]
        column, value = {'source_sha':('sha256','f'*64),'old_source_sha':('sha256','f'*64),
            'source_pages':('pages',1),'source_kind':('kind','GOST'),
            'source_metadata':('metadata_json','{"verified":true}') }[mutation]
        corpus.db.execute('UPDATE sources SET '+column+'=? WHERE id=?',(value,source))
    elif mutation.startswith('clause_'):
        column,value={'clause_text':('text','1.27.35. forged'),
            'clause_status':('status','verified'),'clause_citations':('citations_json','[]')}[mutation]
        corpus.db.execute('UPDATE clauses SET '+column+'=? WHERE id=?',(value,clause))
    elif mutation.startswith('missing'):
        label='1.27.'+mutation[-2:]
        corpus.db.execute('DELETE FROM clauses WHERE id=?',(next(c['id'] for c in r['clauses']
            if c['label']==label and c['source_id']==next(s['source_id'] for s in r['sources'] if s['key']=='69')),))
    elif mutation=='appendix_text':
        corpus.db.execute('UPDATE clauses SET text=text||? WHERE id=?',(' changed', context.ROAD_PAIR[1][1]))
    elif mutation=='appendix_source':
        corpus.db.execute('UPDATE sources SET pages=1 WHERE id=?',(context.ROAD_ORDER_ID,))
    elif mutation=='definition_composition':
        corpus.db.execute("UPDATE norm_tables SET operations_json='[]' WHERE source_id=?",(context.ROAD_378_ID,))
    elif mutation=='old_definition':
        corpus.db.execute('UPDATE norms SET text=text||? WHERE id=?',(' changed',r['definition_equivalences'][1]['old_norm_id']))
    else:
        column,value={'definition_text':('text','Измеритель: 1000 м2'),
            'definition_citations':('citations_json','[]'),
            'definition_attributes':('attributes_json','{"verified":true}'),
            'definition_unit':('unit_json','{"raw":"1000 м2","status":"parsed","scale":"1","unit":"м2"}') }[mutation]
        corpus.db.execute('UPDATE norms SET '+column+'=? WHERE id=?',(value,definition))
    result = lookup(corpus)
    assert result['sources']==[]
    assert result['reason'] in {'addressed_registry_invalid','corpus_source_identity_mismatch'}
    assert not any(c.get('amendment_navigation') for c in result['sources'])


def test_new_metadata_missing_retains_legacy_pair(corpus):
    corpus.db.execute("DELETE FROM metadata WHERE key='addressed_collection_context_v1'")
    result = lookup(corpus, codes=('27-06-032-01',))
    assert result['context']['addressed_amendment_navigation']['target_edition_state']=='unresolved'
    assert {c.get('clause_id') for c in result['sources'] if c.get('amendment_navigation')} == {x[1] for x in context.ROAD_PAIR}


def test_complete_quotes_survive_actual_compact_and_aliases(corpus):
    from normative_agent import compact, model_source_aliases
    result = lookup(corpus)
    supplied = compact(dict(collection_context=result['context']), result['sources'],result['preferred_source_ids'])
    aliased_raw, aliases = model_source_aliases(supplied, result['sources'])
    aliased = cast(dict[str, Any], aliased_raw)
    for before, after in zip(result['context']['literal_chunks'],aliased['collection_context']['literal_chunks'],strict=True):
        assert before['quote']==after['quote']
        assert aliases[after['source_id']]==before['source_id']
    for card in result['sources']:
        preview = next(s for s in supplied['sources'] if s['id']==card['id'])
        assert preview['embedded_text_complete'] is True
        assert preview['text_embedded_at']
        assert preview['full_length']==len(card['text'])

@pytest.mark.parametrize('source', ['pdf-'+context.ROAD_BASE_SHA[:24]])
@pytest.mark.parametrize('code', ['27-06-029-01','27-06-030-01'])
def test_navigation_does_not_invent_definition_equivalence_for_029_030(corpus, source, code):
    result = lookup(corpus, codes=(code,), source=source)
    assert result['reason']=='literal_collection_context'
    assert len([c for c in result['sources'] if c.get('amendment_navigation')])==3
    assert not any(c.get('norm_id') for c in result['sources'])


@pytest.mark.parametrize('source', ['pdf-'+context.ROAD_BASE_SHA[:24]])
def test_gutter_not_admitted_to_new_pair(corpus, source):
    result = lookup(corpus, codes=('27-06-030-02',), source=source)
    assert not any(c.get('amendment_navigation') for c in result['sources'])
    assert 'addressed_amendment_navigation' not in (result['context'] or {})


def test_invalid_json_metadata_cannot_trigger_legacy(corpus):
    corpus.db.execute("UPDATE metadata SET value='not json' WHERE key='addressed_collection_context_v1'")
    assert_closed(corpus)


@pytest.mark.parametrize('count', [3,4])
def test_real_mixed_search_pool_keeps_atomic_context_and_initial_full_definitions(corpus, count):
    from normative_agent import compact, model_source_aliases
    from normative_source_registry import full_text_handle
    norms = (candidates(corpus) + candidates(corpus, source=context.ROAD_378_ID))[:count]
    result = context.collection_context(corpus, norms, expected_pin=corpus.identity)
    assert result['reason']=='literal_collection_context'
    nav = result['context']['addressed_amendment_navigation']
    assert nav['definition_equivalences']
    assert {e['norm_id'] for e in nav['definition_equivalences']} == {
        n['id'] for n in norms if n['source_id']==context.ROAD_378_ID}
    # Same source construction as initial NPC, whose exact candidates already
    # contain complete checked378 definitions. Resources are never copied here.
    initial = [dict(id=full_text_handle('n'+str(i),n['text']),kind='normative',
        label=n['designation']+' · '+n['code'],source_id=n['source_id'],
        page=n['citations'][0]['physical_page'],text=n['text'],origin=n['status'])
        for i,n in enumerate(norms)]
    sources = [*initial,*result['sources']]
    assert len({s['id'] for s in sources})==len(sources)
    assert not any(c.get('norm_id') in {n['id'] for n in norms} for c in result['sources'])
    packed = compact(dict(collection_context=result['context']),sources,result['preferred_source_ids'])
    aliased_raw, aliases = model_source_aliases(packed,sources)
    aliased = cast(dict[str,Any],aliased_raw)
    assert len([c for c in result['sources'] if c.get('amendment_navigation',{}).get('kind')=='addressed_literal'])==3
    for before,after in zip(result['context']['literal_chunks'],aliased['collection_context']['literal_chunks'],strict=True):
        assert before['quote']==after['quote']
        assert aliases[after['source_id']]==before['source_id']
    for source in initial:
        assert next(s for s in packed['sources'] if s['id']==source['id'])['text']==source['text']
    raw=json.dumps(result,ensure_ascii=False,separators=(',',':'))
    assert len(raw)<=20000 and len(raw.encode())<=35000
    assert context.MAX_ADDITION_CHARS==20000 and context.MAX_ADDITION_UTF8_BYTES==35000


def test_duplicate_handles_require_exact_evidence_envelope_and_merge_navigation_only():
    cards=[dict(id='x',text='FULL literal',origin='native')]*2
    literals: list[dict[str,Any]] = [dict(source_id='x',quote='FULL literal',physical_pages=[3],
        matched_paragraphs=[dict(label='1.27.35',candidate_ids=[candidate],references=[reference])])
        for candidate,reference in [('old','031'),('new','032')]]
    unique,chunks=context._deduplicate_delivery(cards,literals)
    assert len(unique)==len(chunks)==1
    assert chunks[0]['quote']=='FULL literal'
    assert chunks[0]['matched_paragraphs']==[dict(label='1.27.35',candidate_ids=['old','new'],references=['031','032'])]
    assert literals[0]['matched_paragraphs'][0]['candidate_ids']==['old']
    with pytest.raises(ValueError,match='envelope_conflict'):
        context._deduplicate_delivery([cards[0],dict(cards[0],origin='ocr')],literals)
    with pytest.raises(ValueError,match='envelope_conflict'):
        context._deduplicate_delivery([dict(cards[0],numeric_verified=True),
            dict(cards[0],numeric_verified=1)],literals)


def test_real_extra_unrelated_carrier_overflow_is_not_cropped(corpus):
    # Replaced historical carriers containing another matched paragraph must be
    # kept whole even when this genuine extra context exceeds the fixed budget.
    result=lookup(corpus)
    key=result['context']['addressed_amendment_navigation']['omitted_historical_clause_ids'][0]
    corpus.db.execute('UPDATE clauses SET text=text||? WHERE id=?',
        ('\n1.27.999. Для норм табл. 27-06-031 '+('полное условие '*100)+'.\n1.27.1000. Далее.',key))
    result=lookup(corpus)
    assert result['reason']=='collection_context_budget_exceeded'
    assert result['sources']==[]


def test_incomplete_supplied_candidate_does_not_suppress_checked_full_definition(corpus):
    norms=candidates(corpus,source=context.ROAD_378_ID,codes=('27-06-031-01',))
    norms[0]['text']='incomplete preview'
    result=context.collection_context(corpus,norms,expected_pin=corpus.identity)
    assert result['reason']=='literal_collection_context'
    card=next(s for s in result['sources'] if s.get('norm_id')==norms[0]['id'])
    assert card['text']==corpus.entity('norm',norms[0]['id'])['text']
    assert 'definition_equivalences' not in result['context']['addressed_amendment_navigation']
