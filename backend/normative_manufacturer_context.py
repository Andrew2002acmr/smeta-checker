"""Two reviewed manufacturer literals for an exact model-class question only."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

REVIEW = 'reviewed_literal_transcription'
HTML_SHA = '8938feb4d80d26eabaf3ee20b2b84558f0d33c4186a1e0fe24405de2d5d41df2'
SOURCES: tuple[dict[str, Any], ...] = (
    dict(source_id='pdf-2734fa10221e63a37a32de97',
         sha256='2734fa10221e63a37a32de97a80368b797dd16262d53fd9f045f160d1a14ee3a',
         kind='MANUFACTURER_PUBLICATION', pages=36, page=26,
         designation='HAMM HD 130 — Wirtgen Technology Winter 2002 (factory publication; archived copy)',
         clause_id='clause-04af5e1dc99b7940705cd826fab75578', label='hd130-vibration',
         text_sha256='fdb211d599b3bcba6b28fcf9f5e15927839d18905fba2b35020999ff66e10afe',
         artifact_type='original_factory_publication_archived', predicate='feature', value='vibration',
         url='https://expresswaysonline.com/wp-content/uploads/2018/10/WTM-Winter-2002.pdf'),
    dict(source_id='pdf-119e7005b140e150895efab0',
         sha256='119e7005b140e150895efab09d5e7b0ea3f2fe14b6e53a5bb5527661d39ed628',
         kind='MANUFACTURER_WEB_CAPTURE', pages=1, page=1,
         designation='HAMM HD 130 — Wirtgen Xibao official HTML (GENERATED WEB CAPTURE, NOT factory PDF)',
         clause_id='clause-6ab91ebb53b7c129416987317f14f75b', label='hd130-tandem',
         text_sha256='21a040f994ca843f304433abe5cd84e7a8f27ec6da7a1ddfefe3632b5ecc5896',
         artifact_type='generated_web_capture_not_factory_pdf', predicate='equipment_class', value='tandem_roller',
         url='https://www.wirtgen-group.com/en-ao/news-and-media/wirtgen-group/xibao-expressway/'),
)
MAX_CHARS = 6500
MAX_UTF8_BYTES = 9500
MAX_CLAIMS = 1
CLASS = re.compile(r'\bтандемн[а-яё]*\s+(?:виброкат[а-яё]*|вибрационн[а-яё]*\s+катк[а-яё]*)\b', re.I)
SCOPE = (
    'Два первичных источника совместно подтверждают только класс точной модели HAMM HD 130: '
    'официальная HTML-публикация называет ее tandem roller; заводской журнал указывает vibration '
    'в блоке HD 90/HD 130. Веб-цитата показана через явно обозначенный сгенерированный снимок, '
    'а не заводской PDF. Это внешний факт о классе машины: для этого факта не требуется повтор '
    'марки или слова «тандемный» в ПД. Он не подтверждает пригодность конкретного экземпляра '
    'для материала, толщины или условий проекта, массу, мощность, замену всех ресурсов нормы '
    'либо правильность сметной нормы. Не переносить данные на HD 120, HD+ или модификации с '
    'суффиксами. Сохрани отдельную проверку технологии по ПД/ПОС, условий нормы и реальных '
    'противоречий. Ни один claim или статус не подтверждается автоматически.'
)


def _empty(reason) -> dict[str, Any]:
    return dict(sources=[], preferred_source_ids=[], context={}, reason=reason)


def _relevant(original_comment, definitions):
    if not isinstance(original_comment, str) or len(original_comment) > 20000:
        return []
    # A suffix/alternative is not inferred away. The original equipment line
    # must end with this unqualified model (ordinary final punctuation allowed).
    if len(re.findall(r'\bhamm\b', original_comment, re.I)) != 1:
        return []
    lines = [line for line in original_comment.splitlines()
             if re.search(r'\bhamm[ \t]+hd[ \t]*130[ \t]*[.;)]?[ \t]*$', line, re.I)]
    if (len(lines) != 1 or not CLASS.search(lines[0])
            or re.search(r'\b(?:не|без|вместо|либо|или)\b', lines[0], re.I)):
        return []
    model = re.search(r'\bhamm[ \t]+hd[ \t]*130', lines[0], re.I)
    if not model:
        return []
    # The same class can be required for several different machines/operations.
    # Bind only to the literal functional part of this original HAMM line.
    def normalized(text):
        return re.sub(r'\s+', ' ', str(text)).strip().rstrip('.;:').casefold()
    functional_text = normalized(lines[0][:model.start()])
    claims = []
    for item in definitions:
        if not isinstance(item, dict):
            continue
        claim = item.get('claim_id')
        if (isinstance(claim, str) and len(claim) <= 160 and claim.startswith('method:')
                and item.get('predicate') != 'advisory'
                and CLASS.search(str(item.get('expected_text', '')))
                and normalized(item.get('expected_text', '')) == functional_text
                and claim not in claims):
            claims.append(claim)
    return claims if len(claims) == 1 else []


def _verified_file(root, relative, digest):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file() or path.stat().st_size > 20000000:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == digest


def _valid(item, expected):
    if any(item.get(field) != expected[key] for field, key in
           [('id', 'clause_id'), ('source_id', 'source_id'), ('source_kind', 'kind'),
            ('designation', 'designation'), ('label', 'label')]):
        return False
    text = item.get('text')
    if (item.get('entity_kind') != 'clause' or item.get('status') != REVIEW
            or not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != expected['text_sha256']):
        return False
    meta = item.get('edition') or {}
    if (meta.get('sha256') != expected['sha256'] or meta.get('kind') != expected['kind']
            or meta.get('status') != 'pdf_verified' or meta.get('ingestion') != 'reviewed_manufacturer'
            or meta.get('evidence_role') != 'manufacturer_equipment_classification'):
        return False
    proof = meta.get('provenance') or {}
    if proof.get('artifact_type') != expected['artifact_type'] or proof.get('url') != expected['url']:
        return False
    if expected['kind'] == 'MANUFACTURER_WEB_CAPTURE':
        if (proof.get('raw_html_sha256') != HTML_SHA
                or proof.get('raw_html_blob_path') != 'provenance/' + HTML_SHA + '.html'
                or proof.get('accessed_at_utc') != '2026-11-01T04:30:58Z'):
            return False
    elif (proof.get('publisher') != 'Wirtgen America Inc.'
          or proof.get('edition') != 'Winter 2002, Vol. 2, No. 2' or proof.get('publisher_physical_page') != 3):
        return False
    clauses = meta.get('reviewed_clauses', [])
    if (len(clauses) != 1 or clauses[0].get('label') != expected['label']
            or clauses[0].get('text') != text or clauses[0].get('physical_pages') != [expected['page']]
            or clauses[0].get('verification') != REVIEW):
        return False
    facts = meta.get('reviewed_facts', [])
    if len(facts) != 1:
        return False
    fact = facts[0]
    if (fact.get('subject') != dict(manufacturer='HAMM', model='HD 130')
            or fact.get('scope') != 'generic_model_class_only' or fact.get('review_status') != REVIEW
            or fact.get('matching_policy') != 'exact_manufacturer_and_model_no_suffix_inference'
            or fact.get('clause_label') != expected['label']
            or fact.get('predicate') != expected['predicate'] or fact.get('value') != expected['value']
            or not fact.get('literal_quote') or fact['literal_quote'] not in text):
        return False
    return item.get('citations') == [dict(source_id=expected['source_id'], physical_page=expected['page'],
        source_sha256=expected['sha256'], text_provenance=REVIEW, artifact_type=expected['artifact_type'])]


def manufacturer_context(kb, original_comment, claim_definitions, *, expected_pin) -> dict[str, Any]:
    """Register both immutable literal sources, never return a verdict decision."""
    claims = _relevant(original_comment, claim_definitions)
    if not claims or len(claims) > MAX_CLAIMS:
        return _empty('no_bounded_exact_model_class_question')
    pin = deepcopy(getattr(kb, 'identity', {}))
    if (not isinstance(pin, dict) or pin != expected_pin or pin.get('profile') != 'roads'
            or not all(isinstance(pin.get(k), str) and pin[k] for k in ('database', 'release_id'))
            or not re.fullmatch('[0-9a-f]{64}', str(pin.get('sha256', '')))):
        return _empty('corpus_pin_mismatch')
    cards, literals = [], []
    try:
        with kb.connect() as db:
            for expected in SOURCES:
                sid = expected['source_id']
                index = kb.source_index.get(sid, {})
                row = db.execute('SELECT sha256,blob_path,kind,designation,pages FROM sources WHERE id=?', (sid,)).fetchone()
                blob = 'blobs/' + expected['sha256'] + '.pdf'
                if (index.get('designation') != expected['designation'] or index.get('pages') != expected['pages']
                        or not row or row['sha256'] != expected['sha256'] or row['blob_path'] != blob
                        or any(row[field] != expected[field] for field in ('kind', 'designation', 'pages'))
                        or not _verified_file(kb.root, blob, expected['sha256'])):
                    return _empty('reviewed_source_unavailable')
                item = kb.entity('clause', expected['clause_id'])
                if not _valid(item, expected):
                    return _empty('reviewed_clause_set_unavailable')
                card_id = 'manufacturer:' + expected['clause_id']
                cards.append(dict(id=card_id, kind='normative', source_id=sid, page=expected['page'],
                    evidence_role='manufacturer_equipment_classification',
                    classification_claim_ids=list(claims),
                    citation_pages=[expected['page']], label=expected['designation'], text=item['text'],
                    origin=REVIEW, clause_id=expected['clause_id'], source_sha256=expected['sha256'],
                    text_sha256=expected['text_sha256'], kb_identity=deepcopy(pin)))
                literals.append(dict(source_id=card_id, quote=item['text'], physical_page=expected['page'],
                    artifact_type=expected['artifact_type'], original_url=expected['url']))
        if not _verified_file(kb.root, 'provenance/' + HTML_SHA + '.html', HTML_SHA):
            return _empty('original_html_unavailable')
    except (OSError, KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return _empty('reviewed_clause_set_unavailable')
    if getattr(kb, 'identity', None) != pin:
        return _empty('corpus_pin_changed')
    result = dict(sources=cards, preferred_source_ids=[c['id'] for c in cards], reason='reviewed_model_class_context',
        context=dict(scope='manufacturer_model_class_only', subject=dict(manufacturer='HAMM', model='HD 130'),
                     claim_ids=claims, kb=pin, instruction=SCOPE, literal_clauses=literals))
    serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if len(serialized) > MAX_CHARS or len(serialized.encode()) > MAX_UTF8_BYTES:
        return _empty('manufacturer_context_budget_exceeded')
    return result
