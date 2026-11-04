"""Read-only corpus adapter, adapted from the local knowledge-lab/kb.py pilot.

No extractor dependencies, network calls, implicit rates or writes to SQLite.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path


def decode(row):
    item = dict(row)
    for key in list(item):
        if key.endswith('_json'):
            item[key[:-5]] = json.loads(item.pop(key))
    return item


def _search_tokens(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е")
    ignored = {"для", "при", "или", "работы", "работ", "по", "на", "и", "в", "из", "с", "м", "шт", "п", "пункт"}
    output = []
    aliases = {"щпс": "щебеночно песчаная", "пгс": "песчано гравийная", "пбв": "полимерно битумное"}
    text = re.sub(r"\b(?:щпс|пгс|пбв)\b", lambda m: m[0] + " " + aliases[m[0]], text)
    for word in re.findall(r"[^\W_]+", text):
        if word in ignored:
            continue
        if re.fullmatch(r"[а-я]+", word) and len(word) >= 4:
            stem = re.sub(r"(иями|ами|ого|ему|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)$", "", word)
            word = (stem if len(stem) >= 3 else word)[:6]
        if word not in output:
            output.append(word)
        if len(output) == 18:
            break
    return output


def _document_matches(number, designation):
    candidates = re.findall(r"\d[\d.\-]+", designation)
    if re.search(r"-(?:\d{2}|\d{4})$", number):
        return number in candidates
    return any(value.split("-", 1)[0] == number or value.split("-", 1)[0].startswith(number + ".")
               for value in candidates)


def decimal(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(',', '.'))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def condition(tree, facts):
    if 'all' in tree or 'any' in tree:
        key = 'all' if 'all' in tree else 'any'
        items = [condition(c, facts) for c in tree[key]]
        states = [s for s, _ in items]
        missing = sorted({m for s, fields in items if s is None for m in fields})
        if key == 'all':
            return (False, []) if False in states else (None, missing) if None in states else (True, [])
        return (True, []) if True in states else (None, missing) if None in states else (False, [])
    field, expected = tree['field'], tree['value']
    actual = facts.get(field)
    if actual is None or actual == '':
        return None, [field]
    op = tree['op']
    if op == 'eq':
        return type(actual) is type(expected) and actual == expected, []
    if op == 'in':
        return actual in expected, []
    left, right = decimal(actual), decimal(expected)
    if left is None or right is None:
        return None, [field]
    return {'gt': left > right, 'lte': left <= right}[op], []


class Corpus:
    def __init__(self, root: Path, relative=None, expected_sha=None):
        self.root = root.resolve()
        pointer = json.loads((self.root / 'CURRENT.json').read_text(encoding='utf-8'))
        self.relative = relative or pointer['database']
        self.path = (self.root / self.relative).resolve()
        if not self.path.is_relative_to(self.root):
            raise ValueError('Недопустимый путь выпуска базы')
        from normative_sqlite_cache import verified_database
        self.path,self.sha = verified_database(self.path,expected_sha)
        if expected_sha and self.sha != expected_sha:
            raise ValueError('Изменился сохранённый выпуск базы знаний')
        with self.connect() as db:
            metadata = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')}
            self.release = metadata['release_id']
            self.profile = 'roads' if metadata.get('profile') == 'roads' else 'electrical'
            self.gesn_scope = metadata.get('gesn_scope', [['ГЭСНм', '08']])
            self.counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                           for table in ('sources', 'norms', 'clauses', 'rules')}
            self.source_index = {r['id']: dict(r) for r in db.execute('SELECT id,designation,pages FROM sources')}
        # This instance is pinned to one verified database SHA/release. Cache
        # only immutable retrieval IDs, never facts, mutable entities or a
        # SQLite connection. A new release/instance starts with an empty LRU.
        self._cached_search_pool = lru_cache(maxsize=256)(self._search_pool)
        self._cached_resource_names = lru_cache(maxsize=1)(self._resource_names)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @property
    def identity(self):
        return {'database': self.relative, 'sha256': self.sha, 'release_id': self.release, 'profile': self.profile}

    def source_pdf(self, source_id):
        with self.connect() as db:
            row = db.execute('SELECT blob_path FROM sources WHERE id=?', (source_id,)).fetchone()
        if not row:
            raise KeyError(source_id)
        path = (self.root / row['blob_path']).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Недопустимый путь источника')
        return path

    def entity(self, kind, entity_id):
        table = {'norm': 'norms', 'clause': 'clauses'}[kind]
        with self.connect() as db:
            row = db.execute(f'SELECT e.*,s.designation,s.kind AS source_kind,s.metadata_json AS edition_json '
                             f'FROM {table} e JOIN sources s ON s.id=e.source_id WHERE e.id=?', (entity_id,)).fetchone()
            if not row:
                raise KeyError(entity_id)
            item = decode(row)
            item['entity_kind'] = kind
            if kind == 'norm':
                item['resources'] = [decode(r) for r in db.execute('SELECT * FROM resources WHERE norm_id=?', (entity_id,))]
                t = db.execute('SELECT operations_json FROM norm_tables WHERE source_id=? AND family=? AND code=?',
                               (item['source_id'], item['family'], item['table_code'])).fetchone()
                item['composition'] = json.loads(t[0]) if t else []
            return item

    def _resource_names(self):
        """Immutable lexical navigation over the existing resource registry.

        Neither quantities nor applicability decisions enter this index.
        Repeated names are tokenized once, while all owning norms remain.
        """
        scope=' OR '.join('(n.family=? AND n.code LIKE ?)' for _ in self.gesn_scope) or '0'
        args=[v for family,number in self.gesn_scope for v in (family,number+'-%')]
        with self.connect() as db:
            rows=db.execute('SELECT DISTINCT r.name,n.id,n.code FROM resources r '
                'JOIN norms n ON n.id=r.norm_id WHERE ('+scope+') ORDER BY r.name,n.code',args).fetchall()
        names={}
        for row in rows:
            names.setdefault(row['name'],[]).append(row['id'])
        return tuple((frozenset(_search_tokens(name)),tuple(ids)) for name,ids in names.items())

    def _resource_matches(self, words):
        # Single generic words such as "смесь" cannot replace work retrieval.
        ignored=set(_search_tokens('смесь материал ресурс расход норма сборник'))
        wanted={w for w in words if re.fullmatch(r'[а-я]{3,}',w) and w not in ignored}
        if len(wanted)<2 or not self.gesn_scope:
            return []
        ranked=[]
        for ordinal,(tokens,ids) in enumerate(self._cached_resource_names()):
            common=wanted & tokens
            if len(common)>=max(2,min(3,len(wanted))):
                ranked.append((-len(common),len(tokens-wanted),ordinal,ids))
        ranked.sort()
        return list(dict.fromkeys(identity for *_,ids in ranked for identity in ids))[:8]

    def _search_pool(self, query, kind, gesn_scope):
        """Exact and FTS candidates in original order, before fact-dependent ranking."""
        entity_kind = 'norm' if kind == 'GESN' else 'clause'
        if entity_kind=='norm' and not gesn_scope:
            return (), ()
        document = re.search(r'(?:ГОСТ\s*(?:Р\s*)?|СП\s*|ПНСТ\s*|ОДМ\s*)(\d[\d.\-]*)', query, re.I)
        source_ids = []
        remaining = query
        if document and entity_kind == 'clause':
            designation = document[1].rstrip('.')
            source_ids = [s['id'] for s in self.source_index.values()
                          if _document_matches(designation, s['designation'])]
            if not source_ids:
                return (), ()
            remaining = query[:document.start()] + query[document.end():]
        words = _search_tokens(remaining)
        if not words:
            words = _search_tokens(query)
        if not words:
            return (), ()
        expr = ' OR '.join('"' + w + '"*' for w in words)
        source_sql = ' AND c.source_id IN (' + ','.join('?' for _ in source_ids) + ')' if source_ids else ''
        scope_sql = ' OR '.join('(n.family=? AND n.code LIKE ?)' for _ in gesn_scope) or '0'
        scope_args = [value for family, number in gesn_scope for value in (family, number + '-%')]
        with self.connect() as db:
            exact = []
            references=list(dict.fromkeys(re.findall(
                r'(?<![\w-])\d{2}-\d{2}-\d{3}(?:-\d{2,3})?(?![\w-])',query)))[:16]
            if references and entity_kind == 'norm':
                pools=[]
                for reference in references:
                    full=reference.count('-')==3
                    column='code' if full else 'table_code'
                    pools.append([r[0] for r in db.execute(
                        f'SELECT n.id FROM norms n WHERE n.{column}=? AND ({scope_sql}) ORDER BY n.code,n.id LIMIT 80',
                        (reference,*scope_args))])
                # A base and its adjustment table must both reach a bounded
                # result. Do not fill it with all alternatives of the first
                # mentioned table before considering the next reference.
                exact=list(dict.fromkeys(pool[i] for i in range(max(map(len,pools),default=0))
                    for pool in pools if i<len(pool)))
                if not exact:
                    return (), ()
            if entity_kind == 'clause':
                clause = re.search(r'\b\d+(?:\.\d+){1,4}\b', remaining)
                if clause:
                    exact = [r[0] for r in db.execute(
                        "SELECT c.id FROM clauses c JOIN sources s ON s.id=c.source_id WHERE c.label=? AND (?='ALL' OR s.kind=?)"
                        + source_sql, (clause[0], kind, kind, *source_ids))]
                    # Some imports retain a section only inside a physical-page
                    # chunk. A scoped numbered request must find its literal
                    # heading before FTS ranks scattered table numbers 5 OR 2.
                    if not exact and source_ids:
                        heading = re.compile(r'(?:^|\n)\s*'+re.escape(clause[0])+r'(?![\d.])\s+\S')
                        exact = [r['id'] for r in db.execute(
                            "SELECT c.id,c.text FROM clauses c JOIN sources s ON s.id=c.source_id "
                            "WHERE (?='ALL' OR s.kind=?)"+source_sql, (kind, kind, *source_ids))
                            if heading.search(r['text'])]
            rows = db.execute(
                'SELECT c.entity_id,bm25(search_fts,0,5,1) AS rank FROM search_fts '
                'JOIN chunks c ON c.id=search_fts.chunk_id JOIN sources s ON s.id=c.source_id '
                "WHERE search_fts MATCH ? AND c.entity_type=? AND (?='ALL' OR s.kind=?) "
                + source_sql + " "
                f"AND (c.entity_type!='norm' OR EXISTS (SELECT 1 FROM norms n WHERE n.id=c.entity_id AND ({scope_sql}))) "
                "ORDER BY rank,c.id LIMIT 80",
                (expr, entity_kind, kind, kind, *source_ids, *scope_args)).fetchall()
        resources=self._resource_matches(words) if entity_kind=='norm' and not references else []
        ids = list(dict.fromkeys(exact + resources + [r['entity_id'] for r in rows]))
        return tuple(exact), tuple(ids)

    def search(self, query, kind='GESN', facts=None, limit=12):
        facts = facts or {}
        query = unicodedata.normalize("NFKC", str(query)).replace("ё", "е")
        query = re.sub(r"[‐‑‒–—−]", "-", query)
        query = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", query)
        entity_kind = 'norm' if kind == 'GESN' else 'clause'
        exact, ids = self._cached_search_pool(query, kind, tuple(tuple(scope) for scope in self.gesn_scope))
        output = []
        for ordinal, entity_id in enumerate(ids):
            item = self.entity(entity_kind, entity_id)
            if entity_kind == 'norm' and not any(item['family'] == family and item['code'].startswith(number + '-') for family, number in self.gesn_scope):
                continue
            item['checks'], item['missing_facts'] = self.checks(item, facts)
            item['rules'] = self.rules(item, facts) if entity_kind == 'norm' else []
            item['edition_confirmed'] = False
            item['automatic_application_allowed'] = False
            item['exact_code'] = entity_id in exact
            conflicts = sum(c['state'] == 'mismatch' for c in item['checks'])
            # Structural conflicts cannot be hidden by an appealing text match.
            missing_checks = sum(c['state'] == 'unknown' for c in item['checks'])
            actual_mass, maximum_mass = decimal(facts.get('mass_kg_per_m')), decimal(item.get('attributes', {}).get('mass_kg_per_m_max'))
            mass_gap = maximum_mass - actual_mass if actual_mass is not None and maximum_mass is not None and maximum_mass >= actual_mass else Decimal('Infinity')
            matches = sum(c['state'] == 'match' for c in item['checks'])
            item['_sort'] = (0 if entity_id in exact else 1, ordinal if entity_id in exact else 0, bool(conflicts), -matches,
                             item['status'] != 'definition_reviewed', missing_checks, mass_gap, ordinal)
            output.append(item)
        output.sort(key=lambda i: i['_sort'])
        for item in output:
            item.pop('_sort')
        return output[:limit]

    def checks(self, item, facts):
        if item['entity_kind'] != 'norm':
            return [], []
        checks, missing = [], []
        fields = [('placement', 'placement', 'Способ прокладки'),
                  ('voltage_kv_max', 'voltage_kv', 'Напряжение, кВ'),
                  ('mass_kg_per_m_max', 'mass_kg_per_m', 'Масса кабеля, кг/м')]
        for expected_key, fact, label in fields:
            expected = item['attributes'].get(expected_key)
            if expected is None:
                continue
            actual = facts.get(fact)
            if actual is None or actual == '':
                state = 'unknown'
                missing.append(label)
            elif fact == 'placement':
                state = 'match' if actual == expected else 'mismatch'
            else:
                number = decimal(actual)
                maximum = decimal(expected)
                state = 'unknown' if number is None or maximum is None else 'match' if number <= maximum else 'mismatch'
                if state == 'unknown':
                    missing.append(label)
            checks.append({'field': fact, 'label': label, 'actual': actual, 'expected': expected, 'state': state})
        unit = item['unit']
        if unit.get('status') != 'parsed':
            checks.append({'label': 'Измеритель', 'state': 'unknown', 'actual': facts.get('unit'), 'expected': unit.get('raw')})
            missing.append('Уточнение измерителя нормы')
        elif facts.get('unit'):
            aliases = {'м': ('м', Decimal(1)), 'm': ('м', Decimal(1)), 'км': ('м', Decimal(1000)), 'м2': ('м2', Decimal(1)), 'га': ('м2', Decimal(10000)), 'м3': ('м3', Decimal(1)), 'шт': ('шт', Decimal(1)), 'т': ('кг', Decimal(1000)), 'кг': ('кг', Decimal(1))}
            def normalized(value):
                value = str(value or '').strip().casefold().rstrip('.').replace('²', '2').replace('³', '3')
                return aliases.get(value, (value, Decimal(1)))
            actual_base, actual_factor = normalized(facts['unit'])
            expected_base, expected_factor = normalized(unit.get('unit'))
            matches = actual_base == expected_base
            entry = {'label': 'Единица ВОР', 'state': 'match' if matches else 'mismatch',
                     'actual': facts['unit'], 'expected': unit.get('raw')}
            quantity, scale = decimal(facts.get('quantity')), decimal(unit.get('scale'))
            if matches and quantity is not None and scale:
                entry['norm_quantity'] = str(quantity * actual_factor / (scale * expected_factor))
            checks.append(entry)
        else:
            checks.append({'label': 'Единица ВОР', 'state': 'unknown', 'actual': None, 'expected': unit.get('raw')})
            missing.append('Единица измерения ВОР')
        if item['status'] != 'definition_reviewed':
            missing.append('Сверка автоматически извлечённой нормы с оригиналом')
        missing.append('Подтверждение редакции и применимости к проекту')
        return checks, missing

    def rules(self, item, facts):
        output = []
        scope_facts = {**{k: None if v == '' else v for k, v in facts.items()}, 'family': item['family'], 'collection': item['table_code'][:2]}
        with self.connect() as db:
            for row in db.execute('SELECT * FROM rules'):
                rule = decode(row)
                scope = rule['scope']
                if any(scope_facts.get(k) is not None and scope_facts[k] != v for k, v in scope.items()):
                    continue
                state, missing = condition(rule['condition'], scope_facts)
                missing += [k for k in scope if scope_facts.get(k) is None]
                output.append({'id': rule['id'], 'title': rule['title'], 'effect': rule['effects'],
                               'missing_facts': sorted(set(missing)), 'citations': rule['citations'],
                               'state': 'not_applicable' if state is False else 'needs_facts' if missing else 'candidate'})
        return output
