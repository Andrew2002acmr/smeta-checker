"""Bounded reverse navigation from norms to their own collection's technical text.

Only complete, explicitly numbered paragraphs containing literal references
are navigation matches. Whole stored chunks are supplied, never a sentence
without its surrounding conditions. Nothing here chooses or applies a norm.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Any


MAX_CANDIDATES = 12
MAX_SOURCES = 2
MAX_CHUNKS_PER_SOURCE = 3
MAX_QUERY_ROWS = 64
MAX_CHUNK_CHARS = 6000
MAX_ADDITION_CHARS = 20000
MAX_ADDITION_UTF8_BYTES = 35000
CODE = re.compile(r'\d{2}-\d{2}-\d{3}-\d{2,3}')
PARAGRAPH = re.compile(r'(?m)^[ \t]*([12]\.\d{1,2}\.\d+(?:\.\d+)?)\.[ \t]+')
FAMILY = re.compile(r'(?<!\w)(ГЭСНмр|ГЭСНм|ГЭСНр|ГЭСН|ФЕРм|ФЕР|ТЕРм|ТЕР)(?!\w)', re.I)
SCOPE = (
    'Это навигация по буквальным ссылкам технической части ТОГО ЖЕ сборника, '
    'а не подтверждение применимости нормы. Читай полные фрагменты совместно '
    'с найденными нормами и ПД; сохраняй исключения, пределы толщины, материал, '
    'технологию и требования к расходу по проекту. matched_paragraphs отмечает '
    'полные нумерованные пункты с прямой ссылкой; начало или конец сохранённого '
    'фрагмента может содержать неполный соседний пункт, который нельзя считать '
    'полным основанием. Подборка ограничена и не доказывает полноту сборника, '
    'а ссылка на найденную норму в пункте о другом виде работ не создаёт '
    'обязанность выполнить этот другой вид работ. Не считай его условие выполненным '
    'только из-за присутствия фрагмента в подборке. Подборка не подтверждает '
    'действие редакции или пригодность конкретной машины. Не требуй отдельного '
    'разрешения на действие, прямо предусмотренное подходящим полным пунктом, '
    'но отдельно проверь все его условия. Статусы требований не меняются автоматически.'
)


def _normal(text):
    value = unicodedata.normalize('NFKC', text)
    value = re.sub('[‐‑‒–—−]', '-', value)
    return re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', value)


def _key(text):
    return re.sub(r'\s+', ' ', _normal(text)).strip()


def _empty(reason, warnings=()):
    diagnostic = dict(scope='same_collection_literal_navigation_only',
        coverage_complete=False, literal_chunks=[], reason=reason, warnings=list(warnings))
    return dict(sources=[], preferred_source_ids=[], context=diagnostic, reason=reason,
                warnings=list(warnings))


def _pages(item, source):
    refs = item.get('citations')
    if not isinstance(refs, list) or not refs:
        return []
    pages = []
    for ref in refs:
        if (not isinstance(ref, dict) or ref.get('source_id') != source['id']
                or type(ref.get('physical_page')) is not int
                or not 1 <= ref['physical_page'] <= source['pages']):
            return []
        pages.append(ref['physical_page'])
    return sorted(set(pages))


def _matches(item, norms):
    """Do not use a paragraph cut off by a chunk boundary or a similar code."""
    text = _normal(item['text'])
    starts = list(PARAGRAPH.finditer(text))
    output = []
    for number, start in enumerate(starts):
        following = starts[number + 1] if number + 1 < len(starts) else None
        paragraph = text[start.start():following.start() if following else len(text)]
        # A page chunk's last paragraph may continue beyond the chunk even if
        # its last visible sentence ends with a full stop. Exact clause records
        # have an explicit paragraph identity; page-window records do not.
        if following is None and (item.get('label') != start[1]
                                  or not paragraph.rstrip().endswith('.')):
            continue
        families = {value.casefold() for value in FAMILY.findall(paragraph)}
        matched = []
        references = []
        for norm in norms:
            if int(start[1].split('.')[1]) != int(norm['code'][:2]):
                continue
            if families - {norm['family'].casefold()}:
                continue
            for ref in (norm['code'], norm['table_code']):
                if re.search(r'(?<![\w-])' + re.escape(ref) + r'(?![\w-])', paragraph):
                    if norm['id'] not in matched:
                        matched.append(norm['id'])
                    if ref not in references:
                        references.append(ref)
        if matched:
            output.append(dict(label=start[1], candidate_ids=matched, references=references,
                               paragraph_key=_key(paragraph)))
    return output


def collection_context(kb, candidates, *, expected_pin) -> dict[str, Any]:
    """Return optional pinned source cards, without changing the candidate list.

    This intentionally does not expand code ranges, infer equivalent families,
    follow cross-document references or certify extraction/edition validity.
    Overflow and incomplete paragraphs are reported; text is never shortened.
    """
    pin = deepcopy(getattr(kb, 'identity', {}))
    if (not isinstance(pin, dict) or pin != expected_pin
            or not all(isinstance(pin.get(key), str) and pin[key]
                       for key in ('database', 'release_id', 'profile'))
            or not re.fullmatch('[0-9a-f]{64}', str(pin.get('sha256', '')))):
        return _empty('corpus_pin_mismatch')
    if len(candidates) > MAX_CANDIDATES:
        return _empty('candidate_budget_exceeded')
    warnings, groups = [], {}
    try:
        for candidate in candidates:
            if candidate.get('entity_kind') != 'norm':
                continue
            norm = kb.entity('norm', candidate['id'])
            if (any(candidate.get(key) != norm.get(key)
                    for key in ('id', 'source_id', 'family', 'code', 'table_code'))
                    or norm.get('entity_kind') != 'norm'
                    or not CODE.fullmatch(str(norm.get('code', '')))
                    or norm.get('table_code') != norm['code'].rsplit('-', 1)[0]
                    or norm.get('source_kind') != 'GESN'
                    or norm.get('family') not in {'ГЭСН', 'ГЭСНм', 'ГЭСНр', 'ГЭСНмр'}
                    or norm.get('status') == 'native_definition_ambiguous'):
                warnings.append('candidate_identity_unverified')
                continue
            group = groups.setdefault((norm['source_id'], norm['family']), [])
            if not any(old['id'] == norm['id'] for old in group):
                group.append(norm)
        if len({source_id for source_id, _ in groups}) > MAX_SOURCES:
            return _empty('source_budget_exceeded')
        if len(groups) != len({source_id for source_id, _ in groups}):
            return _empty('ambiguous_collection_families')
        cards, literals, seen = [], [], set()
        for (source_id, family), norms in groups.items():
            with kb.connect() as db:
                record = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            source = dict(record) if record else {}
            if (source.get('kind') != 'GESN' or type(source.get('pages')) is not int
                    or not re.fullmatch('[0-9a-f]{64}', str(source.get('sha256', '')))):
                warnings.append('source_identity_unverified')
                continue
            if any(not _pages(norm, source) for norm in norms):
                warnings.append('candidate_citations_unverified')
                continue
            boundary = min(page for norm in norms for page in _pages(norm, source))
            terms = list(dict.fromkeys(ref for norm in norms
                                      for ref in (norm['code'], norm['table_code'])))
            with kb.connect() as db:
                rows = db.execute('SELECT id,text FROM clauses WHERE source_id=? AND ('
                    + ' OR '.join('text LIKE ?' for _ in terms) + ') ORDER BY id LIMIT ?',
                    (source_id, *('%' + term + '%' for term in terms), MAX_QUERY_ROWS + 1)).fetchall()
            if len(rows) > MAX_QUERY_ROWS:
                warnings.append('clause_query_budget_exceeded:' + source_id)
                continue
            options = []
            for row in rows:
                item = kb.entity('clause', row['id'])
                pages = _pages(item, source)
                text = item.get('text')
                if text != row['text']:
                    warnings.append('clause_text_changed:' + row['id'])
                    continue
                if (item.get('entity_kind') != 'clause' or item.get('source_id') != source_id
                        or item.get('source_kind') != source['kind']
                        or item.get('designation') != source['designation']
                        or not pages or max(pages) >= boundary
                        or not isinstance(text, str) or not text
                        or item.get('status') not in {'extracted_native_unreviewed',
                                                     'reviewed_literal_transcription'}):
                    continue
                if len(text) > MAX_CHUNK_CHARS:
                    warnings.append('whole_chunk_budget_exceeded:' + item['id'])
                    continue
                matches = _matches(item, norms)
                if not matches:
                    continue
                options.append((item, pages, matches))
            options.sort(key=lambda option: (-len(option[2]), option[1], option[0]['id']))
            count = 0
            for item, pages, matches in options:
                keys = {(source_id, family, match['paragraph_key']) for match in matches}
                if keys <= seen:
                    continue
                if count >= MAX_CHUNKS_PER_SOURCE:
                    warnings.append('clause_output_budget_exceeded:' + source_id)
                    break
                card_id = 'collection:' + item['id']
                metadata = json.loads(source.get('metadata_json') or '{}')
                card = dict(id=card_id, kind='normative', source_id=source_id, page=pages[0],
                    label=source['designation'] + ' · ' + item['label'], text=item['text'],
                    origin=item['status'], clause_id=item['id'], citation_pages=pages,
                    citations=deepcopy(item['citations']), source_sha256=source['sha256'],
                    text_sha256=hashlib.sha256(item['text'].encode()).hexdigest(),
                    edition_status=(item.get('edition') or {}).get('edition_state', 'edition_not_confirmed'),
                    source_provenance={key: metadata[key] for key in
                        ('url', 'provenance', 'edition', 'edition_state', 'amendment_completeness_verified')
                        if key in metadata},
                    kb_identity=deepcopy(pin), automatic_application_allowed=False)
                literal = dict(source_id=card_id, physical_pages=pages, quote=item['text'],
                    matched_paragraphs=[{key: value for key, value in match.items()
                                         if key != 'paragraph_key'} for match in matches])
                cards.append(card)
                literals.append(literal)
                seen.update(keys)
                count += 1
        if getattr(kb, 'identity', None) != pin:
            return _empty('corpus_pin_changed')
        if not cards:
            return _empty('no_complete_literal_context', warnings)
        context = dict(scope='same_collection_literal_navigation_only', kb=pin,
            instruction=SCOPE, literal_chunks=literals, coverage_complete=False,
            warnings=list(dict.fromkeys(warnings)))
        result = dict(sources=cards, preferred_source_ids=[card['id'] for card in cards],
            context=context, reason='literal_collection_context', warnings=context['warnings'])
        serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if len(serialized) > MAX_ADDITION_CHARS or len(serialized.encode()) > MAX_ADDITION_UTF8_BYTES:
            return _empty('collection_context_budget_exceeded', warnings)
        return result
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return _empty('collection_context_unavailable', warnings)
