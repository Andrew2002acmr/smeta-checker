"""Raw retrieval caching must not cache applicability or reduce alternatives."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3
import threading

import pytest

from normative_corpus import Corpus


def database(root, name='one.sqlite', release='one', marker='first'):
    path=root/name
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE metadata(key TEXT,value TEXT);
            CREATE TABLE sources(id TEXT,designation TEXT,kind TEXT,pages INTEGER,metadata_json TEXT);
            CREATE TABLE norms(id TEXT,source_id TEXT,family TEXT,code TEXT,table_code TEXT,title TEXT,
                text TEXT,unit_json TEXT,attributes_json TEXT,citations_json TEXT,status TEXT);
            CREATE TABLE resources(id TEXT,norm_id TEXT,name TEXT,citations_json TEXT);
            CREATE TABLE norm_tables(source_id TEXT,family TEXT,code TEXT,operations_json TEXT);
            CREATE TABLE rules(id TEXT,title TEXT,scope_json TEXT,condition_json TEXT,effects_json TEXT,citations_json TEXT);
            CREATE TABLE clauses(id TEXT,source_id TEXT,label TEXT,title TEXT,text TEXT,citations_json TEXT,status TEXT);
            CREATE TABLE chunks(id TEXT,source_id TEXT,entity_type TEXT,entity_id TEXT);
            CREATE VIRTUAL TABLE search_fts USING fts5(chunk_id UNINDEXED,title,text);
        ''')
        for key,value in {'release_id':release,'profile':'roads','gesn_scope':[['ГЭСН','27']]}.items():
            db.execute('INSERT INTO metadata VALUES(?,?)',(key,json.dumps(value)))
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',('s','ГЭСН 27','GESN',1,'{}'))
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',('sp','СП 78.13330.2012','SP',1,'{}'))
        db.execute('INSERT INTO norm_tables VALUES(?,?,?,?)',('s','ГЭСН','27-01-001','["operation"]'))
        db.execute('INSERT INTO rules VALUES(?,?,?,?,?,?)',('rule','conditional',
            json.dumps({'family':'ГЭСН','collection':'27'}),
            json.dumps({'field':'mass_kg_per_m','op':'gt','value':10}),'{"factor":1.03}','[]'))
        for i in range(85):
            identity=f'n{i:03}'
            db.execute('INSERT INTO norms VALUES(?,?,?,?,?,?,?,?,?,?,?)',(
                identity,'s','ГЭСН',f'27-01-001-{i:02}','27-01-001','Основание',marker,
                json.dumps({'status':'parsed','unit':'м2','raw':'м2','scale':1}),
                json.dumps({'mass_kg_per_m_max':10 if i%2==0 else 30}),'[]','definition_reviewed'))
            db.execute('INSERT INTO resources VALUES(?,?,?,?)',(f'r{i}',identity,'material','[{"page":1}]'))
            db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(identity,'s','norm',identity))
            db.execute('INSERT INTO search_fts VALUES(?,?,?)',(identity,'Основание','Устройство основания'))
        db.execute('INSERT INTO clauses VALUES(?,?,?,?,?,?,?)',('cl','sp','1.2','Правило','Основание','[]','extracted'))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?)',('cl','sp','clause','cl'))
        db.execute('INSERT INTO search_fts VALUES(?,?,?)',('cl','Правило','Основание'))
    (root/'CURRENT.json').write_text(json.dumps({'database':name}))
    return path


@pytest.fixture
def kb(tmp_path):
    database(tmp_path)
    return Corpus(tmp_path)


def fresh_search(kb, query, **kwargs):
    fresh=Corpus(kb.root,relative=kb.relative,expected_sha=kb.sha)
    return fresh.search(query,**kwargs)


def test_cached_pool_keeps_all_80_alternatives_and_rechecks_facts_and_rules(kb):
    for mass in (5,20,None,50):
        facts=dict(unit='м2',quantity='10',mass_kg_per_m=mass)
        result=kb.search('основание',facts=facts,limit=100)
        assert result==fresh_search(kb,'основание',facts=facts,limit=100)
        assert len(result)==80
        if mass==20:
            assert result[0]['attributes']['mass_kg_per_m_max']==30
        expected='needs_facts' if mass is None else 'not_applicable' if mass<=10 else 'candidate'
        assert {rule['state'] for item in result for rule in item['rules']}=={expected}
    assert kb._cached_search_pool.cache_info().misses==1
    assert kb._cached_search_pool.cache_info().hits==3


def test_exact_code_does_not_drop_fts_alternatives_or_change_their_order(kb):
    query='27-01-001-84 основание'
    first=kb.search(query,limit=200)
    assert len(first)==81 and first[0]['id']=='n084' and first[0]['exact_code']
    assert first==kb.search(query,limit=200)==fresh_search(kb,query,limit=200)
    assert {item['id'] for item in first}=={'n084',*(f'n{i:03}' for i in range(80))}


@pytest.mark.parametrize('query,kind',[
    ('СП 78.13330.2012 п. 1.2','ALL'),('СП 99999.123.2020','ALL'),
    ('несуществующий','GESN'),('','GESN'),('01-01-001-01','GESN'),
])
def test_document_scope_exact_clause_and_empty_results_are_identical(kb,query,kind):
    expected=fresh_search(kb,query,kind=kind)
    assert kb.search(query,kind=kind)==expected
    assert kb.search(query,kind=kind)==expected


def test_returned_mutable_entities_are_never_part_of_the_cache(kb):
    expected=deepcopy(kb.search('основание',facts={'unit':'м2'},limit=3))
    altered=kb.search('основание',facts={'unit':'м2'},limit=3)
    altered[0]['resources'][0]['citations'][0]['page']=999
    altered[0]['attributes'].clear()
    altered[0]['checks'].clear()
    altered[0]['rules'][0]['effect']['factor']=999
    assert kb.search('основание',facts={'unit':'м2'},limit=3)==expected
    exact,ids=kb._cached_search_pool('основание','GESN',(('ГЭСН','27'),))
    assert isinstance(exact,tuple) and isinstance(ids,tuple) and all(isinstance(i,str) for i in ids)


def test_two_pinned_releases_with_same_entity_ids_do_not_share_cache(tmp_path):
    one=database(tmp_path)
    expected_sha=hashlib.sha256(one.read_bytes()).hexdigest()
    first=Corpus(tmp_path,relative=one.name,expected_sha=expected_sha)
    database(tmp_path,'two.sqlite','two','second')
    second=Corpus(tmp_path,relative='two.sqlite')
    assert first.search('основание')[0]['text']=='first'
    assert second.search('основание')[0]['text']=='second'
    assert first.search('основание')[0]['text']=='first'
    assert first.sha!=second.sha and first.release!=second.release
    assert first._cached_search_pool.cache_info().hits==1
    assert second._cached_search_pool.cache_info().hits==0


def test_cache_is_bounded_and_scope_is_part_of_the_key(kb):
    for i in range(260):
        assert kb.search(f'неизвестно{i}')==[]
    info=kb._cached_search_pool.cache_info()
    assert info.currsize==info.maxsize==256
    assert kb.search('основание')
    kb.gesn_scope=[]
    assert kb.search('основание')==[]


def test_two_threads_share_only_immutable_ids_not_sqlite_connections(kb,monkeypatch):
    connect=kb.connect
    active={}
    lock=threading.Lock()
    connections=[]
    barrier=threading.Barrier(2)
    @contextmanager
    def tracked():
        with connect() as db:
            identity=id(db)
            with lock:
                assert identity not in active
                active[identity]=threading.get_ident()
                connections.append((identity,threading.get_ident()))
            try:
                yield db
            finally:
                with lock:
                    del active[identity]
    monkeypatch.setattr(kb,'connect',tracked)
    def worker(mass):
        barrier.wait(timeout=5)
        facts=dict(unit='м2',mass_kg_per_m=mass)
        return kb.search('основание',facts=facts,limit=100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(worker,5)
        b=pool.submit(worker,20)
        first,second=a.result(),b.result()
    assert first==fresh_search(kb,'основание',facts=dict(unit='м2',mass_kg_per_m=5),limit=100)
    assert second==fresh_search(kb,'основание',facts=dict(unit='м2',mass_kg_per_m=20),limit=100)
    assert len({thread for _,thread in connections})==2 and not active
