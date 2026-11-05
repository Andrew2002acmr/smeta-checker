import hashlib
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from normative_source_preparation import prepare_source, stage_release
from test_pdf_inline_evidence import mixed_pdf


def fixture_corpus(tmp_path, *, multipage=False):
    path = tmp_path / 'source.pdf'
    with mixed_pdf() as doc:
        if multipage:
            with mixed_pdf() as second:
                doc.insert_pdf(second)
            doc[1].insert_text((40, 120), 'Second physical page boundary')
        doc.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    db_path = tmp_path / 'original.sqlite'
    with sqlite3.connect(db_path) as db:
        db.executescript('''
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE sources(id TEXT PRIMARY KEY,sha256 TEXT);
            CREATE TABLE clauses(id TEXT PRIMARY KEY,source_id TEXT,text TEXT,status TEXT,citations_json TEXT);
            CREATE TABLE chunks(id TEXT PRIMARY KEY,entity_type TEXT,entity_id TEXT);
            CREATE VIRTUAL TABLE search_fts USING fts5(chunk_id UNINDEXED,title,text);
        ''')
        db.execute('INSERT INTO sources VALUES(?,?)', ('source', digest))
        db.execute('INSERT INTO clauses VALUES(?,?,?,?,?)', ('clause', 'source', 'Grade stone M800.',
            'native', json.dumps([dict(physical_page=1)])))
        db.execute('INSERT INTO chunks VALUES(?,?,?)', ('chunk', 'clause', 'clause'))
        db.execute('INSERT INTO search_fts VALUES(?,?,?)', ('chunk', 'Grade', 'Grade stone M800.'))
        db.execute('INSERT INTO clauses VALUES(?,?,?,?,?)', ('other', 'source', 'overlapping native fragment',
            'native', json.dumps([dict(physical_page=1)])))
        db.execute('INSERT INTO chunks VALUES(?,?,?)', ('other-chunk', 'clause', 'other'))
        db.execute('INSERT INTO search_fts VALUES(?,?,?)', ('other-chunk', 'Grade', 'overlapping native fragment'))
        if multipage:
            db.execute('UPDATE clauses SET citations_json=?,text=?',
                (json.dumps([dict(physical_page=1),dict(physical_page=2)]), 'MULTIPAGE_ORIGINAL_SCOPE'))
    @contextmanager
    def connect():
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            yield db
    (tmp_path / 'CURRENT.json').write_text('unchanged')
    return SimpleNamespace(root=tmp_path, identity=dict(release_id='original'),
        profile='roads', source_pdf=lambda _: path, connect=connect), db_path


def test_new_release_marks_lost_symbols_and_updates_search_without_changing_current(tmp_path):
    kb, old_path = fixture_corpus(tmp_path)
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    report = prepare_source(kb, 'source')
    assert report['pages'][0]['coverage']['unresolved'] == 1
    pin = stage_release(kb, report, 'new-release')
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == old_hash
    assert (tmp_path / 'CURRENT.json').read_text() == 'unchanged'
    target = tmp_path / pin['database']
    assert hashlib.sha256(target.read_bytes()).hexdigest() == pin['sha256']
    with sqlite3.connect(target) as db:
        text, status = db.execute('SELECT text,status FROM clauses').fetchone()
        assert 'не распознано' in text and 'inline:' in text
        assert status == 'ocr_unreviewed_inline_raster'
        assert db.execute("SELECT count(*) FROM search_fts WHERE search_fts MATCH 'inline'").fetchone()[0] == 1
    with pytest.raises(FileExistsError):
        stage_release(kb, report, 'new-release')


def test_explicit_transcription_recovers_literal_without_granting_material_verdict(tmp_path):
    kb, _ = fixture_corpus(tmp_path)
    report = prepare_source(kb, 'source')
    region = report['pages'][0]['regions'][0]
    entry = {k: region[k] for k in ('source_id', 'pdf_sha256', 'page', 'bbox', 'image_sha256')}
    entry.update(text='Q₁', reviewer='test reviewer', method='visual')
    report = prepare_source(kb, 'source', transcriptions=[entry])
    pin = stage_release(kb, report, 'transcribed')
    with sqlite3.connect(tmp_path / pin['database']) as db:
        assert db.execute("SELECT count(*) FROM search_fts WHERE search_fts MATCH 'Q1'").fetchone()[0] == 1
        assert db.execute('SELECT status FROM clauses').fetchone()[0] == 'native_with_visual_transcriptions'
        assert 'Q₁' in db.execute('SELECT text FROM clauses').fetchone()[0]
    assert report['automatic_verdicts'] == 0 and report['model_calls'] == 0


def test_modified_original_pdf_is_rejected_before_transcription(tmp_path):
    kb, _ = fixture_corpus(tmp_path)
    kb.source_pdf('source').write_bytes(b'changed')
    with pytest.raises(ValueError, match='PDF bytes changed'):
        prepare_source(kb, 'source')


def test_multipage_clause_preserves_original_and_every_page_in_text_and_search(tmp_path):
    kb, old_path = fixture_corpus(tmp_path, multipage=True)
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    report = prepare_source(kb, 'source')
    pin = stage_release(kb, report, 'two-pages')
    with sqlite3.connect(tmp_path / pin['database']) as db:
        text = db.execute("SELECT text FROM clauses WHERE id='clause'").fetchone()[0]
        assert 'MULTIPAGE_ORIGINAL_SCOPE' in text
        for page in report['pages']:
            assert page['regions'][0]['source_id'] in text
        assert 'Second physical page boundary' in text
        indexed = db.execute("SELECT text FROM search_fts WHERE chunk_id='chunk'").fetchone()[0]
        assert indexed == text
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == old_hash


def test_pdf_changed_after_preparation_cannot_be_published(tmp_path):
    kb, _ = fixture_corpus(tmp_path)
    report = prepare_source(kb, 'source')
    kb.source_pdf('source').write_bytes(b'changed after preparation')
    with pytest.raises(ValueError, match='PDF bytes changed'):
        stage_release(kb, report, 'stale-pdf')
    assert not (tmp_path / 'releases/stale-pdf').exists()


@pytest.mark.parametrize('damage', ['missing_chunk', 'missing_fts', 'duplicate_fts'])
def test_missing_search_chunk_leaves_no_partial_release(tmp_path, damage):
    kb, db_path = fixture_corpus(tmp_path)
    report = prepare_source(kb, 'source')
    with sqlite3.connect(db_path) as db:
        if damage == 'missing_chunk':
            db.execute("DELETE FROM chunks WHERE entity_id='clause'")
        elif damage == 'missing_fts':
            db.execute("DELETE FROM search_fts WHERE chunk_id='chunk'")
        else:
            db.execute("INSERT INTO search_fts SELECT * FROM search_fts WHERE chunk_id='chunk'")
    with pytest.raises(ValueError, match='search chunk'):
        stage_release(kb, report, 'incomplete')
    assert not (tmp_path / 'releases/incomplete').exists()
    assert not list((tmp_path / 'releases').glob('.prepare-*'))
    assert (tmp_path / 'CURRENT.json').read_text() == 'unchanged'


def test_duplicate_prepared_pages_cannot_duplicate_or_relabel_evidence(tmp_path):
    kb, _ = fixture_corpus(tmp_path)
    report = prepare_source(kb, 'source')
    report['pages'].append(report['pages'][0])
    with pytest.raises(ValueError, match='Duplicate raster page'):
        stage_release(kb, report, 'duplicate-page')
    assert not (tmp_path / 'releases/duplicate-page').exists()
