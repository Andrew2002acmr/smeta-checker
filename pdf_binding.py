"""Pure PDF binding helpers shared by preflight and audit workers.

Normalization (basename, backslashes, surrounding quotes, case) and
reference splitting (newline/semicolon) are identical on both paths so the
preflight table never promises a file the worker resolves differently.

An explicit file reference that is missing or ambiguous never falls back to
an unrelated sole PDF: the caller must surface an explicit error.
Fallback by basis or a sole PDF is allowed only without explicit references.
"""

import os
import re

EXACT_MATCH = "🟢 Имя совпадает"
FUZZY_MATCH = "🟡 Опечатка в символах"
NOT_FOUND = "🔴 Не найден"

_EMPTY_TOKENS = {"", "nan", "none", "null", "-"}


def split_pdf_refs(value):
    """Split a VOR file cell into individual references.

    Cells may hold several paths separated by newlines or semicolons
    (continuation rows fold extra files into the same cell layout).
    """
    if value is None:
        return []
    text = str(value)
    if text.strip().lower() in _EMPTY_TOKENS:
        return []
    parts = []
    for chunk in re.split(r"[\n\r;]+", text):
        item = chunk.strip().strip("\"'«»").strip()
        if not item or item.lower() in _EMPTY_TOKENS:
            continue
        if item not in parts:
            parts.append(item)
    return parts


def clean_pdf_name(ref):
    """Normalize one reference to the pool key form: lowercase basename."""
    text = str(ref).strip() if ref is not None else ""
    if text.lower() in _EMPTY_TOKENS:
        return ""
    return os.path.basename(text.replace("\\", "/")).strip().strip("\"'«»").lower()


def _stem(name):
    return re.sub(r"[^a-zA-Zа-яА-Я0-9]", "", os.path.splitext(name)[0]).lower()


def resolve_pdf_name(ref, pool_keys):
    """Resolve one reference against pool keys.

    Returns (matched_key or None, bind_status). Several fuzzy candidates
    are reported as ambiguity instead of taking the first one silently.
    """
    keys = list(pool_keys)
    clean = clean_pdf_name(ref)
    if not clean:
        return None, NOT_FOUND
    if clean in keys:
        return clean, EXACT_MATCH
    target = _stem(clean)
    if not target:
        return None, NOT_FOUND
    matches = []
    for key in keys:
        stem = _stem(key)
        if target == stem or target in stem or stem in target:
            matches.append(key)
    if len(matches) == 1:
        return matches[0], FUZZY_MATCH
    if len(matches) > 1:
        return None, "🔴 Неоднозначно: " + ", ".join(sorted(matches))
    return None, NOT_FOUND


def resolve_position_pdfs(refs, pool_keys, basis_code=""):
    """Resolve a position once for both preflight and execution.

    Returns (matched keys, missing references, ambiguity details, error).
    Different documents need separate page namespaces and are reported
    explicitly instead of silently auditing only the first.
    """
    keys = list(pool_keys)
    refs = [r for r in (refs or []) if clean_pdf_name(r)]
    if not refs:
        basis = re.sub(r"[^a-zA-Zа-яА-Я0-9]", "", str(basis_code)).lower()
        matches = [key for key in keys
                   if basis and _stem(key) and (basis in _stem(key) or _stem(key) in basis)]
        if len(matches) == 1:
            return matches, [], [], None
        if len(matches) > 1:
            error = "Неоднозначная привязка по основанию: " + ", ".join(sorted(matches))
            return [], [], [error], error
        if len(keys) == 1:
            return keys, [], [], None
        return [], [], [], "Не указан однозначный PDF для позиции"
    matched, missing, ambiguous = [], [], []
    for ref in refs:
        key, status = resolve_pdf_name(ref, keys)
        if key is not None and key not in matched:
            matched.append(key)
        elif key is None:
            if status.startswith("🔴 Неоднозначно"):
                ambiguous.append(status)
            else:
                missing.append(ref)
    if missing or ambiguous:
        details = [f"'{ref}' отсутствует" for ref in missing] + ambiguous
        return matched, missing, ambiguous, "Не удалось привязать PDF: " + "; ".join(details)
    if len(matched) > 1:
        return (matched, [], [],
                "Позиция ссылается на несколько PDF (" + ", ".join(matched) + "); "
                "проверка нескольких документов не поддерживается — укажите один файл")
    return matched, [], [], None
