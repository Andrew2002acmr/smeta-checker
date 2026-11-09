"""Bounded source navigation for required work-method obligations."""

import re

from normative_source_roles import classification_source
from normative_thickness_rules import definition_text, source_definition


_METHOD_TERMS = (
    r'\b(?:асфальто|а\s*/\s*)укладчик\w*',
    r'\b(?:вибро)?кат(?:ок|к\w*)\b',
    r'\bбульдозер\w*',
    r'\bмеханизирован\w*',
    r'\b(?:травосе\w*|сеялк\w*)',
)
_SIZE = re.compile(
    r'\b(?P<size>[1-5]|перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*)'
    r'(?:-?(?:го|й))?\s+типоразмер\w*', re.I,
)
_ORDINALS = ('перв', 'втор', 'трет', 'четверт', 'пят')


def _size(text):
    match = _SIZE.search(text)
    if not match:
        return None
    word = match['size'].casefold().replace('ё', 'е')
    if word.isdecimal():
        return int(word)
    return next(index for index, prefix in enumerate(_ORDINALS, 1) if word.startswith(prefix))


def method_candidates(expected, claim_id, sources):
    """Find addressed excerpts to inspect; no compatibility follows from a hit."""
    terms = [re.compile(pattern, re.I) for pattern in _METHOD_TERMS if re.search(pattern, expected, re.I)]
    size = _size(expected)
    if not terms:
        return []

    candidates = []
    for source in sources:
        sid, text = source.get('id'), source.get('text', '')
        if (not sid or not text or not source.get('page')
                or source.get('origin') in {'uncertain_table', 'native_definition_ambiguous'}
                or str(source.get('origin', '')).startswith('ocr')):
            continue
        if classification_source(source):
            if claim_id in (source.get('classification_claim_ids') or []):
                candidates.append((0, sid, 'manufacturer_classification'))
            continue
        if not any(term.search(text) for term in terms):
            continue
        if source.get('kind') == 'project' and source.get('version_id'):
            if size is None or _size(text) == size:
                candidates.append((1, sid, 'project_method_literal'))
            continue
        if source.get('kind') != 'normative' or not source.get('source_id'):
            continue
        definition = source_definition(source)
        if definition:
            title = definition_text(text)
            # A selected code's own row is stronger than a multi-variant header.
            variant = title.partition(definition['code'])[2].partition('\n')[0]
            if size is None or _size(variant) == size:
                candidates.append((2, sid, 'norm_definition_or_composition'))
        elif size is None and text.startswith('Ресурсы нормы '):
            candidates.append((3, sid, 'norm_resource'))
    candidates.sort(key=lambda entry: entry[0])
    return [{'source_id': sid, 'role': role} for _, sid, role in candidates[:6]]
