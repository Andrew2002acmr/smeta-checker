"""Pure PDF binding helpers shared by preflight and audit workers.

Normalization (basename, backslashes, surrounding quotes, case) and
reference splitting (newline/semicolon) are identical on both paths so the
preflight table never promises a file the worker resolves differently.

An explicit file reference that is missing or ambiguous never falls back to
an unrelated sole PDF: the caller must surface an explicit error.
Fallback by basis or a sole PDF is allowed only without explicit references.

Designation helpers treat the loaded PDF as the source of truth: VOR rows
declare designations (``30/09-ТКР16.ГЧ``), the audit verifies each one is
present in the PDF text. Matching is strict — no fuzzy substring fallback.
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

_DESIGNATION_RE = re.compile(
    r"\d{2}/\d{2}\s*[-–—]?\s*[А-ЯA-Z0-9]+(?:\.[А-ЯA-Z0-9]+)?")


def normalize_designation(value):
    """Fold one designation for strict comparison.

    Case, ``ё`` and spacing/punctuation around the dash are insignificant;
    the slash,digits, letters and dots stay significant.
    """
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"\s+", "", text)
    return text.replace("–", "-").replace("—", "-")


def split_designation_refs(value):
    """Extract PD designations from a VOR reference cell.

    ``30/09-ТКР16.ГЧ л. 1,`` yields ``['30/09-ТКР16.ГЧ']``: sheet numbers
    (``л. 1``) are location hints, not designations, and stay out.
    """
    if value is None:
        return []
    text = str(value)
    if text.strip().lower() in _EMPTY_TOKENS:
        return []
    found = []
    for match in _DESIGNATION_RE.findall(text.upper()):
        item = re.sub(r"\s+", "", match).replace("–", "-").replace("—", "-")
        if item and item not in found:
            found.append(item)
    return found


def index_designations(pages):
    """Map normalized designations to 1-based page numbers.

    ``pages`` is an iterable of ``(page_number, text)`` pairs; one pass,
    no PDF access. Both full (``30/09-ТКР16.СО``) and bare
    (``30/09-ТКР16``) forms are indexed so callers can tell a missing
    section from a missing set.
    """
    index = {}
    for page_number, text in pages or []:
        for raw in split_designation_refs(text):
            key = normalize_designation(raw)
            if key:
                index.setdefault(key, [])
                if page_number not in index[key]:
                    index[key].append(page_number)
            bare = normalize_designation(raw.split(".")[0])
            if bare and bare != key:
                index.setdefault(bare, [])
                if page_number not in index[bare]:
                    index[bare].append(page_number)
    return index



def verify_designations(declared, index):
    """Check VOR-declared designations against a PDF designation index.

    Returns ``(found, missing)`` where ``found`` maps each matched
    declaration to its 1-based PDF pages and ``missing`` lists
    declarations absent from the PDF. The PDF is the source of truth:
    a missing designation is a VOR discrepancy, never a reason to
    guess another file.
    """
    found, missing = {}, []
    for raw in declared or []:
        key = normalize_designation(raw)
        if not key:
            continue
        pages = (index or {}).get(key)
        if pages:
            found[str(raw)] = list(pages)
        elif str(raw) not in missing:
            missing.append(str(raw))
    return found, missing

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


def resolve_position_pdfs(refs, pool_keys, basis_code="", bindings=None):
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
        target = (bindings or {}).get(clean_pdf_name(ref))
        if target is not None:
            key, status = (target, EXACT_MATCH) if target in keys else (None, NOT_FOUND)
        else:
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
