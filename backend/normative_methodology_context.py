"""Literal reviewed methodology context, never a claim/verdict decision.

The complete clause set is required: clause 22 without clause 7's exceptions
must not enter a prompt. Additive releases may reuse the same verified text;
every card remains bound to the caller's current frozen corpus identity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from typing import Any

SOURCE_ID = 'pdf-ae59b619236cb4e878a1b9ef'
SOURCE_SHA256 = 'ae59b619236cb4e878a1b9efde11e8d082c8a82902695ebb52928b48833e02ec'
DESIGNATION = 'Приказ Минстроя России от 14.07.2022 № 571/пр. Методика применения сметных норм'
REVIEW_STATUS = 'reviewed_literal_transcription'
TRANSCRIPTION = 'visual_literal_transcription_whitespace_normalized'
CLAUSES = (
    ('5', 'clause-9f014ab86a6f9e35269d7425100d46dd', (3,),
     '1e68c4ad8f058f14671d61b8e24d58bb5200a4ed874b409f099f2f8adc985e2e'),
    ('7', 'clause-33f8d2772aed19d29783274b50f80a4b', (3, 4),
     '7933a764ffaac74b62c1edf567ca55869a8e66383b76108d671e71a2fec5cddd'),
    ('22', 'clause-d0664afac286bb9b46b121b08905b38e', (7,),
     '8feb5db524ae26de90af4180740fa463916e7e23d92cc16f805624b38f37a1b7'),
)
# Includes registry cards AND the full literal context retained by compact().
# A UTF-8 byte cap is conservative for byte-fallback tokenizers; no provider
# tokenizer is guessed and no model request is made to estimate usage.
MAX_ADDITION_CHARS = 10000
MAX_ADDITION_UTF8_BYTES = 18000
MAX_METHOD_CLAIMS = 12

SCOPE = (
    'Это методика применения сметных норм, а не доказательство пригодности конкретной машины '
    'или качества выполненного уплотнения. Пункты 5, 7 и 22 читать совместно. '
    'Не считать отсутствие марки или слова «тандемный» в ПД само по себе универсальным '
    'блокером применения сметной нормы: отдельно оцени состав работы и неизменность '
    'технологических/организационных схем по источникам. Пункт 22 рассматривает случаи, '
    'предусмотренные ПД/ПОС; он не доказывает, что любая предложенная в ВОР машина разрешена. '
    'Сохраняются специальные условия сборника по пункту 7, ограничения материала и толщины, '
    'требования ПД/ПОС и условий производства работ. Если точная модель, мощность или иная '
    'характеристика входят в проверяемое требование, их нельзя подтвердить этой общей методикой. '
    'Она не подтверждает пригодность HAMM HD130, не заменяет паспорт или необходимое '
    'обоснование технологии и не разрешает менять ресурсы, расход или коэффициенты произвольно. '
    'Не требуй будущие результаты пробного уплотнения только из-за проектной стадии ВОР; '
    'реальное ограничение проверяй адресно. Дата проекта, метод сметного расчёта и полнота '
    'изменений остаются отдельной проверкой. Ссылки на ПД и конкретную норму по-прежнему нужны; '
    'общая методика не подтверждает ни один claim автоматически.'
)


def _empty(reason) -> dict[str, Any]:
    return dict(sources=[], preferred_source_ids=[], context={}, reason=reason)


def _method_claims(definitions):
    result = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        claim = definition.get('claim_id')
        if (isinstance(claim, str) and len(claim) <= 160
                and definition.get('predicate') != 'advisory'
                and (claim == 'method' or claim.startswith('method:')
                     or definition.get('kind') in {'method', 'work_method'})):
            if claim not in result:
                result.append(claim)
    return result


def _valid_entity(item, label, entity_id, pages, text_sha):
    if (item.get('id') != entity_id or item.get('entity_kind') != 'clause'
            or item.get('source_id') != SOURCE_ID or item.get('source_kind') != 'METHODOLOGY'
            or item.get('designation') != DESIGNATION or item.get('label') != label
            or item.get('status') != REVIEW_STATUS):
        return False
    text = item.get('text')
    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != text_sha:
        return False
    metadata = item.get('edition') or {}
    if (metadata.get('sha256') != SOURCE_SHA256 or metadata.get('ingestion') != 'reviewed_methodology'
            or metadata.get('kind') != 'METHODOLOGY' or metadata.get('status') != 'pdf_verified'
            or metadata.get('role') != 'estimate_norm_application_methodology'):
        return False
    reviewed = [clause for clause in metadata.get('reviewed_clauses', []) if clause.get('label') == label]
    if (len(reviewed) != 1 or reviewed[0].get('text') != text
            or reviewed[0].get('physical_pages') != list(pages)
            or reviewed[0].get('verification') != TRANSCRIPTION):
        return False
    expected_refs = [dict(source_id=SOURCE_ID, physical_page=page,
                         source_sha256=SOURCE_SHA256, text_provenance=TRANSCRIPTION) for page in pages]
    return item.get('citations') == expected_refs


def methodology_context(kb, claim_definitions, *, expected_pin) -> dict[str, Any]:
    """Return optional cards/context for method claims from one frozen corpus.

    No searches, source writes, fabricated quotes, claim mutations or API calls.
    Missing, unreviewed or changed evidence skips the whole optional addition.
    The caller registers ``sources``, prefers ``preferred_source_ids`` and puts
    ``context`` in inputs.methodology_context. Full quotations live in context
    too, so ordinary source compaction cannot sever clause 7's restrictions.
    """
    claims = _method_claims(claim_definitions)
    if not claims:
        return _empty('no_work_method_claims')
    if len(claims) > MAX_METHOD_CLAIMS:
        return _empty('method_claim_budget_exceeded')
    pin = deepcopy(getattr(kb, 'identity', {}))
    if (not isinstance(pin, dict) or pin != expected_pin or pin.get('profile') != 'roads'
            or not all(isinstance(pin.get(key), str) and pin[key] for key in ('database', 'release_id'))
            or not re.fullmatch('[0-9a-f]{64}', str(pin.get('sha256', '')))):
        return _empty('corpus_pin_mismatch')
    source_index = getattr(kb, 'source_index', {})
    source = source_index.get(SOURCE_ID, {})
    if source.get('designation') != DESIGNATION or source.get('pages') != 27:
        return _empty('reviewed_source_unavailable')
    cards, literals = [], []
    try:
        # Metadata and literal hashes must agree with the source row itself,
        # not merely with a similarly labelled or reassigned PDF record.
        with kb.connect() as db:
            record = db.execute('SELECT sha256,blob_path,kind,designation,pages FROM sources WHERE id=?',
                                (SOURCE_ID,)).fetchone()
        if (not record or record['sha256'] != SOURCE_SHA256
                or record['blob_path'] != 'blobs/' + SOURCE_SHA256 + '.pdf'
                or record['kind'] != 'METHODOLOGY' or record['designation'] != DESIGNATION
                or record['pages'] != 27):
            return _empty('reviewed_source_identity_mismatch')
        for label, entity_id, pages, text_sha in CLAUSES:
            item = kb.entity('clause', entity_id)
            if not _valid_entity(item, label, entity_id, pages, text_sha):
                return _empty('reviewed_clause_set_unavailable')
            card_id = 'methodology:' + entity_id
            cards.append(dict(id=card_id, kind='normative', source_id=SOURCE_ID,
                page=pages[0], citation_pages=list(pages), label=DESIGNATION + ' · п. ' + label,
                text=item['text'], origin=REVIEW_STATUS, clause_id=entity_id,
                source_sha256=SOURCE_SHA256, kb_identity=deepcopy(pin)))
            literals.append(dict(source_id=card_id, clause=label, physical_pages=list(pages),
                                 quote=item['text']))
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return _empty('reviewed_clause_set_unavailable')
    # Detect a caller switching corpus identities while the three rows are read.
    if getattr(kb, 'identity', None) != pin:
        return _empty('corpus_pin_changed')
    context = dict(scope='estimate_norm_application_only', claim_ids=claims, kb=pin,
                   pdf_source_id=SOURCE_ID, source_sha256=SOURCE_SHA256,
                   instruction=SCOPE, literal_clauses=literals)
    result = dict(sources=cards, preferred_source_ids=[card['id'] for card in cards],
                  context=context, reason='reviewed_literal_context')
    serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if len(serialized) > MAX_ADDITION_CHARS or len(serialized.encode()) > MAX_ADDITION_UTF8_BYTES:
        return _empty('methodology_context_budget_exceeded')
    return result
