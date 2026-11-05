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
MATERIAL_CLAUSES = (CLAUSES[1],
    ('12','clause-3651e9d5ed4782554872a0caaf9b3135',(5,),
     '7b27a0faa8b5207d240a489cc9912c76cb031815865a1946201075e403a3f320'))
MATERIAL_SCOPE = (
    'Проверяется спецификация и физическое количество самостоятельного материала ВОР. '
    'Пункты 7 и 12 читать вместе: проектные данные и специальные положения сборника '
    'остаются основанием ресурса П, потери и отходы не объявляются доказанными этой ссылкой. '
    'Различай геометрический объём проектной конструкции и снабженческий расход с потерями. '
    'Не переноси выбор вида нормы родительской работы на независимое физическое количество '
    'материала; если расход взят именно из этой нормы, её область применения обязательна. '
    'Прямые проектные количества, расчёт из площади/толщины, добавки, потери и условия '
    'коэффициентов проверяются раздельно. Эта методика не подтверждает ни количество, '
    'ни совместимость материала, ни коэффициент автоматически.'
)
# Includes registry cards AND the full literal context retained by compact().
# A UTF-8 byte cap is conservative for byte-fallback tokenizers; no provider
# tokenizer is guessed and no model request is made to estimate usage.
MAX_ADDITION_CHARS = 10000
MAX_ADDITION_UTF8_BYTES = 18000
MAX_METHOD_CLAIMS = 12

# Optional addressed excerpts, not a consolidated edition or a loss verdict.
LOSS_SOURCE_SHA256 = '229859743265831e2c3043e7a68250f7df543d8c2dfde637ebf9d18e7431a017'
LOSS_SOURCE_ID = 'pdf-' + LOSS_SOURCE_SHA256[:24]
LOSS_DESIGNATION = 'Приказ Минстроя России от 16.01.2020 № 15/пр. Методика потерь и отходов'
LOSS_AMENDMENT_SHA256 = '1c15bb4c4063aa6f0706d972e3471afb962d1357dfe2ec37a2c8a30f8c039b0f'
LOSS_AMENDMENT_ID = 'pdf-' + LOSS_AMENDMENT_SHA256[:24]
LOSS_AMENDMENT_DESIGNATION = 'Приказ Минстроя России от 14.10.2024 № 695/пр. Изменения Методики 15/пр'
LOSS_FORMULA_TRANSCRIPTION = 'visual_literal_transcription_formula_linearized'
LOSS_FORMULA = 'P_Σ(ij) = P_п(ij) + P_то(ij) + P_тп(ij) + P_проч (1)'
LOSS_RENDER_SHA256 = {
    3: '102c9d46623c7be05251e556ccaa95ce137073283a3a8d603a5af5e124ea623e',
    8: '8164f0fd13d40792045f236fba8ffdc6dd20916ddacfd255209dd5bf60b2d0d8',
    9: '46b1165b5e7e670fafec53d20772c04a3bfad81140455f5908c8b54e85c1a748',
}
LOSS_CLAUSES = (
    ('6', 'clause-a9dfaddf71cd58c264ad3f8c0eb9b6a3', (3,),
     'ae4f24e3bcfb8e75dbf99e3c8de268abc4aa8088328b165b83bd02483d19b36e'),
    ('8', 'clause-8d0551530497054f2abf4c57a33b2c97', (3,),
     '66254e7ed6ba136adf03c55414114d758df95194abaf1d01d53d3022406154c7'),
    ('22', 'clause-14059d01376a45dc224bd0ba465a39ae', (8,),
     '43523bac3cf2952ba1a4e5402f3dbb0ec2fc885f523d3590e1d69e4cd639845a'),
    ('23', 'clause-e23c9bd4f1c7786b11acd88c07b3405f', (8, 9),
     'c5dd30e15e4aeb7d3f07c5ceb6a6a9682e28e7f4cc13e5c37e119a27bf8c8545'),
)
LOSS_SCOPE = (
    'Пункты 6 и 8 Методики 15/пр задают область учета потерь в общем сметном расходе '
    'ресурсов П; пункты 22 и 23 различают полезный и суммарный расход. '
    'Сначала установи предмет конкретного утверждения ВОР: проектное физическое количество, '
    'полезный расход, насыпной объём после коэффициента запаса или общий сметный расход. '
    'Геометрическая формула сама не объявляет потери нулевыми или учтёнными. '
    'Не расширяй проверку проектного физического количества до полного снабженческого '
    'расхода, если это не входит в проверяемое утверждение; общий расход и заявленные '
    'потери требуют своего основания. Коэффициент запаса на уплотнение не доказывает '
    'автоматически учёт всех потерь. Эти выдержки не подтверждают ни один claim. '
    'Проверены только адресные пункты оригинала и отсутствие их изменения приказом '
    '695/пр; полная консолидация и отсутствие иных последующих изменений не заявлены.'
)
MAX_LOSS_BUNDLE_CHARS = 14000
MAX_LOSS_BUNDLE_UTF8_BYTES = 25000

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


def _loss_context(kb, pin):
    """All four reviewed clauses or none; availability never decides necessity."""
    descriptors = (
        (LOSS_SOURCE_ID, LOSS_SOURCE_SHA256, LOSS_DESIGNATION, 44, 'material_loss_methodology'),
        (LOSS_AMENDMENT_ID, LOSS_AMENDMENT_SHA256, LOSS_AMENDMENT_DESIGNATION, 2,
         'material_loss_methodology_amendment'),
    )
    try:
        records = {}
        with kb.connect() as db:
            for sid, sha, designation, pages, role in descriptors:
                source = getattr(kb, 'source_index', {}).get(sid, {})
                row = db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone()
                if (source.get('designation') != designation or source.get('pages') != pages
                        or not row or row['sha256'] != sha or row['kind'] != 'METHODOLOGY'
                        or row['blob_path'] != 'blobs/' + sha + '.pdf'
                        or row['designation'] != designation or row['pages'] != pages):
                    return [], [], 'reviewed_loss_source_unavailable'
                metadata = json.loads(row['metadata_json'])
                if (metadata.get('sha256') != sha or metadata.get('status') != 'pdf_verified'
                        or metadata.get('role') != role or metadata.get('ingestion') != 'reviewed_loss_methodology'):
                    return [], [], 'reviewed_loss_source_identity_mismatch'
                records[sid] = metadata
        amendment = records[LOSS_AMENDMENT_ID]
        if (amendment.get('amends_source_sha256') != LOSS_SOURCE_SHA256
                or amendment.get('unchanged_reviewed_clauses') != ['6', '8', '22', '23']
                or amendment.get('verified_change_scope') != 'clause12_and_appendix4_only'
                or amendment.get('verification') != 'two_independent_visual_readings'):
            return [], [], 'reviewed_loss_amendment_unavailable'
        cards, literals = [], []
        for label, entity_id, pages, text_sha in LOSS_CLAUSES:
            item = kb.entity('clause', entity_id)
            text = item.get('text')
            provenance = LOSS_FORMULA_TRANSCRIPTION if label == '22' else TRANSCRIPTION
            metadata = records[LOSS_SOURCE_ID]
            reviewed = [c for c in metadata.get('reviewed_clauses', []) if c.get('label') == label]
            refs = [dict(source_id=LOSS_SOURCE_ID, physical_page=p, source_sha256=LOSS_SOURCE_SHA256,
                         text_provenance=provenance) for p in pages]
            if (item.get('id') != entity_id or item.get('entity_kind') != 'clause'
                    or item.get('source_id') != LOSS_SOURCE_ID or item.get('source_kind') != 'METHODOLOGY'
                    or item.get('designation') != LOSS_DESIGNATION or item.get('label') != label
                    or item.get('status') != REVIEW_STATUS or not isinstance(text, str)
                    or hashlib.sha256(text.encode()).hexdigest() != text_sha
                    or item.get('edition') != metadata or item.get('citations') != refs
                    or len(reviewed) != 1 or reviewed[0].get('text') != text
                    or reviewed[0].get('physical_pages') != list(pages)
                    or reviewed[0].get('verification') != provenance
                    or reviewed[0].get('independent_readings') != 2):
                return [], [], 'reviewed_loss_clause_set_unavailable'
            renderings = reviewed[0].get('primary_renderings')
            if (not isinstance(renderings, list) or len(renderings) != len(pages)
                    or [r.get('physical_page') for r in renderings] != list(pages)
                    or any(r.get('sha256') != LOSS_RENDER_SHA256[p]
                           for r, p in zip(renderings, pages, strict=True))):
                return [], [], 'reviewed_loss_renderings_unavailable'
            rendering_refs = [dict(physical_page=r['physical_page'], sha256=r['sha256']) for r in renderings]
            notation = reviewed[0].get('formula_notation')
            if label == '22' and (not isinstance(notation, dict)
                    or notation.get('kind') != 'linearized_visual_formula'
                    or notation.get('formula') != LOSS_FORMULA or LOSS_FORMULA not in text):
                return [], [], 'reviewed_loss_formula_provenance_unavailable'
            card_id = 'methodology:loss15:' + entity_id
            cards.append(dict(id=card_id, kind='normative', source_id=LOSS_SOURCE_ID,
                page=pages[0], citation_pages=list(pages), label=LOSS_DESIGNATION + ' · п. ' + label,
                text=text, origin=REVIEW_STATUS, clause_id=entity_id, source_sha256=LOSS_SOURCE_SHA256,
                amendment_sha256=LOSS_AMENDMENT_SHA256, kb_identity=deepcopy(pin),
                text_provenance=provenance, primary_renderings=deepcopy(rendering_refs)))
            literals.append(dict(source_id=card_id, clause=label, physical_pages=list(pages), quote=text,
                document_source_id=LOSS_SOURCE_ID, source_sha256=LOSS_SOURCE_SHA256,
                text_provenance=provenance, primary_renderings=deepcopy(rendering_refs)))
            if label == '22':
                cards[-1]['formula_notation'] = deepcopy(notation)
                literals[-1]['formula_notation'] = deepcopy(notation)
        encoded = json.dumps(dict(sources=cards, literal_clauses=literals, instruction=LOSS_SCOPE),
                             ensure_ascii=False, separators=(',', ':'))
        if len(encoded) > MAX_LOSS_BUNDLE_CHARS or len(encoded.encode()) > MAX_LOSS_BUNDLE_UTF8_BYTES:
            return [], [], 'reviewed_loss_context_budget_exceeded'
        return cards, literals, 'reviewed_addressed_loss_context'
    except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return [], [], 'reviewed_loss_clause_set_unavailable'


def methodology_context(kb, claim_definitions, *, expected_pin, material=False) -> dict[str, Any]:
    """Return optional cards/context for method claims from one frozen corpus.

    No searches, source writes, fabricated quotes, claim mutations or API calls.
    Missing, unreviewed or changed evidence skips the whole optional addition.
    The caller registers ``sources``, prefers ``preferred_source_ids`` and puts
    ``context`` in inputs.methodology_context. Full quotations live in context
    too, so ordinary source compaction cannot sever clause 7's restrictions.
    """
    claims = _method_claims(claim_definitions)
    clauses=CLAUSES
    if material:
        claims=list(dict.fromkeys(['normative:applicability',*[d['claim_id'] for d in claim_definitions
            if d.get('claim_id','').startswith(('basis:','derived:','quantity:consumption'))]]))
        clauses=MATERIAL_CLAUSES
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
        for label, entity_id, pages, text_sha in clauses:
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
    context: dict[str, Any] = dict(scope='material_quantity_basis_only' if material else 'estimate_norm_application_only', claim_ids=claims, kb=pin,
                   pdf_source_id=SOURCE_ID, source_sha256=SOURCE_SHA256,
                   instruction=MATERIAL_SCOPE if material else SCOPE, literal_clauses=literals)
    result: dict[str, Any] = dict(sources=cards, preferred_source_ids=[card['id'] for card in cards],
                  context=context, reason='reviewed_literal_context')
    serialized = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if len(serialized) > MAX_ADDITION_CHARS or len(serialized.encode()) > MAX_ADDITION_UTF8_BYTES:
        return _empty('methodology_context_budget_exceeded')
    if material:
        loss_cards, loss_literals, loss_reason = _loss_context(kb, pin)
        # Optional loss evidence must never remove or shorten the base 571 set.
        if getattr(kb, 'identity', None) != pin:
            return _empty('corpus_pin_changed')
        result['loss_context_reason'] = loss_reason
        if loss_cards:
            result['sources'].extend(loss_cards)
            result['preferred_source_ids'].extend(card['id'] for card in loss_cards)
            result['context']['literal_clauses'].extend(loss_literals)
            result['context']['instruction'] += ' ' + LOSS_SCOPE
            result['context']['loss_basis'] = dict(source_id=LOSS_SOURCE_ID, source_sha256=LOSS_SOURCE_SHA256,
                amendment_source_id=LOSS_AMENDMENT_ID, amendment_sha256=LOSS_AMENDMENT_SHA256,
                edition_scope='addressed_original_clauses_unchanged_by695_not_full_consolidation')
    return result
