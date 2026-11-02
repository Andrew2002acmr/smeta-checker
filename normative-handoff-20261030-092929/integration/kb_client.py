"""Read-only normative corpus client; Python 3.11+, SQLite with FTS5, no pip packages."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

TABLES = ('sources', 'pages', 'clauses', 'norm_tables', 'norms', 'resources',
          'rules', 'relations', 'gaps', 'review_queue', 'chunks')


def decode(row):
    item = dict(row)
    for key in list(item):
        if key.endswith('_json'):
            value = item.pop(key)
            item[key[:-5]] = json.loads(value) if value is not None else None
    return item


def contained(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute():
        raise ValueError('Expected relative path')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path escaped corpus root')
    return path


def database_path(root):
    root = Path(root).resolve()
    current = json.loads((root / 'CURRENT.json').read_text(encoding='utf-8-sig'))
    path = contained(root, current['database'])
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def open_kb(root):
    path = database_path(root)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def search(db, query, kind=None, limit=10):
    words = re.findall(r'[^\W_]+', query.casefold())[:20]
    stems = {'кабель': 'кабел', 'кабели': 'кабел', 'кабеля': 'кабел',
             'освещение': 'освещ', 'заземление': 'заземл', 'прокладка': 'проклад',
             'щебеночно': 'щебеночн', 'уплотнение': 'уплотн'}
    if not words:
        return []
    expression = ' OR '.join('"' + stems.get(w, w) + '"*' for w in words)
    sql = '''SELECT c.*,s.designation,s.kind,s.role,s.sha256 AS source_sha256,
             bm25(search_fts,0,5,1) AS rank
             FROM search_fts JOIN chunks c ON c.id=search_fts.chunk_id
             JOIN sources s ON s.id=c.source_id WHERE search_fts MATCH ?'''
    args = [expression]
    if kind:
        sql += ' AND s.kind=?'
        args.append(kind)
    sql += ' ORDER BY rank,c.id LIMIT ?'
    args.append(max(1, min(int(limit), 100)))
    result = []
    for row in db.execute(sql, args):
        item = decode(row)
        item['requires_engineer_review'] = True
        item['edition_confirmed'] = False
        item['usable_for_calculation'] = False
        result.append(item)
    return result


def get_norm(db, family, code):
    result = []
    for row in db.execute('SELECT * FROM norms WHERE family=? AND code=?', (family, code)):
        item = decode(row)
        item['resources'] = [decode(r) for r in db.execute(
            'SELECT * FROM resources WHERE norm_id=? ORDER BY resource_code,id', (item['id'],))]
        table = db.execute('SELECT * FROM norm_tables WHERE source_id=? AND family=? AND code=?',
                           (item['source_id'], item['family'], item['table_code'])).fetchone()
        item['composition'] = json.loads(table['operations_json']) if table else []
        item['usable_for_calculation'] = False
        item['requires_engineer_review'] = True
        result.append(item)
    return result


def get_page(db, source_id, physical_page):
    row = db.execute('SELECT * FROM pages WHERE source_id=? AND page=?',
                     (source_id, physical_page)).fetchone()
    return decode(row) if row else None


def source_pdf(root, db, source_id):
    row = db.execute('SELECT blob_path FROM sources WHERE id=?', (source_id,)).fetchone()
    if row is None:
        raise KeyError(source_id)
    path = contained(root, row['blob_path'])
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
