"""Document selection must retain amendments and explicit historical editions."""
import json
import sqlite3

import pytest

from normative_corpus import Corpus


@pytest.fixture
def corpus(tmp_path):
    db = sqlite3.connect(tmp_path / 'knowledge.sqlite')
    db.executescript('''
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE sources(id TEXT,designation TEXT,kind TEXT,pages INTEGER,metadata_json TEXT);
        CREATE TABLE norms(id TEXT);
        CREATE TABLE rules(id TEXT);
        CREATE TABLE clauses(id TEXT,source_id TEXT,label TEXT,title TEXT,text TEXT,citations_json TEXT,status TEXT);
        CREATE TABLE chunks(id TEXT,source_id TEXT,entity_type TEXT,entity_id TEXT);
        CREATE VIRTUAL TABLE search_fts USING fts5(chunk_id UNINDEXED,title,text);
    ''')
    for key,value in {'release_id':'synthetic','profile':'roads','gesn_scope':[]}.items():
        db.execute('INSERT INTO metadata VALUES(?,?)',(key,json.dumps(value)))
    for sid,designation,label,body in [
        ('base','СП 78.13330.2012','10.18','Плотность и уплотнение основания'),
        ('change','СП 78.13330.2012 Изм.2','10.18','Коэффициент уплотнения основания изменён'),
        ('old','ГОСТ Р 52056-2003','5.1','Историческая редакция вяжущего'),
        ('new','ГОСТ Р 52056-2025','5.1','Новая редакция вяжущего'),
    ]:
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(sid,designation,'SP' if sid in ('base','change') else 'GOST',1,'{}'))
        db.execute('INSERT INTO clauses VALUES(?,?,?,?,?,?,?)',(sid,sid,label,label,body,'[]','extracted'))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(sid,sid,'clause',sid))
        db.execute('INSERT INTO search_fts VALUES(?,?,?)',(sid,label,body))
    db.commit()
    db.close()
    (tmp_path/'CURRENT.json').write_text(json.dumps({'database':'knowledge.sqlite'}))
    return Corpus(tmp_path)


def test_explicit_document_clause_search_includes_base_and_amendment(corpus):
    results=corpus.search('СП 78.13330.2012 п. 10.18',kind='ALL')
    assert {v['id'] for v in results[:2]} == {'base','change'}


def test_abbreviated_designation_and_russian_inflection_find_supplement(corpus):
    assert 'change' in {v['id'] for v in corpus.search('СП 78 уплотнение',kind='ALL')}


def test_explicit_old_edition_is_not_replaced_by_current_edition(corpus):
    assert {v['id'] for v in corpus.search('ГОСТ Р 52056-2003 пункт 5.1',kind='ALL')} == {'old'}
    assert {v['id'] for v in corpus.search('ГОСТ Р 52056-2025 пункт 5.1',kind='ALL')} == {'new'}
    assert corpus.search('ГОСТ Р 999999-2025 пункт 5.1',kind='ALL') == []


def test_empty_rate_scope_is_valid_and_does_not_assume_electrical_collection(corpus):
    assert corpus.gesn_scope == []
    assert corpus.search('01-01-001-01',kind='GESN') == []
