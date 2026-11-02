"""Manufacturer classification evidence cannot stand in for a work norm."""

import hashlib

from normative_manufacturer_context import SOURCES


CLASSIFICATION_ROLE = 'manufacturer_equipment_classification'
CLASSIFICATION_DOCUMENTS = {item['source_id'] for item in SOURCES}
CLASSIFICATION_CLAUSES = {item['clause_id'] for item in SOURCES}
MESSAGE = ('Источник производителя подтверждает только класс машины; для применимости нормы, '
    'её измерителя или расчётного основания нужен отдельный нормативный источник.')


def classification_source(source):
    """A stripped role tag never changes these immutable documents' purpose."""
    sid = str(source.get('id', ''))
    return bool(source.get('evidence_role') == CLASSIFICATION_ROLE
        or source.get('source_id') in CLASSIFICATION_DOCUMENTS
        or sid in CLASSIFICATION_DOCUMENTS
        or any(source.get('label') == item['designation']
            or str(source.get('label', '')).startswith(item['designation'] + ' · ') for item in SOURCES)
        or any(sid == 'manufacturer:' + clause or sid.startswith('manufacturer:' + clause + ':')
            for clause in CLASSIFICATION_CLAUSES))


def classification_only_basis(item, sources):
    """Only the producer's exact class claim may use the paired literal proof."""
    lookup = {s['id']: s for s in sources}
    cited = [lookup[sid] for sid in item.get('normative_source_ids', []) if sid in lookup]
    if not cited or not all(classification_source(source) for source in cited):
        return False
    claims = [claim for claim in item.get('claim_ids', [])
        if claim not in {'presence', 'quantity'} and not claim.startswith('geometry:')]
    if not claims:
        return False
    if any(not claim.startswith('method:') for claim in claims):
        return True
    # Generic retrieval/DTO-stripped sources remain manufacturer references,
    # but cannot reconstruct the private, current-position claim permission.
    proof = []
    for expected in SOURCES:
        matches = [source for source in cited if source.get('source_id') == expected['source_id']
            and source.get('id') == 'manufacturer:' + expected['clause_id']
            and source.get('evidence_role') == CLASSIFICATION_ROLE
            and source.get('source_sha256') == expected['sha256']
            and source.get('text_sha256') == expected['text_sha256']
            and hashlib.sha256(str(source.get('text', '')).encode()).hexdigest() == expected['text_sha256']
            and source.get('classification_claim_ids') == claims]
        if len(matches) != 1:
            return True
        proof.append(matches[0])
    return len(claims) != 1 or not proof[0].get('kb_identity') or proof[0]['kb_identity'] != proof[1].get('kb_identity')
