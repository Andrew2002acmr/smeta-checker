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

# Addressed simultaneous navigation, not an asserted consolidated edition.
# These hashes bind the two actually reviewed v5/v9 records, including their
# retained native layout/punctuation, and the original independent review.
ROAD_BASE_SHA = '6aa5203d913dd5eb1538a4c23577e90d8ce95fa741547058429c31d74f9cb8a5'
ROAD_ORDER_SHA = '5151486c1540769b94985d066fdef6442d0892fcbd1ad52a0704ea591852c223'
ROAD_ORDER_ID = 'pdf-' + ROAD_ORDER_SHA[:24]
ROAD_ORDER_DESIGNATION = 'ГЭСН 27 ФСНБ-2022 — приказ Минстроя 102/пр от 16.02.2024 (поправка)'
ROAD_REVIEW_SHA = '7e57bd9562030228588fb1122996cf2114c9b311ae458f90aad4059c40a537af'
ROAD_PAIR = (
    ('1.27.35', 'clause-44b6dbf37f4b8f166f90c17874fd5963', 222,
     '157fa34c6f0d450ac689423409d65b01e5bd00a9d122c9aa7fd2e23a5f2c0374'),
    ('27.1', 'clause-51ca0934e697b15a5155c8ac9d43519a', 243,
     '7ea7f8524e1c03e4a86da419f75ce929d9a66a34b48f86edb6cdc83cc168a073'),
)
ROAD_PAIR_SCOPE = (
    'Новая 1.27.35 и ДОБАВЛЕННОЕ приложение 27.1 приказа 102/пр даны рядом '
    'с исходной техчастью как кандидаты, без подмены. PDF назван ГЭСН 81-02-27-2022, '
    'но его стр.140 уже содержит другое 27.1, а 102/пр предписывает ДОПОЛНИТЬ. '
    'Соответствие бинарника целевой цепочке 1046/пр не удостоверено. '
    'Консолидация и применимость поправки не подтверждены. '
    'Два fallback при отсутствии проектных данных относятся к горячим смесям '
    'и отдельно ЩМА в перечисленных нормах. Лоток 27-06-030-02/27-06-069-01 '
    'остаётся по проектным данным; 27-07-006 не включён. Таблица требует типа '
    'смеси и максимальной плотности КАМЕННЫХ материалов; 1,004 требует '
    'проектного условия прижимного вальца. Доставка не подтверждает ни один claim.'
)


def _road_amendment_pair(kb, source, norms, pin):
    try:
        return _road_amendment_pair_checked(kb, source, norms, pin)
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return [], [], 'road_amendment_pair_unavailable'


def _road_amendment_pair_checked(kb, source, norms, pin):
    """Known literal pair only; never follow arbitrary amends_source_ids."""
    matched = [norm for norm in norms if norm['family'] == 'ГЭСН'
               and norm['table_code'] in {'27-06-029', '27-06-030', '27-06-031', '27-06-032'}
               and norm['code'] != '27-06-030-02']
    if (source.get('sha256') != ROAD_BASE_SHA
            or source.get('id') != 'pdf-' + ROAD_BASE_SHA[:24]
            or source.get('designation') != 'ГЭСН 27 ФСНБ-2022'
            or source.get('pages') != 142 or not matched):
        return [], [], ''
    with kb.connect() as db:
        row = db.execute('SELECT * FROM sources WHERE id=?', (ROAD_ORDER_ID,)).fetchone()
    if not row:
        return [], [], 'road_amendment_pair_unavailable'
    amendment = dict(row)
    metadata = json.loads(amendment.get('metadata_json') or '{}')
    review = metadata.get('independent_verification')
    review_sha = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()
    if (amendment.get('sha256') != ROAD_ORDER_SHA or amendment.get('kind') != 'GESN'
            or amendment.get('designation') != ROAD_ORDER_DESIGNATION
            or amendment.get('pages') != 764
            or amendment.get('blob_path') != 'blobs/' + ROAD_ORDER_SHA + '.pdf'
            or metadata.get('sha256') != ROAD_ORDER_SHA
            or metadata.get('status') != 'pdf_verified'
            or metadata.get('amends_source_ids') != [source['id']]
            or metadata.get('supersedes_labels') != ['1.27.35']
            or review_sha != ROAD_REVIEW_SHA):
        return [], [], 'road_amendment_pair_identity_mismatch'
    cards, literals = [], []
    for label, entity_id, page, text_sha in ROAD_PAIR:
        item = kb.entity('clause', entity_id)
        with kb.connect() as db:
            stored = db.execute('SELECT text FROM clauses WHERE id=? AND source_id=?',
                                (entity_id, ROAD_ORDER_ID)).fetchone()
        text = item.get('text')
        if (not stored or text != stored['text'] or not isinstance(text, str)
                or hashlib.sha256(text.encode()).hexdigest() != text_sha
                or item.get('id') != entity_id or item.get('label') != label
                or item.get('entity_kind') != 'clause' or item.get('source_kind') != 'GESN'
                or item.get('source_id') != ROAD_ORDER_ID
                or item.get('designation') != amendment.get('designation')
                or item.get('edition') != metadata
                or item.get('status') != 'native_visually_verified'
                or item.get('citations') != [dict(source_id=ROAD_ORDER_ID, physical_page=page)]
                or len(text) > MAX_CHUNK_CHARS):
            return [], [], 'road_amendment_pair_literal_mismatch'
        card_id = 'collection:' + entity_id
        relation = dict(kind='simultaneous_addressed_amendment_navigation',
                        target_edition_state='unresolved')
        cards.append(dict(id=card_id, kind='normative', source_id=ROAD_ORDER_ID,
            source_sha256=ROAD_ORDER_SHA, page=page, citation_pages=[page],
            label=amendment['designation'] + ' · ' + label, clause_id=entity_id,
            text=text, text_sha256=text_sha, origin=item['status'],
            citations=deepcopy(item['citations']), kb_identity=deepcopy(pin),
            amendment_navigation=relation, automatic_application_allowed=False))
        literals.append(dict(source_id=card_id, physical_pages=[page], quote=text,
            matched_paragraphs=[dict(label=label,
                candidate_ids=[norm['id'] for norm in matched],
                references=list(dict.fromkeys(norm['table_code'] for norm in matched)))]))
    return cards, literals, ''


# Exact independently reviewed addressed registry, not a mutable review flag.
ADDRESSED_REGISTRY_SHA = '96cd308fca9b8f5354c95c43f1265c3edfe4077c06533b1d8d856608d01b4bbd'
ROAD_378_SHA = '2d9d1f7aa5f6ee79799a726c2219f56c4ebc29f32bf446826e32a28a69d79366'
ROAD_378_ID = 'pdf-' + ROAD_378_SHA[:24]
OLD_EQ_ROW_SHA = {
    '27-02-010-02':'608faaaf7a9ea741f08b1d1f50d995a6c8317d245c420523777b7977088b8e3d',
    '27-06-031-01':'064e4da9fc4b4a959cd76a7e72ba3cbc3ebcad1cc5fbef6c811b7d2ecfc47b65',
    '27-06-032-01':'c84423ab6708d443ed2cb701993dc18b1c9e2c180508308f76eea0e9ada9acdc',
}
CHECKED_DEFINITION_ROW_SHA = {
    '27-06-031-01': '55c8291441e2eab21d6e4a722f9a19db18489bef42154bb575f392a04f7c3272',
    '27-06-032-01': '739cbdeab5b711c5747eaddb1ce705f035631a0a265353907b68269d7cf523fe',
    '27-02-010-02': 'badf5cf7adbcd84b3f2178aa0a97f3a49f0890c174e13642593aedf90ac803b6',
}
ADDRESSED_SCOPE = (
    'Адресно проверены 1.27.35/37 приказа 69/пр от 07.02.2025 и исходное '
    'приложение 27.1 приказа 102/пр; это не полная консолидация будущей цепочки. '
    'Максимальная плотность КАМЕННЫХ материалов не является '
    'плотностью уплотнённой смеси. Тождество полного текста определений 378 '
    'не отождествляет ресурсы. Применимость и условия не подтверждаются автоматически.'
)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':')).encode()).hexdigest()


def _text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _checked_addressed_registry(kb, wrapper):
    """Bind known registry to stored literals; no PDF extraction in LLM worker."""
    if (wrapper.get('schema')!='addressed_collection_context_v1'
            or wrapper.get('registry_sha256')!=ADDRESSED_REGISTRY_SHA
            or _digest(wrapper.get('registry'))!=ADDRESSED_REGISTRY_SHA):
        raise ValueError('addressed_registry_digest_mismatch')
    registry=wrapper['registry']
    sources,clauses,definitions={},{},{}
    with kb.connect() as db:
        for expected in registry['sources']:
            found=db.execute('SELECT * FROM sources WHERE id=?',(expected['source_id'],)).fetchone()
            if not found:
                raise ValueError('addressed_source_missing')
            source=dict(found)
            fields=dict(id=expected['source_id'],sha256=expected['sha256'],kind=expected['kind'],
                designation=expected['designation'],pages=expected['physical_page_count'],blob_path=expected['blob_path'])
            meta=json.loads(source.get('metadata_json') or '{}')
            if (any(source.get(k)!=v for k,v in fields.items())
                    or meta.get('addressed_registry_sha256')!=ADDRESSED_REGISTRY_SHA
                    or any(meta.get(k)!=v for k,v in expected.items() if k!='file')
                    or meta.get('ingestion')!='reviewed_addressed_native_literals'
                    or meta.get('edition_state')!='metadata_checked_amendments_incomplete'
                    or meta.get('consolidated_edition') is not False
                    or meta.get('amendment_completeness_verified') is not False):
                raise ValueError('addressed_source_identity_mismatch')
            sources[source['id']]=source
        for expected in registry['clauses']:
            found=db.execute('SELECT * FROM clauses WHERE id=?',(expected['id'],)).fetchone()
            if not found:
                raise ValueError('addressed_clause_missing')
            row=dict(found)
            if (any(row.get(k)!=expected[k] for k in ('id','source_id','label','title','status'))
                    or _text_sha(row['text'])!=expected['raw_text_sha256']
                    or row['text']!=expected['text']
                    or json.loads(row['citations_json'])!=expected['citations']):
                raise ValueError('addressed_clause_literal_mismatch')
            item=kb.entity('clause',row['id'])
            if (item.get('text')!=row['text'] or item.get('status')!=row['status']
                    or item.get('citations')!=expected['citations']
                    or item.get('source_id')!=row['source_id']
                    or item.get('source_kind')!=sources[row['source_id']]['kind']
                    or item.get('designation')!=sources[row['source_id']]['designation']):
                raise ValueError('addressed_clause_entity_mismatch')
            clauses[row['id']]=item
        for expected in registry['norm_definitions']:
            found=db.execute('SELECT * FROM norms WHERE id=?',(expected['id'],)).fetchone()
            if not found:
                raise ValueError('addressed_definition_missing')
            row=dict(found)
            if (_digest(row)!=CHECKED_DEFINITION_ROW_SHA.get(expected['code'])
                    or any(row.get(k)!=expected[k] for k in ('id','source_id','family','code','table_code','title','status'))
                    or _text_sha(row['text'])!=expected['raw_text_sha256']
                    or row['text']!=expected['text']
                    or json.loads(row['citations_json'])!=expected['citations']):
                raise ValueError('addressed_definition_literal_mismatch')
            proof=expected['proof']
            if proof.get('version')!='native-definition-scope-v1' or not proof.get('rows'):
                raise ValueError('addressed_native_proof_mismatch')
            refs=[dict(source_id=row['source_id'],physical_page=r['page'],bbox=r['bbox'],
                coordinate_system='pdf-points',source_sha256=sources[row['source_id']]['sha256']) for r in proof['rows']]
            if refs!=expected['citations'] or any(_key(r['text']) not in _key(row['text']) for r in proof['rows']):
                raise ValueError('addressed_native_proof_citation_mismatch')
            unit=json.loads(row['unit_json'])
            if (unit != dict(raw=unit.get('raw'), status='parsed',
                    scale=unit.get('raw','').split(' ',1)[0],
                    unit=unit.get('raw','').split(' ',1)[-1])
                    or 'Измеритель: '+unit['raw'] not in row['text']):
                raise ValueError('addressed_definition_measure_mismatch')
            composition=db.execute('SELECT operations_json FROM norm_tables WHERE source_id=? AND family=? AND code=?',
                (row['source_id'],row['family'],row['table_code'])).fetchone()
            if not composition or json.loads(composition[0])!=[row['text'].split('Состав работ:',1)[1].strip()]:
                raise ValueError('addressed_definition_composition_mismatch')
            item=kb.entity('norm',row['id'])
            if (item.get('text')!=row['text'] or item.get('citations')!=expected['citations']
                    or item.get('status')!=row['status'] or item.get('source_id')!=row['source_id']
                    or item.get('code')!=row['code'] or item.get('unit')!=unit):
                raise ValueError('addressed_definition_entity_mismatch')
            definitions[row['id']]=item
        preserved=registry['preserved_appendix102']
        found=db.execute('SELECT * FROM sources WHERE id=?',(preserved['source_id'],)).fetchone()
        if not found or _digest(dict(found))!=preserved['row_sha256']:
            raise ValueError('addressed_appendix_source_mismatch')
        sources[preserved['source_id']]=dict(found)
        for expected in preserved['clauses']:
            row=db.execute('SELECT * FROM clauses WHERE id=?',(expected['id'],)).fetchone()
            if not row or _digest(dict(row))!=expected['row_sha256'] or _text_sha(row['text'])!=expected['raw_text_sha256']:
                raise ValueError('addressed_appendix_literal_mismatch')
            item=kb.entity('clause',expected['id'])
            if (item.get('text')!=row['text'] or item.get('citations')!=expected['citations']
                    or item.get('status')!=expected['status'] or item.get('source_id')!=preserved['source_id']):
                raise ValueError('addressed_appendix_entity_mismatch')
            clauses[expected['id']]=item
        base=db.execute('SELECT * FROM sources WHERE id=?',('pdf-'+ROAD_BASE_SHA[:24],)).fetchone()
        if not base or any(base[k]!=v for k,v in dict(sha256=ROAD_BASE_SHA,kind='GESN',
                designation='ГЭСН 27 ФСНБ-2022',pages=142,blob_path='blobs/'+ROAD_BASE_SHA+'.pdf').items()):
            raise ValueError('addressed_original_source_identity_mismatch')
        for equality in registry['definition_equivalences']:
            row=db.execute('SELECT * FROM norms WHERE id=?',(equality['old_norm_id'],)).fetchone()
            if (not row or _digest(dict(row))!=OLD_EQ_ROW_SHA.get(equality['code'])
                    or _text_sha(row['text'])!=equality['old_raw_text_sha256']
                    or re.sub(r'\s+',' ',row['text']).strip()!=re.sub(r'\s+',' ',definitions[equality['new_norm_id']]['text']).strip()
                    or equality['resources_equivalent'] is not False):
                raise ValueError('addressed_definition_equivalence_mismatch')
    return dict(registry=registry,sources=sources,clauses=clauses,definitions=definitions)


def _latest_road_context(kb,groups,pin,supplied_candidates) -> dict[str, Any] | None:
    eligible=[n for (sid,family),norms in groups.items() for n in norms
        if sid in {'pdf-'+ROAD_BASE_SHA[:24],ROAD_378_ID} and family=='ГЭСН'
        and n['table_code'] in {'27-06-029','27-06-030','27-06-031','27-06-032'}
        and n['code']!='27-06-030-02']
    if not eligible:
        return None
    with kb.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            return None
        row=db.execute('SELECT value FROM metadata WHERE key=?',('addressed_collection_context_v1',)).fetchone()
    if not row:
        return None
    try:
        checked=_checked_addressed_registry(kb,json.loads(row['value']))
    except (KeyError,ValueError,TypeError,AttributeError,sqlite3.Error):
        return dict(invalid=True)
    registry=checked['registry']
    source69=next(s for s in registry['sources'] if s['key']=='69')
    chosen=[next(c for c in registry['clauses'] if c['source_id']==source69['source_id'] and c['label']==label)
        for label in ('1.27.35','1.27.37')]
    appendix=next(c for c in registry['preserved_appendix102']['clauses'] if c['label']=='27.1')
    chosen.append(appendix)
    cards,literals,equivalences=[],[],[]
    def deliver(item,source,relation,matched):
        card_id='collection:'+item['id']
        pages=sorted({c['physical_page'] for c in item['citations']})
        # Full-page references cover the complete independently bound definition.
        # Do not repeat every native row rectangle in the bounded model packet.
        citations=(deepcopy(item['citations']) if item.get('entity_kind')!='norm' else
            [dict(source_id=source['id'],physical_page=page,source_sha256=source['sha256'])
                for page in pages])
        cards.append(dict(id=card_id,kind='normative',source_id=source['id'],source_sha256=source['sha256'],
            page=pages[0],
            label=('ГЭСН '+item['code']+' · 378/пр' if item.get('entity_kind')=='norm' else
                ('102/пр' if source['id']==ROAD_ORDER_ID else '69/пр')+' · '+item['label']),
            text=item['text'],origin=item['status'],
            citations=citations,kb_identity=deepcopy(pin),
            amendment_navigation=dict(kind=relation),
            automatic_application_allowed=False,
            **{('norm_id' if item.get('entity_kind')=='norm' else 'clause_id'):item['id']}))
        literals.append(dict(source_id=card_id,physical_pages=pages,quote=item['text'],matched_paragraphs=matched))
    for expected in chosen:
        item=checked['clauses'][expected['id']]
        deliver(item,checked['sources'][item['source_id']],'addressed_literal',
            [dict(label=expected['label'],candidate_ids=[n['id'] for n in eligible],
                references=list(dict.fromkeys(n['table_code'] for n in eligible)))])
    for equality in registry['definition_equivalences']:
        if equality['code'] not in {'27-06-031-01','27-06-032-01'}:
            continue
        matched=[n['id'] for n in eligible if n['id'] in {equality['old_norm_id'],equality['new_norm_id']}]
        if matched:
            item=checked['definitions'][equality['new_norm_id']]
            if any(c.get('id')==item['id'] and all(c.get(key)==item.get(key)
                    for key in ('source_id','family','code','text','status','citations','unit','designation'))
                    for c in supplied_candidates):
                # NPC already supplies this exact checked full norm as a candidate
                # source. Preserve its relationship, not a duplicate evidence card.
                equivalences.append(dict(candidate_ids=matched,norm_id=item['id'],
                    source_id=item['source_id'],text_sha256=_text_sha(item['text']),
                    delivery='existing_candidate_full_definition'))
                continue
            deliver(item,checked['sources'][item['source_id']],'definition_equivalence',
                [dict(label='definition:'+item['code'],candidate_ids=matched,references=[item['code']])])
    return dict(cards=cards,literals=literals,equivalences=equivalences,candidate_ids={n['id'] for n in eligible},
        covered_labels={'1.27.35','1.27.37'},invalid=False)


def _deduplicate_delivery(cards, literals):
    """Deduplicate evidence identity, merging navigation mappings separately."""
    unique_cards = {}
    for card in cards:
        old = unique_cards.setdefault(card['id'], card)
        if _digest(old) != _digest(card):
            raise ValueError('collection_source_handle_envelope_conflict')
    unique_literals = {}
    for literal in literals:
        key = (literal['source_id'], literal['quote'], tuple(literal['physical_pages']))
        old = unique_literals.get(key)
        if old is None:
            old = deepcopy(literal)
            old['matched_paragraphs'] = []
            unique_literals[key] = old
        for match in literal['matched_paragraphs']:
            identity = {k:v for k,v in match.items() if k not in {'candidate_ids','references'}}
            target = next((m for m in old['matched_paragraphs'] if
                {k:v for k,v in m.items() if k not in {'candidate_ids','references'}} == identity), None)
            if target is None:
                old['matched_paragraphs'].append(deepcopy(match))
            else:
                for field in ('candidate_ids','references'):
                    target[field] = list(dict.fromkeys([*target.get(field,[]), *match.get(field,[])]))
    return list(unique_cards.values()), list(unique_literals.values())


def _normal(text):
    value = unicodedata.normalize('NFKC', text)
    value = re.sub('[‐‑‒–—−]', '-', value)
    return re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', value)


def _key(text):
    return re.sub(r'\s+', ' ', _normal(text)).strip()


def _empty(reason, warnings=()) -> dict[str, Any]:
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


def ranked_collection_context(kb, candidates, *, expected_pin) -> dict[str, Any]:
    """Route ranked definitions to the strict bounded reader.

    Search results may include material standards, clauses and unrelated
    collections. They remain addressable as search hits, but must not prevent
    the reader from opening technical context for the leading definitions.
    """
    selected, seen, source_ids = [], set(), set()
    omitted = 0
    for candidate in candidates:
        if (candidate.get('entity_kind') != 'norm'
                or candidate.get('source_kind') != 'GESN'
                or candidate.get('status') == 'native_definition_ambiguous'):
            continue
        key = (candidate.get('source_id'), candidate.get('id'))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        if (len(selected) >= MAX_CANDIDATES
                or key[0] not in source_ids and len(source_ids) >= MAX_SOURCES):
            omitted += 1
            continue
        source_ids.add(key[0])
        selected.append(candidate)
    if not selected:
        return _empty('no_ranked_norm_definitions')
    result = collection_context(kb, selected, expected_pin=expected_pin)
    if omitted:
        warning = 'ranked_definitions_omitted:' + str(omitted)
        result['warnings'] = [*result['warnings'], warning]
        result['context']['warnings'] = result['warnings']
    return result


def definition_links(chunk, definition_ids):
    """Expose a literal condition-to-definition route in the agent registry."""
    return [
        dict(label=match['label'], references=match['references'],
             definition_source_ids=linked)
        for match in chunk['matched_paragraphs']
        if (linked := list(dict.fromkeys(definition_ids[item_id]
            for item_id in match['candidate_ids'] if item_id in definition_ids)))
    ]


def collection_context(kb, candidates, *, expected_pin) -> dict[str, Any]:
    """Return optional pinned source cards, without changing the candidate list.

    General navigation does not expand code ranges or infer equivalent families.
    The fixed addressed registry permits only its checked cross-document
    literals/definition identities, never resource or applicability equivalence.
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
        latest=_latest_road_context(kb,groups,pin,candidates)
        if latest and latest.get('invalid'):
            return _empty('addressed_registry_invalid',['addressed_registry_invalid_no_newest_navigation'])
        cards=list(latest['cards']) if latest else []
        literals=list(latest['literals']) if latest else []
        omitted=[]
        seen=set()
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
            new_group=bool(latest and any(n['id'] in latest['candidate_ids'] for n in norms))
            pair_cards,pair_literals,pair_warning=([],[],'') if new_group else _road_amendment_pair(kb, source, norms, pin)
            cards.extend(pair_cards)
            literals.extend(pair_literals)
            if pair_warning:
                warnings.append(pair_warning)
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
                if (new_group and latest is not None and all(m['label'] in latest['covered_labels']
                        and set(m['candidate_ids'])<=latest['candidate_ids'] for m in matches)):
                    omitted.append(item['id'])
                    continue  # Omit the WHOLE historical carrier, never crop it.
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
        cards,literals = _deduplicate_delivery(cards,literals)
        if getattr(kb, 'identity', None) != pin:
            return _empty('corpus_pin_changed')
        if not cards:
            return _empty('no_complete_literal_context', warnings)
        context = dict(scope='same_collection_literal_navigation_only', kb=pin,
            instruction=SCOPE, literal_chunks=literals, coverage_complete=False,
            warnings=list(dict.fromkeys(warnings)))
        if latest:
            context['addressed_amendment_instruction']=ADDRESSED_SCOPE
            context['addressed_amendment_navigation']=dict(registry_sha256=ADDRESSED_REGISTRY_SHA,
                source_ids=[c['id'] for c in latest['cards']],last_reviewed_addressed_date='2025-02-07',
                target_edition_state='not_fully_consolidated',resources_aliased=False,
                omitted_historical_clause_ids=list(dict.fromkeys(omitted)))
            if latest['equivalences']:
                context['addressed_amendment_navigation']['definition_equivalences']=latest['equivalences']
        elif any(card.get('amendment_navigation') for card in cards):
            context['addressed_amendment_instruction'] = ROAD_PAIR_SCOPE
            context['addressed_amendment_navigation'] = dict(
                source_ids=['collection:' + record[1] for record in ROAD_PAIR],
                base_source_sha256=ROAD_BASE_SHA, amendment_source_sha256=ROAD_ORDER_SHA,
                independent_review_sha256=ROAD_REVIEW_SHA, target_edition_state='unresolved')
        result = dict(sources=cards, preferred_source_ids=[card['id'] for card in cards],
            context=context, reason='literal_collection_context', warnings=context['warnings'])
        serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if len(serialized) > MAX_ADDITION_CHARS or len(serialized.encode()) > MAX_ADDITION_UTF8_BYTES:
            return _empty('collection_context_budget_exceeded', warnings)
        return result
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return _empty('collection_context_unavailable', warnings)
