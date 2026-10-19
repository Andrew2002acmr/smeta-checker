"""Page selection and audit orchestration, independent of Streamlit and PDF objects."""

import json
import math
import re
from collections import Counter
from decimal import Decimal
import unicodedata

from audit_models import AuditResult, LlmAuditResponse
from audit_tracing import span

TECHNICAL_ERROR = "Технический сбой"
INCOMPLETE = "Неполная проверка"

def result_level(status):
    """Map an audit outcome to an observation level without touching the result."""
    if status == TECHNICAL_ERROR:
        return "ERROR"
    if status == INCOMPLETE:
        return "WARNING"
    return "DEFAULT"


def empty_result(status, message):
    return AuditResult(
        status=status, work_found=False, volume_confirmed=False,
        unit_confirmed=False, comment_confirmed=False,
        discrepancy_details=str(message)).to_legacy()


def technical_error(message):
    return empty_result(TECHNICAL_ERROR, message)


class BudgetExceeded(Exception):
    pass


def _prompt_reserve(prompt, max_output_tokens):
    # Halved UTF-8 bytes: measured ~0.26 tokens/byte on Russian PDF prompts
    # (DeepSeek byte-level BPE), so half bytes keeps ~2x headroom over the
    # observed ratio. Output is reserved in full. Without provider usage
    # (including failed attempts), keep this charge.
    return len(prompt.encode("utf-8")) // 2 + max_output_tokens


class RequestBudget:
    """Count every HTTP attempt, including retries, before sending it."""

    def __init__(self, max_requests=6, max_total_tokens=60000, max_output_tokens=6000):
        if min(max_requests, max_total_tokens, max_output_tokens) <= 0:
            raise ValueError("Бюджеты должны быть положительными")
        self.max_requests = max_requests
        self.max_total_tokens = max_total_tokens
        self.max_output_tokens = max_output_tokens
        self.requests = 0
        self.tokens = 0
        self.reserved = 0

    def reserve(self, prompt):
        reserve = _prompt_reserve(prompt, self.max_output_tokens)
        if self.requests >= self.max_requests or self.tokens + reserve > self.max_total_tokens:
            raise BudgetExceeded("Исчерпан общий бюджет запросов или токенов на позицию")
        self.requests += 1
        self.tokens += reserve
        self.reserved = reserve

    def record(self, usage):
        total = getattr(usage, "total_tokens", None)
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            self.tokens += total - self.reserved
        self.reserved = 0

    def refund(self):
        """Return the last reservation when no HTTP response arrived.

        A failed attempt (network/5xx before any usage) must not consume
        the token budget: without provider usage there is nothing spent.
        The request counter stays — the attempt happened.
        """
        self.tokens -= self.reserved
        self.reserved = 0

    def can_retry(self, prompt):
        """A retry must leave one equally sized attempt for another context."""
        reserve = _prompt_reserve(prompt, self.max_output_tokens)
        return (self.requests + 2 <= self.max_requests
                and self.tokens + 2 * reserve <= self.max_total_tokens)

    def can_retry_same_context(self, prompt):
        """A same-context fix retry needs room for one more attempt only."""
        reserve = _prompt_reserve(prompt, self.max_output_tokens)
        return (self.requests + 1 <= self.max_requests
                and self.tokens + reserve <= self.max_total_tokens)


def parse_page_reference(value, page_count):
    """Return physical PDF indices, preserving warnings for invalid references."""
    raw = str(value).strip() if value is not None else ""
    if raw.lower() in {"", "nan", "none", "null", "-"}:
        return [], "Страница не указана", []
    # Excel floats can also occur in folded continuation rows.
    raw = re.sub(r"(?<![\d.])(\d+)\.0+(?![\d.])", r"\1", raw)
    if not re.fullmatch(r"\d+(?:\s*[-–—]\s*\d+)?(?:[\s,;]+\d+(?:\s*[-–—]\s*\d+)?)*", raw):
        return [], f"Не распознан номер страницы '{value}'", []
    pages, invalid = [], []
    for match in re.finditer(r"(\d+)(?:\s*[-–—]\s*(\d+))?", raw):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            invalid.append(match.group(0))
            continue
        for number in range(start, end + 1):
            if number not in pages:
                pages.append(number)
    description = f"Стр. {', '.join(map(str, pages))}" if pages else "Страницы не определены"
    if invalid:
        description += f"; недопустимые страницы: {', '.join(invalid)} (PDF: {page_count} стр.)"
    return [p - 1 for p in pages], description, pages


_STOP_WORDS = set("для при или как под над без работы работ объем объема количество проект документация лист страница согласно устройство выполнение установка монтаж демонтаж категория категории сложности условия условиях полевых выполненная выполненных применением".split())
_CODE_RE = re.compile(r"(?<!\w)(?=[a-zа-яё0-9-]*[0-9])[a-zа-яё0-9]+(?:-[a-zа-яё0-9]+)*(?!\w)")
_UNIT_RE = re.compile(r"[a-zа-яё]+/[a-zа-яё]+")


def _terms(text):
    text = unicodedata.normalize("NFKC", str(text)).lower().replace("ё", "е")
    result = set()
    for word in re.findall(r"[a-zа-яё]{3,}", text):
        if word in _STOP_WORDS:
            continue
        stem = re.sub(r"(иями|ами|ого|ему|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)$", "", word)
        result.add((stem if len(stem) >= 3 else word)[:6])
    for code in _CODE_RE.findall(text):
        if len(code) >= 2 and any(c.isalpha() or c == "-" for c in code):
            result.add(code)
    result.update(_UNIT_RE.findall(text))
    return result


def _compact_terms(text):
    """Term overlap that tolerates OCR splits like `г и бкими бе тонными`."""
    terms = _terms(text)
    tokens = _plain_page(text).split()
    spellings = set()
    for width in (2, 3):
        for start in range(len(tokens) - width + 1):
            window = tokens[start:start + width]
            if all(re.fullmatch(r"[а-яёa-z]+", token) for token in window):
                joined = "".join(window)
                if 3 <= len(joined) <= 24:
                    spellings.add(joined)
    return terms | _terms(" ".join(spellings))


def _has_digit(token):
    return any(c.isdigit() for c in token)


_THOUSANDS_RE = re.compile(r"(?<!\w)(\d{1,3}(?:[ \u00a0\u2009]\d{3})+)(?!\w)")


def _squeeze_thousands(text):
    return _THOUSANDS_RE.sub(
        lambda m: m.group(1).replace(" ", "").replace(" ", "").replace(" ", ""),
        text)


def _numbers(text):
    """Comparable decimal tokens without losing significant fractional digits."""
    text = _squeeze_thousands(unicodedata.normalize("NFKC", str(text)))
    return {Decimal(token.replace(",", "."))
            for token in re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?", text)}


def _close_numbers(first, second):
    """Tolerant number-set intersection for workbook float tails.

    Cached Excel doubles (631.1999999999999) never equal exact schedule
    decimals (631,2); relative 1e-9 tolerance (project norm) bridges
    the binary artifact without merging genuine decimals.
    Returns the matching subset of ``first``.
    """
    tol = Decimal("1e-9")
    hits = set()
    for value in first:
        base = max(abs(value), Decimal(1))
        if any(abs(value - other) <= base * tol for other in second):
            hits.add(value)
    return hits


def format_ru(value):
    """Format a Decimal for Russian remarks: kill float tails, keep precision.

    Quantizes binary-float artifacts (78.89999999999999 → 78,90) at 1e-6
    while preserving genuine decimals (310,035; 0,165; 165). Fractional
    values keep at least two decimals (78,90 not 78,9); integers stay
    bare. Comma is the decimal separator. Non-numeric input passes
    through as text.
    """
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value).strip().replace(",", "."))
        except Exception:
            return str(value)
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    quantized = value.quantize(Decimal("0.000001"))
    text = format(quantized, "f").rstrip("0")
    head, _, tail = text.partition(".")
    while len(tail) < 2:
        tail += "0"
    return f"{head},{tail}"


_TABLE_MARKERS = re.compile(r"объем|объём|количеств|ед\.\s*изм|таблиц|спецификац|м2|м²|мм|толщин", re.IGNORECASE)
_SCOPE_STEMS = ("площадк", "участ", "территори", "объект", "сооружени",
                "корпус", "блок", "систем", "зон")
_SECTION_STEMS = ("сечени", "разрез", "профил", "section", "sect", "сеч")
_TAG_RE = re.compile(r"<[^>]+>")


def _spaced(word):
    """Match OCR-split letters where safe: spaces inside one token."""
    return "".join(re.escape(char) + r"\s*" for char in word)


def _spaced_word(word):
    """Match an abbreviation with optional OCR spacing (Cyrillic-safe)."""
    return "".join(re.escape(char) + r"[ ]*" for char in word)


def _stem_group(stems):
    ordered = sorted(stems, key=len, reverse=True)
    return (r"(?<![A-Za-zА-Яа-яЁё0-9])(?:"
            + "|".join(_spaced(stem) for stem in ordered) + r")[а-яёa-z]*")


_SCOPE_LABEL_RE = re.compile(
    _stem_group(_SCOPE_STEMS)
    + r"\s*[:№#\-–—]?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-/]*"
      r"(?:\s+[A-Za-zА-Яа-яЁё0-9\-/]+){0,2})",
    re.IGNORECASE)
_SECTION_ID_RE = re.compile(
    _stem_group(_SECTION_STEMS) + r"\s*\.?\s*(?:№\s*)?(\d+)\s*[-–—/]\s*(\d+)",
    re.IGNORECASE)

def _plain_page(text):
    """Searchable text: markup becomes spaces, rows keep line boundaries."""
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"<\s*br\s*/?\s*>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = text.replace(" ", "")
    text = re.sub(r"м\s*2(?!\w)", "м2", text, flags=re.IGNORECASE)
    return re.sub(r"[^\S\n]+", " ", text).strip()


def _clean_scope_name(raw):
    """Keep a leading name; continuations must be abbreviations, not quantities."""
    kept = []
    for pos, token in enumerate(str(raw).split()):
        token = token.strip("-–—/,;:.()\"'«»")
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9\-/]+", token or ""):
            break
        if pos == 0:
            if not re.search(r"[A-ZА-ЯЁ]", token):
                break
        elif token != token.upper() or not re.search(r"[A-ZА-ЯЁ]", token):
            break
        kept.append(token)
    display = " ".join(kept).upper()
    if len(re.sub(r"[^A-ZА-ЯЁ0-9]", "", display)) < 2:
        return ""
    return display

def _scope_key(display):
    return re.sub(r"[^A-ZА-ЯЁ0-9]", "", display.upper())


def _scope_mention_re(display):
    """Bare scope reference tolerating OCR letter spacing, e.g. `Л О С`."""
    parts = []
    for token in display.split():
        atoms = [atom for atom in re.split(r"[-/]+", token) if atom]
        pieces = [(_spaced_word(atom) if len(atom) <= 3 else _spaced(atom))
                  for atom in atoms]
        glue = r"[ ]*[-–—/]?[ ]*" if len("".join(atoms)) <= 3 else r"\s*[-–—/]?\s*"
        parts.append(glue.join(pieces))
    body = r"\s+".join(parts)
    return re.compile(r"(?<![A-Za-zА-Яа-яЁё0-9])" + body
                      + r"(?![A-Za-zА-Яа-яЁё0-9])", re.IGNORECASE)


def _section_mentions(text):
    """(position, normalized id) for generic section references."""
    found = []
    for match in _SECTION_ID_RE.finditer(text):
        found.append((match.start(), f"{int(match.group(1))}-{int(match.group(2))}"))
    return found


def _section_caption(text, pos):
    """A section id in a title or short heading outranks a note mention."""
    if pos < 240:
        return True
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start:end if end >= 0 else len(text)].strip()
    return len(line) <= 80


_BARE_SCOPE_RE = re.compile(r"[A-ZА-ЯЁ]{2,}[A-ZА-ЯЁ0-9]*(?:[-/][A-ZА-ЯЁ0-9]+)*")
_MEASURE_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(м2|мм|см|м)(?!\w)", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"(?<![\w.,-])(\d+(?:[.,]\d+)?)(?![\w.,-])")
_MERGED_NUMBER_RE = re.compile(r"(?<![\w.,-])(\d+(?:[.,]\d+)?)[ ](\d+)(?![\w.,-])")
_PAREN_RE = re.compile(r"\(([^()]{1,40})\)")


def _known_scopes(*texts):
    """Scope registry from labelled names; keys are comparison keys."""
    known = {}
    for text in texts:
        for match in _SCOPE_LABEL_RE.finditer(_plain_page(text)):
            display = _clean_scope_name(match.group(1))
            if display:
                known.setdefault(_scope_key(display), display)
    return known


def _mention_res(known):
    return {key: _scope_mention_re(display) for key, display in known.items()}


def _mentioned(mention_res, text):
    return {key for key, pattern in mention_res.items() if pattern.search(text)}


def _bare_scope_names(segment, known=()):
    """Uppercase abbreviations in parens, e.g. `526 м2 (КНС)` without a label."""
    allowed = {key for key in (known or {})} | {name.upper() for name in (known or {}).values()}
    names = set()
    for match in _PAREN_RE.finditer(segment):
        inner = re.sub(r"\s+", "", match.group(1)).strip("-–—/,;:.\"'«»")
        if _BARE_SCOPE_RE.fullmatch(inner or "") and (not allowed or inner.upper() in allowed):
            names.add(inner.upper())
    return names


def _segment_spans(text):
    """Split text into rows: blank lines, newlines, semicolons."""
    spans, start = [], 0
    for match in re.finditer(r"\n\s*\n|\n|;", text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans

def _label_scope_spans(segment, mention_res, known):
    """Scope labels with their spans for adjacency binding."""
    collapsed = re.sub(r"\s+", " ", segment).strip()
    hits = []
    for key, display in known.items():
        pattern = mention_res[key]
        for match in pattern.finditer(segment):
            hits.append((match.start(), match.end(), display))
            continue
        letters_only = re.sub(r"[^A-ZА-ЯЁ]", "", collapsed.upper())
        if len(display) >= 2 and letters_only == _scope_key(display):
            hits.append((0, len(segment), display))
    return hits


def _split_label_scope_names(segment, mention_res, known):
    """Scope names from text whose labels OCR splits across newlines/letters."""
    return {display for _, _, display in _label_scope_spans(segment, mention_res, known)}


def _row_scopes(segment, mention_res, known):
    """Scopes bound to one row: labels or bare `526 м2 (КНС)` abbreviations."""
    return (_split_label_scope_names(segment, mention_res, known)
            | _bare_scope_names(segment, known))


def _plain_scopes(text):
    """All scope names mentioned in one text, via the shared registry."""
    plain = _plain_page(text)
    known = _known_scopes(plain)
    mention_res = _mention_res(known)
    return {display for _, _, display in _label_scope_spans(plain, mention_res, known)}


def measurement_scopes(text, value, unit, scope_hint=""):
    """Scopes bound to one dimensioned measurement by adjacent scope text.

    Binds each number+unit to the nearest scope mention: an enclosing or
    trailing parenthetical (``526м2 (площадка КНС)``), or the closest label
    in the same row. A row naming two scopes binds each measurement to its
    own parenthetical only; without an adjacent scope the result is empty.
    An optional ``scope_hint`` names the expected scope (comparison key or
    display); ambiguous rows then keep only the hinted scope when present.
    Generic: no document or scope names are hardcoded.
    """
    plain = _plain_page(text)
    try:
        target = Decimal(str(value).replace(",", "."))
    except Exception:
        return set()
    want_unit = _quote_text(str(unit)).replace(" ", "")
    measures = [(match.start(), match.end()) for match in _MEASURE_RE.finditer(plain)
                if Decimal(match.group(1).replace(",", ".")) == target
                and match.group(2).lower() == want_unit]
    unitless = want_unit.strip().lower() in {"", "шт", "шт.", "компл", "т", "кг"}
    if not measures and unitless:
        measures = [(match.start(), match.end()) for match in _BARE_NUMBER_RE.finditer(plain)
                    if Decimal(match.group(1).replace(",", ".")) == target]
    if not measures:
        return set()
    known = _known_scopes(plain, re.sub(r"\s+", " ", plain))
    mention_res = _mention_res(known)
    hint = _scope_key(scope_hint) if scope_hint else ""
    hint_display = next((display for key, display in known.items() if key == hint), "")
    if scope_hint and hint and not hint_display:
        hint_display = scope_hint.upper()
    spans = _segment_spans(plain)
    bound = set()
    for start, end in measures:
        seg_start = next(a for a, b in spans if a <= start < b)
        seg_end = next(b for a, b in spans if a <= start < b)
        segment = plain[seg_start:seg_end]
        labels = _label_scope_spans(segment, mention_res, known)
        groups = []
        for paren in _PAREN_RE.finditer(segment):
            top, bottom = paren.start(), paren.end()
            scopes = ({display for pos, stop, display in labels
                       if pos >= top and stop <= bottom}
                      | {name for name in _bare_scope_names(paren.group(1), known)})
            if scopes:
                groups.append((top, bottom, scopes))
        adjacent = set()
        touching = [(top, bottom, scopes) for top, bottom, scopes in groups
                    if top + seg_start - 1 <= end <= bottom + seg_start]
        if touching:
            adjacent |= min(touching)[2]
        if not adjacent and hint_display:
            hinted = [scopes for _, _, scopes in groups if hint_display in scopes]
            adjacent |= set().union(*hinted) if hinted else set()
        bound |= adjacent
    return bound


def section_scopes(text):
    """Normalized section id to scopes named in the same row.

    A section without a row scope inherits the page scope only when the
    page names exactly one scope; otherwise it maps to an empty set.
    Generic: section ids and scopes come from the text, never hardcoded.
    """
    plain = _plain_page(text)
    known = _known_scopes(plain)
    mention_res = _mention_res(known)
    found = {}
    for start, end in _segment_spans(plain):
        segment = plain[start:end]
        scopes = _row_scopes(segment, mention_res, known)
        for _, section_id in _section_mentions(segment):
            found.setdefault(section_id, set()).update(scopes)
    if len(known) == 1:
        only = next(iter(known.values()))
        for scopes in found.values():
            if not scopes:
                scopes.add(only)
    return found


_SCOPE_STEM_TERMS = set().union(*(_terms(stem) for stem in _SCOPE_STEMS))


def _labelled_scope(segment, scope_key):
    """True when a scope-stem noun governs the scope name in the segment."""
    for match in _SCOPE_LABEL_RE.finditer(_plain_page(segment)):
        name = _clean_scope_name(match.group(1))
        if name and _scope_key(name) == scope_key:
            return True
    return False


def _schedule_item_rows(raw, known, mention_res):
    """Yield (header scope, row text, row cells) for item rows under a group.

    Structural rule, no document keywords: a group header is a row with a
    filled description cell and empty quantity cells; rows carrying a
    quantity inherit the current group. A header names its scope when it
    mentions exactly one known scope. Headers are never yielded.
    """
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, flags=re.DOTALL):
        cells = [_plain_page(cell).strip()
                 for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL)]
        cells = [cell for cell in cells
                 if not re.fullmatch(r"\s*\d+\s*-\s*нов\.\s*", cell)]
        rows.append(cells)
    current = ""
    for row in rows:
        joined = " | ".join(_MERGED_NUMBER_RE.sub(r"\1 | \2", cell) for cell in row)
        tail = row[2:]
        tail_numbers = any(_numbers(cell) for cell in tail)
        description = " ".join(row[:2]).strip()
        if description and not tail_numbers:
            marked = {display for display in _row_scopes(joined, mention_res, known)}
            current = next(iter(marked)) if len(marked) == 1 else ""
            continue
        if not current or len([cell for cell in row if cell]) < 3 or not any(len(cell) > 8 for cell in row[:4]):
            continue
        if not tail_numbers:
            continue
        if not any(re.fullmatch(r"\s*(м3|м³|м2|м²|мм|см|м|шт\.?|кг|т|л|компл|пог\.?\s*м)\s*",
                                cell, re.IGNORECASE) for cell in row[1:]):
            continue
        yield current, joined, tuple(row)


def _scoped_resource_scopes(texts, skip, task_terms, numbers, known, mention_res):
    """Count qualifying schedule rows per scope for operation anchoring.

    A row qualifies on the exact expected spec count (`numbers` includes
    the target amount) plus at least two shared work/material terms with
    one distinctive term; plate-only generic overlap never qualifies.
    Exact match keeps merged codes like `165 831` from counting. Unique
    scope wins; ambiguity or nothing yields empty.
    """
    found = Counter()
    if not numbers or not task_terms:
        return found
    for idx, raw in enumerate(texts):
        if idx in skip:
            continue
        text = _plain_page(raw)
        if "поставщик" not in text.lower() or "наименован" not in text.lower():
            continue
        for header, joined, row in _schedule_item_rows(raw, known, mention_res):
            shared = task_terms & _terms(joined)
            cells = [cell for cell in row[4:] if cell] or [cell for cell in row[1:] if cell][-1:]
            cell_numbers = set().union(*[_numbers(cell) for cell in cells]) if cells else set()
            numbers_hit = _close_numbers(cell_numbers, numbers)
            if re.search(r"пбзгу", joined, re.IGNORECASE):
                numbers_hit -= {n for n in numbers_hit if n == 1}
            generic = shared <= {"плит", "бетонн", "гибк", "геотек", "укладк", "креплен",
                                 "работ", "устрой", "монтаж", "установ"}
            exact = numbers_hit and len(cell_numbers) <= 2
            if numbers_hit and len(shared) >= 2 and (not generic or exact):
                found[_scope_key(header)] += 1
    return found

class PageTextIndex:
    def __init__(self, texts, errors=None, pending_ocr=(), table_rows=None):
        self.texts = tuple(texts)
        self.errors = dict(errors or {})
        self.pending_ocr = tuple(sorted(set(pending_ocr)))
        self.table_rows = tuple(tuple(row) for row in table_rows) if table_rows is not None else None
        self.terms = tuple(_terms(text) for text in self.texts)
        frequency = Counter(term for terms in self.terms for term in terms)
        self.weights = {term: math.log(1 + len(self.texts) / count) for term, count in frequency.items()}

    def _score(self, text, work_name, comment, reference, volume, prefer_volume=False,
               *, quantity_search=False, formula_numbers=()):
        terms = _terms(text)
        qwork, qcomm = _terms(work_name), _terms(comment)
        if quantity_search or prefer_volume:
            # Once work is located, its exact material code must not crowd
            # out quantity-bearing pages using another spelling of that code.
            qwork = {term for term in qwork if not _has_digit(term)} or qwork
        inter = terms & qwork
        score = sum((8 if _has_digit(t) else 4) * self.weights.get(t, 1) for t in inter)
        if qwork:
            score += 8 * len(inter) / len(qwork)
        cinter = terms & qcomm
        score += sum(self.weights.get(t, 1) for t in cinter)
        if qcomm:
            score += 4 * len(cinter) / len(qcomm)
        numbers = _numbers(text)
        if numbers & _numbers(volume):
            score += 1000 if prefer_volume else 4
        if numbers.intersection(formula_numbers):
            score += 500
        if numbers and _TABLE_MARKERS.search(text):
            score += 5 if prefer_volume else 2
        sections = re.findall(r"(?:подраздел|раздел)\s+(\d+(?:\.\d+)+)", reference.lower())
        if any(section in text for section in sections):
            score += 4
        return score

    def candidates(self, work_name, comment, reference, volume, excluded=()):
        excluded = set(excluded)
        ranked = []
        for idx, text in enumerate(self.texts):
            if idx in excluded or not text.strip():
                continue
            score = self._score(text, work_name, comment, reference, volume)
            if score > 0:
                ranked.append((score, idx))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [idx for _, idx in ranked]

    def support_candidates(self, work_name, volume, unit, excluded=(), *, formula_numbers=()):
        """Find quantity-bearing pages for an already located work, not another location search."""
        query, excluded = _terms(work_name), set(excluded)
        expected = _numbers(volume)
        ranked = []
        for idx, text in enumerate(self.texts):
            if idx in excluded:
                continue
            numbers = _numbers(text)
            # A differing quantity is still evidence. Tables may use a header
            # rather than repeating the work name in every row.
            if not numbers or not (self.terms[idx] & query or _TABLE_MARKERS.search(text)):
                continue
            score = self._score(text, work_name, "", "", volume, quantity_search=True,
                                formula_numbers=formula_numbers)
            if _TABLE_MARKERS.search(text):
                score += 5
            if numbers & expected:
                score += 2
            if self.table_rows is not None and idx < len(self.table_rows):
                for row in self.table_rows[idx]:
                    row_terms = _terms(row)
                    row_numbers = _numbers(row)
                    if query & row_terms and expected & row_numbers:
                        score += 10
                        break
            if unit and (_terms(unit) & self.terms[idx]
                         or re.search(r"(?<!\w)" + re.escape(unit) + r"(?!\w)", text, re.IGNORECASE)):
                score += 3
            ranked.append((score, idx))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [idx for _, idx in ranked]

    def related_context(self, target_indices, work_name, volume, formula="",
                        comment=""):
        """Link an anchored scope to named sections and scoped quantity rows.

        Returns (scope, related_indices): the anchored project scope display
        name ("" when nothing anchors one) plus bounded, ordered extra page
        indices extending the caller's primary pages. Scope comes from an
        explicit work/comment scope, a dimensioned formula operand bound to
        a scope parenthetical on the anchor page, a uniquely scoped
        schedule resource row on a primary page first (header + expected
        quantity + shared work/material terms), or the same match on
        non-primary pages.
        Sections link when their row carries the anchored scope; a labelled
        mention, a different object qualifier never links, and the section
        nearest the anchor page comes first. Schedule tables link when the
        header scope's group contains the formula operand and shares task
        terms (or shares terms while the operand sits under the same
        header). Bare numbers, work-word overlap, and page proximity alone
        never qualify. Source texts are untouched; no pages or scopes are
        hardcoded.
        """
        targets = [i for i in dict.fromkeys(target_indices or [])
                   if 0 <= i < len(self.texts)]
        if not targets:
            return "", []
        plains = [_plain_page(text) for text in self.texts]
        known = _known_scopes(
            *plains, _plain_page(work_name), _plain_page(comment))
        if not known:
            return "", []
        mention_res = _mention_res(known)

        def mentioned(text):
            return {key for key, pattern in mention_res.items()
                    if pattern.search(text)}

        explicit = mentioned(_plain_page(work_name) + "\n" + _plain_page(comment))
        measured_operands = _numbers(formula or "")
        scope_units = ("м2", "мм", "см", "м")
        evidence = Counter()
        for i in targets:
            for start, end in _segment_spans(plains[i]):
                segment = plains[i][start:end]
                for number in _numbers(segment) & measured_operands:
                    bound = set()
                    for unit in scope_units:
                        bound |= measurement_scopes(segment, number, unit)
                    for display in bound:
                        evidence[_scope_key(display)] += 1
        anchored = set(explicit) | set(evidence)
        target_numbers = set(measured_operands)
        scope_key = ""
        primary_resource = _scoped_resource_scopes(
            [self.texts[i] for i in targets], set(),
            _terms(work_name) | _terms(comment),
            set(measured_operands) | _numbers(volume), known, mention_res)
        if len(primary_resource) == 1:
            scope_key = next(iter(primary_resource))
            anchored = {scope_key}
            target_numbers = set(measured_operands) | _numbers(volume)
        if not anchored and measured_operands | _numbers(volume):
            resource_numbers = set(measured_operands) | _numbers(volume)
            resource = _scoped_resource_scopes(
                self.texts, set(targets), _terms(work_name) | _terms(comment),
                resource_numbers, known, mention_res)
            if len(resource) == 1:
                scope_key = next(iter(resource))
                anchored = {scope_key}
                target_numbers = set(resource_numbers)
        if not anchored:
            return "", []
        if not scope_key:
            scope_key = sorted(
                anchored,
                key=lambda key: (key not in explicit, -evidence.get(key, 0), key))[0]
        anchor_text = "\n".join(plains[i] for i in targets)
        scope_rows = {display for start, end in _segment_spans(anchor_text)
                      for display in _row_scopes(
                          anchor_text[start:end], mention_res, known)}
        linked_ids = set()
        for start, end in _segment_spans(anchor_text):
            segment = anchor_text[start:end]
            nearby = set(_row_scopes(segment, mention_res, known))
            for _, section_id in _section_mentions(segment):
                section_row_scopes = set(section_scopes(segment).get(section_id, ()))
                if not section_row_scopes:
                    section_row_scopes = nearby or scope_rows or {known[scope_key]}
                if known[scope_key] in section_row_scopes:
                    linked_ids.add(section_id)
        target_set = set(targets)
        linked, specified = [], []
        query = (_terms(work_name) | _terms(comment)) - _SCOPE_STEM_TERMS
        for idx, text in enumerate(plains):
            if idx in target_set or not self.texts[idx].strip():
                continue
            page_scopes = mentioned(text)
            if linked_ids:
                hits = [(pos, sid) for pos, sid in _section_mentions(text)
                        if sid in linked_ids]
                if hits:
                    scored = []
                    for start, end in _segment_spans(text):
                        segment = text[start:end]
                        ids = {sid for _, sid in _section_mentions(segment)}
                        if not (ids & linked_ids):
                            continue
                        row_keys = {_scope_key(display) for display in _row_scopes(
                            segment, mention_res, known)}
                        if scope_key in row_keys:
                            labelled = _labelled_scope(segment, scope_key)
                            scored.append((0 if labelled else 1, start, idx))
                        elif not _row_scopes(segment, mention_res, known) \
                                and page_scopes == {scope_key}:
                            scored.append((2, start, idx))
                    if scored:
                        scored.sort()
                        rank, seg_start, _ = scored[0]
                    anchor_rank = len(anchor_text)
                    anchor_ids = {sid for _, sid in hits}
                    for position, identifier in _section_mentions(anchor_text):
                        if identifier in anchor_ids and known[scope_key] in (
                                set(section_scopes(anchor_text[position-150:position+150]).get(identifier, ()))
                                or scope_rows or {known[scope_key]}):
                            anchor_rank = min(anchor_rank, position)
                    if scored:
                        caption = any(_section_caption(text, pos) for pos, _ in hits)
                        adjacent = abs(idx - targets[0])
                        linked.append(((0 if caption else 1, anchor_rank, rank, adjacent,
                                       0 if scope_key in page_scopes else 1, idx), idx))
                        continue
                    continue
            raw = self.texts[idx]
            table = ("поставщик" in text.lower() and "наименован" in text.lower())
            if table and scope_key in page_scopes:
                items = [(header, joined) for header, joined, _ in _schedule_item_rows(raw, known, mention_res)]
                scoped = [joined for header, joined in items
                          if _scope_key(header) == scope_key]
                if not scoped:
                    continue
                numbers = set().union(*[_numbers(joined) for joined in scoped])
                linked_numbers = _close_numbers(set(target_numbers), numbers)
                if not linked_numbers:
                    continue
                group_terms = set().union(*[_terms(joined) for joined in scoped])
                if len(query & group_terms) < 2 or (query & group_terms) <= {
                        "плит", "бетонн", "гибк", "геотек", "укладк", "креплен",
                        "работ", "устрой", "монтаж", "установ"}:
                    continue
                if not any(_close_numbers(row_numbers, linked_numbers) for joined in scoped
                           for row_numbers in [_numbers(joined)]):
                    continue
                specified.append(idx)
        ordered = [idx for _, idx in sorted(linked)] + specified
        return known[scope_key], ordered[:3]


    def fragment(self, idx, work_name, comment, reference, volume, limit, prefer_volume=False,
                 *, formula_numbers=()):
        """Keep source-order intervals, preferring line boundaries and relevant headers."""
        if limit <= 0:
            return ""
        text = self.texts[idx]
        if len(text) <= limit:
            return text
        # Table headings commonly precede the highest-scoring numerical row.
        header_end = min(240, limit // 5)
        newline = text.rfind("\n", 0, header_end)
        if newline > 0:
            header_end = newline + 1
        if not self._score(text[:header_end], work_name, comment, reference, volume, prefer_volume,
                           formula_numbers=formula_numbers):
            header_end = 0
        intervals = [(0, header_end)] if header_end else []
        separator = "\n[...]\n"
        # A highest-scoring window must fit in full, even with a distant header.
        capacity = max(1, limit - header_end - (len(separator) if header_end else 0))
        size = min(1400, capacity)
        step = max(1, size - min(200, size // 4))
        starts = range(0, len(text), step)
        windows = []
        for start in starts:
            end = min(start + size, len(text))
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            # An oversized line must keep local windows, including its tail.
            if line_end - line_start <= capacity:
                start, end = line_start, line_end
            windows.append((self._score(text[start:end], work_name, comment, reference,
                                        volume, prefer_volume, formula_numbers=formula_numbers), start, end))
        windows.sort(key=lambda item: (-item[0], item[1]))

        def merged(parts):
            result = []
            for start, end in sorted(parts):
                if result and start <= result[-1][1]:
                    result[-1] = (result[-1][0], max(end, result[-1][1]))
                else:
                    result.append((start, end))
            return result

        for score, start, end in windows:
            if score <= 0 and windows[0][0] > 0:
                break
            # Subtract already selected text before charging the context budget.
            uncovered = [(start, end)]
            for left, right in intervals:
                uncovered = [(a, b) for lo, hi in uncovered
                             for a, b in ((lo, min(hi, left)), (max(lo, right), hi))
                             if a < b]
            for left, right in uncovered:
                while left < right:
                    candidate = merged(intervals + [(left, right)])
                    used = sum(b - a for a, b in candidate) + len(separator) * (len(candidate) - 1)
                    if used <= limit:
                        intervals = candidate
                        break
                    # Trimming a bridge can separate intervals again, adding
                    # another separator. Recompute the actual merged cost.
                    # Best effort: the hard context budget may cut a line here.
                    right -= used - limit
        return separator.join(text[start:end] for start, end in intervals)


class QuoteValidationError(ValueError):
    def __init__(self, page, quote):
        super().__init__(f"Цитата не найдена в переданном тексте стр. {page}")
        self.quote = quote


class EvidenceValidationError(ValueError):
    """A fixable evidence mistake: which kind failed and what to select."""

    def __init__(self, message, *, kind="", expected="", hint=""):
        super().__init__(message)
        self.kind = kind
        self.expected = expected
        self.hint = hint


def _quote_text(text):
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans({
        "\u00ad": "", "‐": "-", "‑": "-", "–": "-", "—": "-", "−": "-",
        "«": '"', "»": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
    }))
    return re.sub(r"\s+", " ", text).strip().casefold().replace("ё", "е")


def is_material_specification(text):
    """Classify a material schedule by its headers, not by a matching quantity."""
    plain = _quote_text(re.sub(r"<[^>]+>", " ", text))
    return ("спецификац" in plain or "поставщик" in plain) and (
        "техническая характеристика" in plain or "наименование и" in plain) and not (
        "порядок производства" in plain or "выполнить гибкими" in plain
        or "сечение" in plain or "проектная документация" in plain)


_OPERATION_STOP_TERMS = {"плит", "бетонн", "работ", "устрой", "монтаж", "гибк", "гбп"}


def _shares_work_object(first, second):
    """True when two citations name the same work object beyond generic words."""
    shared = _compact_terms(first) & _compact_terms(second)
    distinctive = {term for term in shared
                   if not re.search(r"[0-9]", term) and term not in _OPERATION_STOP_TERMS}
    return len(shared) >= 2 or bool(distinctive)


def _area_thickness_volume(calculation, evidence, expected_volume, expected_unit, formula,
                           expected_work=""):
    """Compute S × t only from dimensioned, verbatim PDF evidence, never VOR operands."""
    if (_quote_text(str(expected_unit)).replace(" ", "") != "м3"
            or not re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*\*\s*\d+(?:[.,]\d+)?\s*", str(formula))):
        raise ValueError("Расчет площадь × толщина требует формулу произведения и единицу м3 в ВОР")
    if not isinstance(calculation, dict) or set(calculation) != {"area", "thickness"}:
        raise ValueError("Для расчета нужны площадь и толщина с доказательствами")
    values = {}
    factors = {"м2": Decimal(1), "м": Decimal(1), "см": Decimal("0.01"), "мм": Decimal("0.001")}
    for name, allowed in (("area", {"м2"}), ("thickness", {"м", "см", "мм"})):
        operand = calculation[name]
        if not isinstance(operand, dict):
            raise ValueError("Некорректный операнд расчета")
        value = str(operand.get("value", "")).strip()
        unit = _quote_text(str(operand.get("unit", ""))).replace(" ", "")
        if len(value) > 32 or not re.fullmatch(r"\d+(?:[.,]\d+)?", value) or unit not in allowed:
            raise ValueError("Некорректное значение или размерность операнда")
        number = Decimal(value.replace(",", "."))
        if number <= 0:
            raise ValueError("Площадь и толщина должны быть положительными")
        quote = operand["quote"].strip()
        if not any(e["kind"] == "volume" and e["page"] == operand.get("page")
                   and e["quote"] == quote for e in evidence):
            raise ValueError("Операнд расчета не подкреплен проверенной цитатой объема")
        # Bind number and unit in the same source phrase, not elsewhere in the quote.
        measurement_text = _quote_text(re.sub(r"<[^>]+>", " ", quote))
        measurement_text = re.sub(r"м\s+([23])\b", r"м\1", _squeeze_thousands(measurement_text))
        measurements = re.findall(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(м2|мм|см|м)(?!\w)", measurement_text)
        if not any(Decimal(n.replace(",", ".")) == number and u == unit for n, u in measurements):
            raise ValueError("Число с единицей операнда отсутствует в цитате")
        if name == "thickness" and str(expected_work).strip():
            plain = _plain_page(quote)
            hits = [m for m in _MEASURE_RE.finditer(plain)
                    if Decimal(m.group(1).replace(",", ".")) == number
                    and m.group(2).lower() == unit]
            # The measured object stands left of the number
            # ("плитами ... толщиной 0,15"); text after it belongs to
            # neighbouring callouts and must not qualify the object.
            windows = [plain[max(0, m.start() - 120):m.start()] for m in hits] or [plain]
            if not any(_shares_work_object(window, str(expected_work)) for window in windows):
                raise ValueError("Толщина относится к другому объекту, а не к найденной работе")
        values[name] = number * factors[unit]
    actual = values["area"] * values["thickness"]
    expected = str(expected_volume).strip().replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", expected):
        raise ValueError("Некорректный заявленный объем для сравнения с расчетом")
    expected = Decimal(expected)
    # Excel cached doubles can contain a binary tail; this is not a rounding allowance.
    matches = abs(actual - expected) <= max(abs(actual), abs(expected)) * Decimal("1e-12")
    text = format(actual, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text), matches


_RATE_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(м3\s*/\s*м2|м³\s*/\s*м²|кг\s*/\s*м2|т\s*/\s*м2|"
    r"л\s*/\s*м2|шт\s*/\s*м2|м2\s*/\s*м2)",
    re.IGNORECASE)
_CONSUMPTION_WORD_RE = re.compile(r"расход|норма", re.IGNORECASE)


def detect_consumption(comment, formula=""):
    """Detect a material consumption rate (V = S × q) by dimensional analysis.

    Primary signal is the compound unit itself (м3/м2, кг/м2, …): a rate
    unit means a consumption norm, never a geometric thickness, even when
    the numbers coincide. Keyword wording (расход/норма) is only a
    fallback for unit spellings outside the known set. Returns
    (rate, unit) as (Decimal, str) or (None, "") when no rate is found.
    """
    text = unicodedata.normalize("NFKC", f"{comment}\n{formula}")
    match = _RATE_UNIT_RE.search(text)
    if match:
        return (Decimal(match.group(1).replace(",", ".")),
                match.group(2).lower().replace(" ", ""))
    fallback = re.search(
        r"(?:расход|норма)(?:\s+\S+){0,3}?\s+(\d+(?:[.,]\d+)?)\s*(\S+/\S+)",
        text, re.IGNORECASE)
    if fallback and _CONSUMPTION_WORD_RE.search(text):
        return (Decimal(fallback.group(1).replace(",", ".")),
                fallback.group(2).lower().replace(" ", ""))
    return None, ""


def parse_audit_response(raw, context, comment_required=True, *, expected_volume=None,
                         expected_unit="", formula="", quote_sources=None,
                         position_type="", expected_work="", expected_scope=""):
    """Accept only evidence from the exact page fragments supplied to the model."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Модель вернула пустой ответ")
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("В ответе модели нет JSON")
    data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(data, dict):
        raise ValueError("В ответе модели отсутствуют обязательные поля")
    try:
        wire = LlmAuditResponse.model_validate(data)
    except ValueError as exc:
        raise ValueError(f"В ответе модели некорректные поля: {exc}") from exc
    data = wire.model_dump()
    data["technology"] = {**data.get("technology", {}), "cross_pages": []}
    if position_type.casefold() != "работа":
        data["technology"] = {"status": "not_applicable", "expected": "", "actual": "", "cross_pages": []}
        data["evidence"] = [e for e in data["evidence"]
                            if e.get("kind") != "technology"]
    for field in ("found_volume", "found_unit", "discrepancy_details"):
        data[field] = data[field] if data[field] is not None else ""
    data["found_volume"] = data["found_volume"] or "-"
    data["found_unit"] = data["found_unit"] or "-"
    if quote_sources is not None:
        def resolve_source(item):
            source_id = item.get("source_id") if isinstance(item, dict) else None
            if not isinstance(source_id, str) or source_id not in quote_sources:
                raise ValueError(f"Неизвестный идентификатор цитаты: {source_id}")
            return quote_sources[source_id]
        data["evidence"] = [
            {**resolve_source(item), "kind": item.get("kind")} for item in data["evidence"]]
        calculation = data.get("volume_calculation")
        if isinstance(calculation, dict):
            for operand in calculation.values():
                source = resolve_source(operand)
                operand.update(source)
    evidence = []
    calculation = data.get("volume_calculation")
    source_evidence = list(data["evidence"])
    if calculation is not None:
        if not isinstance(calculation, dict) or set(calculation) != {"area", "thickness"}:
            raise ValueError("Некорректный расчет объема")
        for operand in calculation.values():
            if not isinstance(operand, dict):
                raise ValueError("Некорректный операнд расчета")
            operand["page"] = int(operand["page"]) if isinstance(operand.get("page"), str) and operand["page"].isdigit() else operand.get("page")
            source_evidence.append({"page": operand.get("page"), "quote": operand.get("quote"), "kind": "volume"})
    for item in source_evidence:
        if not isinstance(item, dict):
            raise ValueError("Некорректное доказательство")
        page, quote, kind = item.get("page"), item.get("quote"), item.get("kind")
        if isinstance(page, str) and re.fullmatch(r"\d+", page.strip()):
            page = int(page.strip())
        if isinstance(page, bool) or not isinstance(page, int) or page not in context:
            raise ValueError(f"Цитата ссылается на страницу {page}, которой не было в запросе")
        if str(kind).strip().casefold() == "technology":
            kind = "technology"
        if kind not in {"work", "volume", "unit", "comment", "technology"}:
            raise ValueError("Неизвестный вид доказательства")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 1200:
            raise ValueError("Некорректная цитата")
        normalized_quote = _quote_text(quote)
        if not normalized_quote or not any(normalized_quote in _quote_text(piece)
                                           for piece in context[page].split("\n[...]\n")):
            raise QuoteValidationError(page, quote)
        entry = {"page": page, "quote": quote.strip(), "kind": kind}
        if entry not in evidence:
            evidence.append(entry)
    data["evidence"] = evidence
    consumption_rate, consumption_unit = detect_consumption(expected_work, formula)
    data["consumption"] = ({"rate": format(consumption_rate, "f"), "unit": consumption_unit}
                           if consumption_rate is not None else None)
    if calculation is not None:
        if not data["work_found"]:
            raise ValueError("Расчет объема невозможен без найденной работы")
        if consumption_rate is not None:
            raise EvidenceValidationError(
                "Объем по норме расхода нельзя выводить через S×t: толщина плиты и расход щебня "
                "имеют разный физический смысл при совпадающих числах",
                kind="volume", expected="",
                hint="убери volume_calculation из ответа: для нормы расхода (м3/м2) расчёт S×t запрещён, "
                     "объём подтверждается только прямой цитатой со значением")
        if expected_scope:
            for operand in calculation.values():
                scopes = measurement_scopes(operand["quote"], operand["value"], operand["unit"])
                if not scopes:
                    source = context[operand["page"]]
                    scopes = measurement_scopes(source, operand["value"], operand["unit"])
                if not scopes:
                    scopes = set().union(*section_scopes(context[operand["page"]]).values())
                keys = {_scope_key(scope) for scope in scopes}
                if keys and (len(keys) != 1 or _scope_key(expected_scope) not in keys):
                    raise ValueError("Операнд расчёта относится к другому или неоднозначному участку ПД")
        actual, matches = _area_thickness_volume(
            calculation, evidence, expected_volume, expected_unit, formula,
            expected_work=expected_work)
        data.update(found_volume=actual, found_unit="м3",
                    volume_confirmed=matches, unit_confirmed=True)
        area, thickness = calculation["area"], calculation["thickness"]
        detail = (f"Расчет по ПД: {format_ru(area['value'])} {area['unit']} × "
                  f"{format_ru(thickness['value'])} {thickness['unit']} = {format_ru(actual)} м3.")
        if not matches:
            detail += (f" Заявлено в ВОР: {format_ru(expected_volume)} {expected_unit}; "
                       "объем не совпадает.")
        data["discrepancy_details"] = detail + " " + data["discrepancy_details"]
    page = data["found_page"]
    if not data["work_found"]:
        if page not in (None, "", "-") or evidence or any(data[f] for f in ("volume_confirmed", "unit_confirmed")):
            raise ValueError("Ненайденная работа не может иметь подтвержденную страницу или объем")
        data["found_page"] = "-"
        data["found_volume"] = "-"
        data["found_unit"] = "-"
    else:
        if isinstance(page, bool) or not re.fullmatch(r"\d+", str(page).strip()):
            raise ValueError("found_page должен содержать номер страницы найденной работы")
        page = int(str(page).strip())
        cited_pages = sorted({item["page"] for item in evidence if item["kind"] == "work"})
        if len(cited_pages) == 1 and cited_pages[0] in context:
            # The work citation is already validated verbatim against the
            # context: a single work page fixes a mismatched found_page
            # instead of discarding the whole answer.
            page = cited_pages[0]
        work_quotes = [item["quote"] for item in evidence
                       if item["page"] == page and item["kind"] == "work"]
        if not work_quotes:
            raise ValueError("Найденная работа не подкреплена цитатой с указанной страницы")
        if str(expected_work).strip():
            work_text = " ".join(work_quotes)
            page_ok = _shares_work_object(context[page], str(expected_work))
            if not _shares_work_object(work_text, str(expected_work)) and not page_ok:
                raise ValueError("Цитата work относится к другому объекту, а не к проверяемой работе")
        if position_type.casefold() == "работа" and is_material_specification(context[page]):
            raise ValueError("Описанная работа есть в другом фрагменте этого пакета: выбери kind=work "
                             "со страницы описания (например, 84), а спецификацию оставь для kind=volume/unit.")
        data["found_page"] = str(page)
        for flag, kind, field in (("volume_confirmed", "volume", "found_volume"),
                                  ("unit_confirmed", "unit", "found_unit")):
            if calculation is not None:
                continue  # Derived volume/unit are proved by both dimensioned operands above.
            quotes = [e["quote"] for e in evidence if e["kind"] == kind]
            if kind == "unit" and not quotes and data[field] != "-":
                # A volume quote can also prove its unit; no second copy is needed.
                unit = _quote_text(data[field]).rstrip(".")
                for e in evidence:
                    if e["kind"] != "volume":
                        continue
                    words = re.findall(r"\w+", _quote_text(e["quote"]))
                    if unit in words or (len(unit) >= 3 and _terms(unit) & _terms(e["quote"])):
                        quotes.append(e["quote"])
            if (data[flag] or data[field] != "-") and (data[field] == "-" or not quotes):
                raise EvidenceValidationError(
                    f"Поле {field} не подкреплено доказательством",
                    kind=kind, expected=str(data[field]),
                    hint=(f"добавь evidence с kind={kind}, выбрав source_id фрагмента, "
                          f"где видно значение {data[field]}"))
            if kind == "volume" and data[field] != "-":
                quoted_numbers = _numbers(" ".join(quotes))
                if quoted_numbers and not _numbers(data[field]) <= quoted_numbers:
                    raise EvidenceValidationError(
                        "Фактический объем отсутствует в цитате",
                        kind=kind, expected=str(data[field]),
                        hint=("выбери source_id того фрагмента, где видно число "
                              f"{data[field]} вместе с единицей; фрагмент шапки таблицы без числа не подходит"))
                tables = [q for q in quotes if "<tr" in q.lower()]
                schedules = [q for q in tables
                             if "поставщик" in q.lower() and "наименован" in q.lower()]
                if str(expected_work).strip() and schedules:
                    claim = _numbers(data[field])
                    work_terms = (_compact_terms(str(expected_work))
                                  - _OPERATION_STOP_TERMS
                                  - {t for t in _compact_terms(str(expected_work)) if any(c.isdigit() for c in t)})
                    aligned = False
                    for quote in schedules:
                        rows = re.findall(r"<tr.*?>(.*?)</tr>", quote, re.DOTALL | re.IGNORECASE)
                        for row in rows or [quote]:
                            if claim and claim <= _numbers(row) and (work_terms & _compact_terms(row)):
                                aligned = True
                                break
                        if aligned:
                            break
                    if not aligned:
                        raise EvidenceValidationError(
                            "Число объёма и работа находятся в разных строках таблицы",
                            kind=kind, expected=str(data[field]),
                            hint=("выбери source_id фрагмента, где число "
                                  f"{data[field]} стоит в одной строке таблицы с проверяемой работой"))
        if comment_required and data["comment_confirmed"] and not any(e["kind"] == "comment" for e in evidence):
            data["comment_confirmed"] = False
        technology = data["technology"]
        state = technology["status"]
        if position_type.casefold() == "работа" and state in {"not_checked", "not_applicable"}:
            raise ValueError("Для найденной операции требуется независимая оценка technology")
        expected, actual = technology["expected"].strip(), technology["actual"].strip()
        if state in {"confirmed", "unconfirmed", "different"}:
            if not expected or _quote_text(expected) not in _quote_text(expected_work):
                raise EvidenceValidationError(
                    "Проверяемая технология должна быть дословным условием ВОР",
                    kind="technology", expected=str(expected_work),
                    hint="скопируй expected дословно из наименования или примечания ВОР из запроса")
        if state in {"confirmed", "unconfirmed", "different"}:
            quotes = [_quote_text(re.sub(r"<[^>]+>", " ", e["quote"])) for e in evidence
                      if e["kind"] == "technology"]
            actual_terms = _compact_terms(actual)
            if not actual or not any(actual_terms <= _compact_terms(quote) for quote in quotes):
                raise EvidenceValidationError(
                    "Технология ПД не подкреплена цитатой kind=technology",
                    kind="technology", expected=str(actual),
                    hint=("выбери source_id фрагмента, где дословно видно «"
                          f"{actual[:120]}»; actual обязан присутствовать именно в выбранном фрагменте"))
        if state == "confirmed":
            words = re.findall(r"[а-яёa-z]+", expected.lower())
            quote_terms = set()
            for quote in quotes:
                quote_terms |= _compact_terms(quote)
            missing = sorted(
                term for term in _compact_terms(expected)
                if len(term) >= 5 and term not in quote_terms
                and any(term in word for word in words))
            if len(missing) >= 2:
                technology["status"] = "unconfirmed"
                technology["actual"] = actual
                data["discrepancy_details"] = (
                    data["discrepancy_details"].strip()
                    + f" В цитате ПД отсутствуют условия ВОР ({', '.join(missing)}); требуется сверка."
                ).strip()
    if not comment_required:
        data["comment_confirmed"] = True
    data["found_pages"] = sorted({e["page"] for e in evidence})
    data["status"] = "ОК" if all(data[f] for f in (
        "work_found", "volume_confirmed", "unit_confirmed")) else "Ошибка"
    if data["status"] == "Ошибка" and not data["discrepancy_details"]:
        missing = [label for flag, label in (
            ("work_found", "работа"), ("volume_confirmed", "объём"),
            ("unit_confirmed", "единица измерения"))
            if not data[flag]]
        data["discrepancy_details"] = "Не подтверждены: " + ", ".join(missing) + "."
    if comment_required and not data["comment_confirmed"]:
        data["discrepancy_details"] = (
            data["discrepancy_details"].strip()
            + " Примечание ВОР не подтверждено цитатой ПД."
        ).strip()
    if data["work_found"] and position_type.casefold() == "работа":
        technology = data["technology"]
        if technology["status"] == "different":
            quantities_ok = bool(data["volume_confirmed"] and data["unit_confirmed"])
            data["status"] = INCOMPLETE if quantities_ok else "Ошибка"
            data["discrepancy_details"] += (
                f" Различается описание технологии: ВОР — «{technology['expected']}», "
                f"ПД — «{technology['actual']}»; требуется инженерная сверка.")
        elif technology["status"] == "unconfirmed":
            data["status"] = INCOMPLETE
            data["discrepancy_details"] += " Способ выполнения не подтверждён; наличие операции сохранено."
    data["quantity_basis"] = ("consumption" if consumption_rate is not None
                              else "derived" if calculation is not None else "direct"
                              if data["found_volume"] != "-" else "unknown")
    data["search_complete"] = data["work_found"]
    return AuditResult.from_legacy(data).to_legacy()


def audit_text_pages(index, target_indices, ask, work_name, volume, reference="", comment="",
                     max_contexts=6, max_context_chars=10000, max_total_chars=60000, max_pages=4, unit="",
                     formula="", search_available=None):
    """Check references first; the first located work stops the search."""
    if min(max_contexts, max_context_chars, max_total_chars, max_pages) <= 0:
        raise ValueError("Лимиты поиска должны быть положительными")
    primary = list(dict.fromkeys(i for i in target_indices if 0 <= i < len(index.texts)))
    scope, related = index.related_context(primary, work_name, volume, formula, comment)
    with span("rank-candidate-pages", as_type="retriever", input={
            "work": work_name, "volume": volume, "comment": comment, "reference": reference}) as observation:
        candidates = index.candidates(work_name, comment, reference, volume, primary)
        degraded = [i for i in primary if i in index.errors or i in index.pending_ocr]
        observation.update(output={"primary_pages": [i + 1 for i in primary],
                                   "ranked_pages": [i + 1 for i in candidates],
                                   "scope": scope, "related_pages": [i + 1 for i in related],
                                   "pending_ocr": [i + 1 for i in index.pending_ocr],
                                   "reading_errors": {i + 1: e for i, e in index.errors.items()}},
                           status_message=(f"Ссылка: {[i + 1 for i in primary]}; кандидатов: {len(candidates)}; "
                                           f"pending OCR: {len(index.pending_ocr)}; сбоев: {len(index.errors)}"),
                           level="WARNING" if degraded else "DEFAULT")
    visited, contexts, characters = set(), 0, 0
    failures = {i: error for i, error in index.errors.items() if i in primary}
    last_failure, confirmation_used = None, False

    def finish(result):
        result = dict(result)
        result["contexts"] = contexts
        if result.get("work_found"):
            found_index = int(result["found_page"]) - 1
            if found_index not in primary:
                adjacent = any(abs(found_index - page) == 1 for page in primary)
                if adjacent:
                    refs = ", ".join(str(page + 1) for page in primary)
                    note = f"Работа найдена на стр. {result['found_page']}, рядом со ссылкой ВОР (стр. {refs}). "
                else:
                    if result["status"] not in {TECHNICAL_ERROR, INCOMPLETE}:
                        result["status"] = "Ошибка"
                    note = f"Работа найдена на стр. {result['found_page']}, вне ссылки ВОР. "
                result["discrepancy_details"] = note + result["discrepancy_details"]
            if any(i in failures or i in index.pending_ocr for i in primary):
                result["status"] = INCOMPLETE
                result["search_complete"] = False
                result["discrepancy_details"] += " Не все страницы ссылки ВОР прочитаны; найденные доказательства сохранены."
            technology = result.get("technology") or {}
            if technology.get("status") in {"different", "unconfirmed"}:
                expected = str(technology.get("expected") or "")
                quoted_terms = set()
                for item in result.get("evidence", []):
                    if item.get("kind") == "technology":
                        quoted_terms |= _compact_terms(str(item.get("quote") or ""))
                words = re.findall(r"[а-яёa-z]+", expected.lower())
                need = {term for term in _compact_terms(expected)
                        if len(term) >= 5 and term not in quoted_terms
                        and any(term in word for word in words)}
                if need:
                    quoted_pages = {int(e["page"]) for e in result.get("evidence", [])
                                    if isinstance(e.get("page"), int)}
                    hits = []
                    threshold = max(2, (len(need) + 1) // 2)
                    for number, text in enumerate(index.texts, start=1):
                        if number in quoted_pages:
                            continue
                        if len(need & _compact_terms(text)) >= threshold:
                            hits.append(number)
                            if len(hits) >= 3:
                                break
                    if hits:
                        pages = ", ".join(map(str, hits))
                        if technology["status"] == "different":
                            technology = dict(technology, status="unconfirmed")
                            result["technology"] = technology
                            result["status"] = INCOMPLETE
                        result["technology"] = {**result.get("technology", {}),
                                                "cross_pages": hits}
                        result["discrepancy_details"] += (
                            f" Формулировка ВОР также встречается на стр. {pages}; "
                            f"сверка формулировок — инженеру.")
        return result

    def confirm_quantities(located):
        nonlocal contexts, characters, confirmation_used
        # Actual differing quantities/units are findings, not failed searches.
        if confirmation_used or (located["found_volume"] != "-" and located["found_unit"] != "-"):
            return located
        formula_numbers = _numbers(formula)
        work_pages = list(dict.fromkeys(
            [int(located["found_page"]) - 1]
            + [e["page"] - 1 for e in located["evidence"] if e["kind"] == "work"]))[:min(2, max_pages)]
        with span("rank-quantity-pages", as_type="retriever",
                  input={"work": work_name, "volume": volume, "unit": unit, "formula": formula}) as observation:
            quantity_pages = index.support_candidates(work_name, volume, unit,
                                                     formula_numbers=formula_numbers)
            observation.update(output={"ranked_pages": [i + 1 for i in quantity_pages]},
                               status_message=f"Кандидатов: {len(quantity_pages)}")
        support = [i for i in dict.fromkeys(related + quantity_pages) if i not in work_pages]
        pages = work_pages + support[:max_pages - len(work_pages)]
        if len(pages) == len(work_pages) and not any(i in quantity_pages for i in work_pages):
            return located
        if contexts >= max_contexts or characters >= max_total_chars:
            result = dict(located)
            result.update(status=INCOMPLETE, search_complete=False)
            result["discrepancy_details"] += " Работа найдена, но бюджет подтверждения объема и единицы исчерпан."
            return result
        per_page = min(max_context_chars, max_total_chars - characters) // len(pages)
        if per_page < 1:
            result = dict(located)
            result.update(status=INCOMPLETE, search_complete=False)
            return result
        context = {}
        for i in pages:
            quotes = list(dict.fromkeys(e["quote"] for e in located["evidence"]
                                        if e["kind"] == "work" and e["page"] == i + 1))
            # Preserve a work-bearing excerpt even when the original work
            # quote is long; at least half the page budget stays fresh.
            separator = "\n[...]\n"
            prefix_limit = max(0, (per_page - len(separator)) // 2)
            prefix = ""
            if quotes and prefix_limit:
                quote_index = PageTextIndex((min(quotes, key=len),))
                prefix = quote_index.fragment(0, work_name, "", reference, volume, prefix_limit)
            separator = separator if prefix else ""
            capacity = per_page - len(prefix) - len(separator)
            fragment = index.fragment(i, work_name, "", reference, volume, capacity, prefer_volume=True,
                                      formula_numbers=formula_numbers)
            context[i + 1] = prefix + (separator + fragment if fragment else "")
        characters += sum(len(text) for text in context.values())
        visited.update(pages)
        contexts += 1
        confirmation_used = True
        with span("confirm-quantity-context", as_type="chain", input=context) as observation:
            verified = ask(context)  # exactly one bounded confirmation, never a page/chunk loop
            found = "да" if verified.get("work_found") else "нет"
            observation.update(output=verified,
                               status_message=f"{verified['status']}; работа: {found}",
                               level=result_level(verified["status"]))
        if verified["status"] == TECHNICAL_ERROR or not verified.get("work_found"):
            result = dict(located)
            result.update(status=verified["status"] if verified["status"] == TECHNICAL_ERROR else INCOMPLETE,
                          search_complete=False)
            result["discrepancy_details"] += " Подтверждение связанных данных не завершено: " + verified["discrepancy_details"]
            return result
        verified_work = [e for e in verified["evidence"] if e["kind"] == "work"]
        rebound = [e for e in verified_work if e["page"] != int(located["found_page"])]
        allowed = any(e["page"] == int(located["found_page"]) for e in verified_work)
        if not allowed and len(rebound) == 1 and scope:
            located_quote = next(e["quote"] for e in located["evidence"] if e["kind"] == "work")
            package = set(primary) | set(related)
            old_page = int(located["found_page"]) - 1
            new_page = rebound[0]["page"] - 1
            # Both work citations must sit inside the same linked package
            # the search itself assembled (primary + related pages). Scope
            # names are not re-derived here: the anchor page legitimately
            # names several scopes.
            same_package = old_page in package and new_page in package
            same_operation = bool(_compact_terms(located_quote) & _compact_terms(rebound[0]["quote"])
                                  - {"плит", "бетонн", "работ", "устрой", "монтаж", "гибк", "гбп"})
            allowed = same_package and same_operation
        if not allowed:
            result = dict(located)
            result.update(status=INCOMPLETE, search_complete=False)
            result["discrepancy_details"] += " Добор не подтвердил связь с первоначально найденной работой."
            return result
        if rebound and allowed and not any(e["page"] == int(located["found_page"]) for e in verified_work):
            located = dict(located, found_page=str(rebound[0]["page"]))
        result = dict(verified)
        result["found_page"] = located["found_page"]
        evidence = [e for e in located["evidence"] if e["kind"] in {"work", "technology"}]
        evidence += [e for e in verified["evidence"] if e not in evidence]
        result["evidence"] = evidence
        result["found_pages"] = sorted({e["page"] for e in evidence})
        if located.get("technology", {}).get("status") == "different":
            result["technology"] = located["technology"]
            quantities_ok = bool(result.get("volume_confirmed") and result.get("unit_confirmed"))
            result["status"] = INCOMPLETE if quantities_ok else "Ошибка"
            result["discrepancy_details"] += " " + located["discrepancy_details"]
        return result

    groups = []
    if primary:
        for start in range(0, len(primary), max_pages):
            groups.append(primary[start:start + max_pages])
    while groups or candidates:
        if contexts and search_available is not None and not search_available():
            break  # Keep an HTTP attempt for quantity confirmation; never grow the budget.
        from_reference = bool(groups)
        seeds = groups.pop(0) if groups else candidates[:min(2, max_pages)]
        candidates = [i for i in candidates if i not in seeds]
        pages = list(seeds)
        # Neighbours support referenced pages, but are not a semantic relation
        # between arbitrary search hits in an assembled PDF.
        if from_reference and related:
            pages = list(dict.fromkeys(pages + related))[:max_pages]
        for seed in seeds if from_reference and not related else ():
            for neighbor in (seed - 1, seed + 1):
                if 0 <= neighbor < len(index.texts) and neighbor not in pages and len(pages) < max_pages:
                    pages.append(neighbor)
        readable = [i for i in pages if index.texts[i].strip()]
        for i in pages:
            if i in index.errors:
                failures[i] = index.errors[i]
        if not readable or not any(i not in visited for i in readable):
            continue
        if contexts >= max_contexts or characters >= max_total_chars:
            break
        limit = min(max_context_chars, max_total_chars - characters)
        per_page = limit // len(readable)
        if per_page < 1:
            break
        with span("select-context", as_type="retriever",
                  input={"pages": [i + 1 for i in readable], "per_page_chars": per_page}) as observation:
            operation_first = sorted(readable, key=lambda i: (is_material_specification(index.texts[i]), i))
            context = {}
            for i in operation_first:
                context[i + 1] = index.fragment(i, work_name, comment, reference, volume, per_page,
                                                formula_numbers=_numbers(formula))
            context_chars = sum(map(len, context.values()))
            observation.update(output=context, metadata={"context_chars": context_chars},
                               status_message=f"Фрагментов: {len(context)}; символов: {context_chars}")
        visited.update(readable)
        characters += sum(len(text) for text in context.values())
        contexts += 1
        with span("locate-work", as_type="chain", input=context) as observation:
            result = ask(context)
            found = "да" if result.get("work_found") else "нет"
            observation.update(output=result,
                               status_message=f"{result['status']}; работа: {found}",
                               level=result_level(result["status"]))
        if result["status"] in {TECHNICAL_ERROR, INCOMPLETE} and not result.get("work_found"):
            last_failure = result
            if result.get("recoverable"):
                continue
            return finish(result)
        if result["work_found"]:
            # First confirmed work stops the search: one bounded confirmation
            # at most; a mismatched quantity/unit is a finding, never a
            # reason to look for another work.
            with span("confirm-quantities", as_type="chain", input=result) as observation:
                confirmed = confirm_quantities(result)
                observation.update(output=confirmed, status_message=(
                    f"{confirmed['status']}; объём: {confirmed['found_volume']}; ед.: {confirmed['found_unit']}"),
                    level=result_level(confirmed["status"]))
            if confirmed["status"] == TECHNICAL_ERROR:
                return finish(confirmed)
            if confirmed["found_volume"] == "-" or confirmed["found_unit"] == "-":
                confirmed = dict(confirmed)
                confirmed.update(status=INCOMPLETE, search_complete=False)
                confirmed["discrepancy_details"] += " Найденная работа сохранена; количество или единица не установлены."
            return finish(confirmed)
    if last_failure is not None:
        return finish(last_failure)
    reason = "Работа не найдена в отобранных фрагментах; это не доказывает ее отсутствие в PDF."
    if contexts >= max_contexts or characters >= max_total_chars:
        reason += " Исчерпан бюджет поиска на позицию."
    reason += f" Проверено контекстов: {contexts}; страниц с фрагментами: {len(visited)} из {len(index.texts)}."
    if index.pending_ocr:
        reason += f" Не распознано сканов: {len(index.pending_ocr)}."
    if failures:
        reason += " Сбои чтения: " + "; ".join(f"стр. {i + 1}: {error}" for i, error in failures.items())
    return finish(empty_result(TECHNICAL_ERROR if failures else INCOMPLETE, reason))
