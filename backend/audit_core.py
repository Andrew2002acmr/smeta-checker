"""Page selection and audit orchestration, independent of UI and PDF objects."""

import json
import math
import re
from collections import Counter, OrderedDict
from decimal import Decimal
import unicodedata
import sys
from threading import Lock

from audit_measurements import (
    _close_numbers,
    _numbers,
    canonical_unit,
    paired_units,
    iter_paired_measurements,
    paired_measurements,
    has_paired_units,
    iter_measurements,
    parse_measurement,
    compare_measurements,
)
from audit_models import (
    AuditResult, ClaimCheck, ClaimKind, ClaimSpec, ClaimState,
    EvidenceFact, LlmAuditResponse, LlmClaimProposal, claim_status,
)
from audit_predicates import technical_specs, predicate_check, geometry_product
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

    def __init__(self, max_requests=6, max_total_tokens=120000, max_output_tokens=12000):
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


_page_terms_cache = OrderedDict()
_page_terms_size = 0
_page_terms_lock = Lock()
_PAGE_TERMS_LIMIT = 32 * 1024 * 1024


def _cached_page_terms(text):
    """Reuse immutable page tokens across positions, with a per-process RAM bound."""
    global _page_terms_size
    with _page_terms_lock:
        cached = _page_terms_cache.get(text)
        if cached is not None:
            _page_terms_cache.move_to_end(text)
            return cached[0]
    terms = frozenset(_terms(text))
    size = sys.getsizeof(text) + sys.getsizeof(terms) + sum(map(sys.getsizeof, terms))
    if size <= _PAGE_TERMS_LIMIT:
        with _page_terms_lock:
            previous = _page_terms_cache.pop(text, None)
            if previous is not None:
                _page_terms_size -= previous[1]
            _page_terms_cache[text] = (terms, size)
            _page_terms_size += size
            while _page_terms_size > _PAGE_TERMS_LIMIT:
                _, (_, removed_size) = _page_terms_cache.popitem(last=False)
                _page_terms_size -= removed_size
    return terms


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
_UNIT_CELL_RE = re.compile(
    r"\s*(м3|м³|м2|м²|мм|см|м|шт\.?|кг|т|л|компл|пог\.?\s*м)\s*", re.IGNORECASE)


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


def _unit_cell_index(row):
    """Quantity-unit column: a lone unit token cell is the header role carrier.

    A bare number elsewhere (mark codes, row numbers) never qualifies; the
    quantity amount must sit in the cell right after this unit cell.
    """
    for pos, cell in enumerate(row):
        if _UNIT_CELL_RE.fullmatch(cell or ""):
            return pos
    return None


def _schedule_item_rows(raw, known, mention_res):
    """Yield (header scope, row text, row cells, unit position) for item rows.

    Structural rule, no document keywords: a group header is a row with a
    filled description cell and empty quantity cells; rows carrying a
    quantity inherit the current group. A header names its scope when it
    mentions exactly one known scope. Headers are never yielded. The unit
    position is the index of the lone unit-token cell (the quantity
    column header role); item rows without one are skipped so amount
    binding cannot fall back to a max number in another column.
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
        unit_at = _unit_cell_index(row)
        if unit_at is None:
            continue
        after = [cell for cell in row[unit_at + 1:] if cell]
        if not after or not any(_numbers(cell) for cell in after):
            continue
        yield current, joined, tuple(row), unit_at


def _scoped_resource_scopes(texts, skip, task_terms, numbers, known, mention_res):
    """Count qualifying schedule rows per scope for operation anchoring.

    A row qualifies on the exact expected spec count plus shared terms."""
    found = Counter()
    if not numbers or not task_terms:
        return found
    for idx, raw in enumerate(texts):
        if idx in skip:
            continue
        text = _plain_page(raw)
        if "поставщик" not in text.lower() or "наименован" not in text.lower():
            continue
        for header, joined, row, unit_at in _schedule_item_rows(raw, known, mention_res):
            shared = task_terms & _terms(joined)
            after = [cell for cell in row[unit_at + 1:] if cell]
            # Quantity-column cells only: a mark code in another column
            # must not count, even when it equals the expected amount.
            cell_numbers = set().union(*[_numbers(cell) for cell in after]) if after else set()
            numbers_hit = _close_numbers(cell_numbers, numbers)
            if re.search(r"пбзгу", joined, re.IGNORECASE):
                numbers_hit -= {n for n in numbers_hit if n == 1}
            generic = shared <= {"плит", "бетонн", "гибк", "геотек", "укладк", "креплен",
                                 "работ", "устрой", "монтаж", "установ"}
            exact = numbers_hit and len(cell_numbers) <= 2
            if numbers_hit and len(shared) >= 2 and (not generic or exact):
                found[_scope_key(header)] += 1
    return found

def _canonical_page_map(data):
    """Stringify page-index keys so write/read hashing see one JSON order.

    ``errors``/``quality`` use int page numbers in memory but JSON object
    keys after a round trip, and ``json.dumps(sort_keys=True)`` orders
    ``2`` before ``10`` numerically yet after it lexicographically. Without
    this both sides hash different byte strings and every snapshot with a
    double-digit page entry fails as a corpus mismatch.
    """
    if not isinstance(data, dict):
        return data
    return {str(key): value for key, value in data.items()}


class PageTextIndex:
    def __init__(self, texts, errors=None, pending_ocr=(), table_rows=None, locators=None,
                 *, quality=None, metadata=None, repeated_lines=None, tables=None):
        self.texts = tuple(texts)
        self.errors = dict(errors or {})
        self.pending_ocr = tuple(sorted(set(pending_ocr)))
        # Physical grids and legacy flattened strings are different contracts.
        # Older snapshots accidentally put grids in table_rows; read them safely.
        self._legacy_tables = tables is None and table_rows is not None and any(
            isinstance(row, dict) for page in table_rows for row in page)
        if self._legacy_tables:
            tables = table_rows
        self.tables = tuple(tuple(page) for page in tables) if tables is not None else None
        if table_rows is None and self.tables is not None:
            table_rows = [tuple(" | ".join(cell.get("text", "") for cell in table["cells"]
                                         if cell["row"] == row)
                                for table in page for row in range(table["row_count"]))
                          for page in self.tables]
        self.table_rows = tuple(tuple(row) for row in table_rows) if table_rows is not None else None
        # Plain-data source locators per page (SourceLocator-compatible dicts);
        # None means unknown provenance (historical snapshots), never fabricated.
        self.locators = (tuple(tuple(dict(item) for item in page) for page in locators)
                         if locators is not None else None)
        self.quality = dict(quality or {})
        self.metadata = dict(metadata or {})
        self.repeated_lines = tuple(repeated_lines or ())
        self.terms = tuple(_cached_page_terms(text) for text in self.texts)
        frequency = Counter(term for terms in self.terms for term in terms)
        self.weights = {term: math.log(1 + len(self.texts) / count) for term, count in frequency.items()}

    def to_snapshot(self):
        """Plain-data, content-addressed extraction boundary; never PDF handles."""
        import hashlib
        payload = {
            "schema": 1, "texts": list(self.texts),
            "errors": _canonical_page_map(self.errors),
            "pending_ocr": list(self.pending_ocr),
            "table_rows": self.table_rows, "locators": self.locators,
            "quality": _canonical_page_map(self.quality), "metadata": self.metadata,
        }
        if self.tables is not None and not self._legacy_tables:
            payload["tables"] = self.tables
        # Canonical JSON also detaches nested dictionaries from the index.
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        payload = json.loads(encoded)
        payload["corpus_id"] = hashlib.sha256(encoded).hexdigest()
        return payload

    @classmethod
    def from_snapshot(cls, snapshot):
        """Reject stale/corrupt corpora instead of silently extracting another."""
        import hashlib
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 1:
            raise ValueError("Unsupported extraction snapshot schema")
        payload = {key: value for key, value in snapshot.items() if key != "corpus_id"}
        for field in ("errors", "quality"):
            if isinstance(payload.get(field), dict):
                payload[field] = _canonical_page_map(payload[field])
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != snapshot.get("corpus_id"):
            raise ValueError("Extraction snapshot corpus hash mismatch")
        payload = json.loads(encoded)
        texts = payload.get("texts")
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValueError("Invalid snapshot page texts")
        count = len(texts)
        for field in ("table_rows", "locators", "tables"):
            if payload.get(field) is not None and len(payload[field]) != count:
                raise ValueError(f"Invalid snapshot {field} page count")
        def integer_keys(field):
            data = payload.get(field) or {}
            if not isinstance(data, dict):
                raise ValueError(f"Invalid snapshot {field}")
            restored = {int(key): value for key, value in data.items()}
            if any(index < 0 or index >= count for index in restored):
                raise ValueError(f"Invalid snapshot {field} page")
            return restored
        pending = payload.get("pending_ocr") or []
        if any(type(index) is not int or not 0 <= index < count for index in pending):
            raise ValueError("Invalid snapshot pending OCR page")
        return cls(texts, integer_keys("errors"), pending, payload.get("table_rows"),
                   payload.get("locators"), quality=integer_keys("quality"),
                   metadata=payload.get("metadata"), tables=payload.get("tables"))

    def _score(self, text, work_name, comment, reference, volume, prefer_volume=False,
               *, quantity_search=False, formula_numbers=(), page_terms=None):
        terms = _terms(text) if page_terms is None else page_terms
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
        # Quantities are assertions to verify, never retrieval relevance.
        # Otherwise a convenient number outranks the actual construction and
        # differing project quantities disappear before the model sees them.
        if prefer_volume and numbers:
            score += 1000
        if numbers.intersection(formula_numbers) and _TABLE_MARKERS.search(text):
            # Formula operands are explicit input evidence, not the expected
            # result; retain their surrounding quantity heading.
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
            score = self._score(text, work_name, comment, reference, volume, page_terms=self.terms[idx])
            if score > 0:
                structured = self.tables is not None and any(
                    table.get("quality") == "usable" and _terms(work_name).intersection(
                        _terms(" ".join(cell["text"] for cell in table["cells"])))
                    for table in self.tables[idx])
                ranked.append((bool(structured), score, idx))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [idx for _, _, idx in ranked]

    def support_candidates(self, work_name, volume, unit, excluded=(), *, formula_numbers=()):
        """Find quantity-bearing pages for an already located work, not another location search."""
        query, excluded = _terms(work_name), set(excluded)
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
                                formula_numbers=formula_numbers, page_terms=self.terms[idx])
            if _TABLE_MARKERS.search(text):
                score += 5
            if self.table_rows is not None and idx < len(self.table_rows):
                for row in self.table_rows[idx]:
                    row_terms = _terms(row)
                    row_numbers = _numbers(row)
                    if query & row_terms and row_numbers:
                        score += 10
                        break
            if unit and (_terms(unit) & self.terms[idx]
                         or re.search(r"(?<!\w)" + re.escape(unit) + r"(?!\w)", text, re.IGNORECASE)):
                score += 3
            ranked.append((score, idx))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [idx for _, idx in ranked]

    def related_context(self, target_indices, work_name, volume, formula="",
                        comment="", expected_scope=None):
        """Link an anchored scope to named sections and scoped quantity rows.

        Returns (scope, related_indices): the anchored project scope display
        name ("" when nothing anchors one) plus bounded, ordered extra page
        indices extending the caller's primary pages. Scope comes from an
        independent ``expected_scope`` provenance (VOR section identity),
        an explicit work/comment scope, a dimensioned formula operand bound
        to a scope parenthetical on the anchor page, a uniquely scoped
        schedule resource row on a primary page first (header + expected
        quantity + shared work/material terms), or the same match on
        non-primary pages. An explicit empty ``expected_scope`` (``""``)
        declares unknown provenance and never derives a group from an
        expected quantity; legacy ``None`` (omitted) keeps historical
        quantity-derived behaviour. Sections link when their row carries
        the anchored scope; a labelled mention, a different object
        qualifier never links, and the section nearest the anchor page
        comes first. Schedule tables link when the header scope's group
        contains the formula operand and shares task terms (or shares
        terms while the operand sits under the same header). Bare
        numbers, work-word overlap, and page proximity alone never
        qualify. Source texts are untouched; no pages or scopes are
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
        hint_key = _scope_key(expected_scope) if expected_scope else ""
        if hint_key:
            # Independent VOR provenance pins the subject: quantity matches
            # may only rank inside it, never reassign it. Unknown hint
            # fails closed instead of borrowing a convenient group.
            if hint_key not in known:
                return "", []
            anchored = {hint_key}
            target_numbers = set(measured_operands) | _numbers(volume)
            scope_key = hint_key
        else:
            anchored = set(explicit) | set(evidence)
            target_numbers = set(measured_operands)
            scope_key = ""
            if expected_scope is None:
                # Legacy path (provenance omitted): a uniquely scoped
                # schedule resource row may anchor the scope by quantity.
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
                items = [(header, joined) for header, joined, _, _ in _schedule_item_rows(raw, known, mention_res)]
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
        if self.tables is not None and self.tables[idx]:
            from table_context import select_fragment
            def score(value):
                # Locate a physical row by its subject, never by the expected
                # quantity. The retained source quote must fit evidence DTOs.
                return self._score(value, work_name, comment, reference, "")
            ranked = sorted(self.tables[idx], key=lambda table: (
                table.get("quality") == "usable",
                score(" ".join(cell["text"] for cell in table["cells"]))), reverse=True)
            for table in ranked:
                selected = select_fragment(table, min(limit, 1200), score)
                if selected is not None:
                    return selected[0]
            # A cut HTML row is not evidence. Keep only prose outside tables.
            text = re.sub(r"<table\b.*?</table>", "", text, flags=re.I | re.S)
            return text[:limit]
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


def build_claim_specs(position):
    """Derive checkable VOR requirements from cells, never from PDF text."""
    name = str(getattr(position, "work_name", "") or "")
    comment = str(getattr(position, "comment", "") or "")
    volume = str(getattr(position, "volume", "") or "")
    unit = str(getattr(position, "unit", "") or "")
    position_type = str(getattr(position, "position_type", "") or "")
    source = getattr(position, "position_source", None)
    cells = (source.get("cells", {}) if isinstance(source, dict)
             else getattr(source, "cells", {}) or {})
    def source_cells(*columns):
        return sorted(address for address in cells
                      if re.sub(r"\d+$", "", address) in columns)
    specs = [
        ClaimSpec(claim_id="presence", kind=ClaimKind.PRESENCE,
                  label=name or position_type or "позиция",
                  cells=source_cells("B"), expected_text=name, required=True),
        ClaimSpec(claim_id="quantity", kind=ClaimKind.QUANTITY,
                  label=f"{volume} {unit}".strip(), cells=source_cells("D", "E"),
                  expected_text=f"{volume} {unit}".strip(),
                  expected_value=volume, expected_unit=unit, required=True),
        ClaimSpec(claim_id="unit", kind=ClaimKind.UNIT, label=unit,
                  cells=source_cells("C"), expected_text=unit,
                  expected_unit=unit, required=True),
    ]
    if name and position_type.casefold() == "работа":
        specs.append(ClaimSpec(claim_id="method", kind=ClaimKind.METHOD,
                               label=name, cells=source_cells("B", "I"),
                               expected_text=name, required=True))
    specs.extend(technical_specs(name, comment, position_type,
                                source_cells("B", "I"),
                                str(getattr(position, "formula", "") or "")))
    return specs


def _wire_claims(raw):
    """Decode the claim-proposal container; malformed JSON fails the answer."""
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
    return wire.model_dump()


def _resolve_proposal_source(proposal_id, quote_sources):
    if not isinstance(proposal_id, str) or proposal_id not in (quote_sources or {}):
        raise EvidenceValidationError(
            f"Неизвестный идентификатор цитаты: {proposal_id}",
            kind="volume", expected=str(proposal_id),
            hint="выбирай source_id только из переданного каталога фрагментов")
    return quote_sources[proposal_id]


def _validate_proposal_quote(page, quote, context):
    if isinstance(page, str) and re.fullmatch(r"\d+", page.strip()):
        page = int(page.strip())
    if isinstance(page, bool) or not isinstance(page, int) or page not in context:
        raise ValueError(f"Цитата ссылается на страницу {page}, которой не было в запросе")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 1200:
        raise ValueError("Некорректная цитата")
    normalized_quote = _quote_text(quote)
    if not normalized_quote or not any(normalized_quote in _quote_text(piece)
                                       for piece in context[page].split("\n[...]\n")):
        raise QuoteValidationError(page, quote)
    return page, quote.strip()


def _proposal_evidence(proposal, quote_sources, context, kind):
    """Resolve one proposal to verbatim evidence; unknown IDs fail locally."""
    ids = list(dict.fromkeys([
        *(proposal.get("source_ids") or []),
        *([proposal.get("value_source_id")] if proposal.get("value_source_id") else []),
        *([proposal.get("unit_source_id")] if proposal.get("unit_source_id") else []),
        *(proposal.get("subject_source_ids") or []),
        *(proposal.get("relation_source_ids") or []),
    ]))
    evidence = []
    for source_id in ids:
        source = _resolve_proposal_source(source_id, quote_sources)
        page, quote = _validate_proposal_quote(
            source.get("page"), source.get("quote"), context)
        entry = {"page": page, "quote": quote, "kind": kind,
                 "source_id": source_id, "locator": source.get("locator", source),
                 "binding_role": ("value" if source_id == proposal.get("value_source_id")
                                  else "unit" if source_id == proposal.get("unit_source_id")
                                  else "subject" if source_id in proposal.get("subject_source_ids", [])
                                  else "relation" if source_id in proposal.get("relation_source_ids", [])
                                  else "fact")}
        if entry not in evidence:
            evidence.append(entry)
    return evidence


def _row_texts(quote):
    """Split an HTML quote into row texts; plain quotes stay whole."""
    if "<tr" not in quote.lower():
        return [quote]
    rows = re.findall(r"<tr.*?>(.*?)</tr>", quote, re.DOTALL | re.IGNORECASE)
    return [" | ".join(
        _plain_page(cell).strip() for cell in re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row,
            re.DOTALL | re.IGNORECASE)) for row in rows] or [quote]


def _bind_quantity_fact(quotes, expected_work="", expected_unit=""):
    """Bind the single atomic number+unit fact addressed by the proposal.

    Binding uses only proposal geometry: one measurement in the addressed
    text, adjacent table cells, or one work-aligned schedule row. The
    expected VOR value never selects which number wins; comparison
    happens separately.
    """
    candidates = []
    for quote in quotes:
        rows = _row_texts(quote)
        if len(rows) > 1 and str(expected_work).strip():
            query = _terms(expected_work)
            scored = [(sum(3 if _has_digit(term) else 1 for term in query & _terms(row)), row)
                      for row in rows if _shares_work_object(row, expected_work)]
            if scored:
                best = max(score for score, _ in scored)
                rows = [row for score, row in scored if score == best]
        for text in rows:
            pairs = iter_paired_measurements(text)
            if pairs:
                requested = canonical_unit(expected_unit)
                # The VOR unit selects a typed component. Its numeric value
                # is never used to pick the convenient side of the slash.
                applicable = [m for m in pairs if requested and m.dimension == requested[1]]
                distinct = {(format(m.value, "f"), m.unit) for m in applicable}
                if len(distinct) != 1:
                    return None, "ambiguous_measurements"
                candidates.append((text, applicable[0]))
                continue
            if (any(paired_units(cell.strip()) for cell in text.split("|"))
                    if "|" in text else has_paired_units(text)):
                return None, "ambiguous_measurements"
            measurements = iter_measurements(text)
            cells = [chunk.strip() for chunk in text.split("|")]
            if len(cells) > 1:
                # Table row: a bare number in its own cell pairs with a
                # neighbouring unit cell. Numbers embedded in a text cell
                # (density 500 inside the name) never borrow a neighbour
                # unit; they need their own inline measurement.
                cell_numbers = []
                for pos, chunk in enumerate(cells):
                    bare = re.sub(r"\s+", "", chunk)
                    if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", bare):
                        continue
                    for token in _numbers(chunk):
                        neighbours = " ".join(cells[max(0, pos - 1):pos + 2])
                        unit_hit = canonical_unit(neighbours)
                        if unit_hit is None:
                            for cell in cells[max(0, pos - 1):pos + 2]:
                                unit_hit = canonical_unit(cell)
                                if unit_hit is not None:
                                    break
                        if unit_hit is not None:
                            cell_numbers.append((token, unit_hit[0]))
                distinct_cells = {(format(token, "f"), unit)
                                  for token, unit in cell_numbers}
                if len(distinct_cells) == 1:
                    token, unit = next(iter(distinct_cells))
                    hit = next(iter(iter_measurements(f"{token} {unit}")), None)
                    if hit is not None:
                        candidates.append((text, hit))
                        continue
                if cell_numbers:
                    return None, "ambiguous_measurements"
            distinct_text = {(format(item.value, "f"), item.unit)
                             for item in measurements if item.value is not None}
            if len(distinct_text) == 1:
                candidates.append((text, measurements[0]))
                continue
            if not measurements:
                cells = [chunk.strip() for chunk in text.split("|")]
                numbers = []
                for pos, chunk in enumerate(cells):
                    for token in _numbers(chunk):
                        neighbours = " ".join(cells[max(0, pos - 1):pos + 2])
                        unit_hit = canonical_unit(neighbours)
                        if unit_hit is None:
                            for cell in cells[max(0, pos - 1):pos + 2]:
                                unit_hit = canonical_unit(cell)
                                if unit_hit is not None:
                                    break
                        if unit_hit is not None:
                            numbers.append((token, unit_hit[0]))
                distinct = {(format(token, "f"), unit) for token, unit in numbers}
                if len(distinct) == 1:
                    token, unit = next(iter(distinct))
                    hit = next(iter(iter_measurements(f"{token} {unit}")), None)
                    if hit is not None:
                        candidates.append((text, hit))
                    continue
                if numbers:
                    return None, "ambiguous_measurements"
                continue
            if not (str(expected_work).strip() and "<tr" in quote.lower()
                    and "поставщик" in quote.lower()
                    and "наименован" in quote.lower()):
                return None, "ambiguous_measurements"
            work_terms = (_compact_terms(str(expected_work))
                          - _OPERATION_STOP_TERMS
                          - {term for term in _compact_terms(str(expected_work))
                             if any(char.isdigit() for char in term)})
            cells = [chunk.strip() for chunk in text.split("|")]
            row_measurements = [
                measurement for measurement in measurements
                if any(format(measurement.value, "f") in chunk
                       for chunk in cells if measurement.value is not None)]
            if (work_terms & _compact_terms(text)) and len(row_measurements) == 1:
                candidates.append((text, row_measurements[0]))
                continue
            return None, "ambiguous_measurements"
    distinct = {(format(item.value, "f"), item.unit)
                for _, item in candidates if item.value is not None}
    if not candidates:
        quoted_numbers = _numbers(" ".join(quotes))
        if not quoted_numbers:
            return None, "worded_quantity_without_digits"
        return None, "quantity_not_bound_to_unit"
    if len(distinct) != 1:
        # Several bound facts from distinct addressed fragments are a
        # source conflict (both kept), not an unresolvable ambiguity.
        pages = set()
        for quote in quotes:
            for text, _ in candidates:
                if text in _row_texts(quote):
                    pages.add(quote)
        if len(pages) > 1:
            return candidates[0][1], "conflicting_facts"
        return None, "ambiguous_measurements"
    return candidates[0][1], "bound"


def _quantity_binding_quotes(items):
    """Use explicit unit-cell evidence only when table provenance links it.

    Joined text is an internal parsing view, never a fabricated source quote.
    The persisted evidence remains the original, hash-bound fragments.
    """
    values = [item for item in items if item.get("binding_role") not in {"subject", "relation", "unit"}] or items
    quotes = []
    for value in values:
        quote = value["quote"]
        locator = value.get("locator") or {}
        for unit in items:
            if unit is value or unit.get("binding_role") != "unit":
                continue
            other = unit.get("locator") or {}
            same_table = (value.get("page") == unit.get("page") and locator.get("table_id")
                          and locator.get("table_id") == other.get("table_id"))
            same_row = locator.get("row_id") and locator.get("row_id") == other.get("row_id")
            linked_header = unit.get("source_id") in (locator.get("header_ids") or [])
            if same_table and (same_row or linked_header) and paired_measurements(quote, unit["quote"]):
                quote = unit["quote"] + " | " + quote
                break
        quotes.append(quote)
    return quotes


def _compare_bound_fact(fact, expected_volume, expected_unit):
    if expected_volume is None:
        return "equal"
    expected = parse_measurement(str(expected_volume), unit=str(expected_unit))
    actual = parse_measurement(str(fact.value), unit=fact.unit)
    comparison = compare_measurements(expected, actual)
    if comparison["state"] == "confirmed":
        return "equal"
    if comparison["state"] == "review_required":
        return "measurement_unresolved"
    return ("unit_mismatch" if comparison["reason"] == "incompatible_unit"
            else comparison["reason"])


def _facts_for(claim_id, proposal_evidence):
    facts = []
    for item in proposal_evidence:
        facts.append(EvidenceFact(
            claim_id=claim_id, value=item.get("quote", "")[:500], unit="",
            source_ids=[item.get("source_id", "")] if item.get("source_id") else [],
            value_source_id=item.get("source_id", "") if item.get("kind") == "volume" else "",
            unit_source_id=item.get("source_id", "") if item.get("kind") == "unit" else "",
            subject_source_ids=[item.get("source_id", "")] if item.get("kind") == "work" and item.get("source_id") else [],
            locators=[item.get("locator", {})],
        ))
    return facts


def _evaluate_presence(proposal_evidence, expected_work, position_type, context):
    if not proposal_evidence:
        return ClaimCheck(claim_id="presence", kind=ClaimKind.PRESENCE,
                          state=ClaimState.NOT_FOUND,
                          reasons=["no_proposal"], required=True), []
    if all(item.get("locator", {}).get("quality", "usable") != "usable"
           for item in proposal_evidence):
        return ClaimCheck(claim_id="presence", kind=ClaimKind.PRESENCE,
                          state=ClaimState.REVIEW_REQUIRED,
                          reasons=["unusable_table_structure"], required=True), proposal_evidence
    work_text = " ".join(item["quote"] for item in proposal_evidence)
    if str(expected_work).strip():
        page_ok = any(_shares_work_object(context[item["page"]], str(expected_work))
                      for item in proposal_evidence
                      if "<tr" not in context[item["page"]].lower())
        if not _shares_work_object(work_text, str(expected_work)) and not page_ok:
            return ClaimCheck(claim_id="presence", kind=ClaimKind.PRESENCE,
                              state=ClaimState.NOT_FOUND,
                              reasons=["work_object_mismatch"], required=True), []
    if position_type.casefold() == "работа":
        pages = {item["page"] for item in proposal_evidence}
        if pages and all(is_material_specification(context[page]) for page in pages):
            return ClaimCheck(claim_id="presence", kind=ClaimKind.PRESENCE,
                              state=ClaimState.REVIEW_REQUIRED,
                              reasons=["operation_needs_description_page"],
                              required=True), []
    return ClaimCheck(claim_id="presence", kind=ClaimKind.PRESENCE,
                      state=ClaimState.CONFIRMED, reasons=["work_cited"],
                      facts=_facts_for("presence", proposal_evidence),
                      required=True), proposal_evidence


def _evaluate_quantity(proposal_evidence, expected_volume, expected_unit,
                       expected_work):
    if not proposal_evidence:
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.NOT_FOUND, reasons=["no_proposal"],
                          required=True), []
    value_items = [item for item in proposal_evidence
                   if item.get("binding_role") not in {"subject", "relation", "unit"}]
    if not value_items:
        value_items = proposal_evidence
    quotes = _quantity_binding_quotes(proposal_evidence)
    if any(item.get("locator", {}).get("quality", "usable") != "usable"
           for item in value_items):
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.REVIEW_REQUIRED, reasons=["unusable_ocr"],
                          facts=_facts_for("quantity", proposal_evidence),
                          required=True), proposal_evidence
    fact, bound = _bind_quantity_fact(quotes, expected_work, expected_unit)
    if fact is None:
        if bound == "worded_quantity_without_digits":
            return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                              state=ClaimState.REVIEW_REQUIRED,
                              reasons=["worded_quantity_without_digits"],
                              required=True), proposal_evidence
        if bound == "ambiguous_measurements":
            return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                              state=ClaimState.REVIEW_REQUIRED,
                              reasons=["ambiguous_measurements"],
                              required=True), proposal_evidence
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.NOT_FOUND,
                          reasons=["quantity_not_bound_to_unit"],
                          required=True), []
    if bound == "conflicting_facts":
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.SOURCE_CONFLICT,
                          reasons=["conflicting_applicable_facts"],
                          facts=_facts_for("quantity", proposal_evidence),
                          required=True), proposal_evidence
    outcome = _compare_bound_fact(fact, expected_volume, expected_unit)
    if outcome == "measurement_unresolved":
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.REVIEW_REQUIRED, reasons=[outcome],
                          facts=_facts_for("quantity", proposal_evidence),
                          required=True), proposal_evidence
    if outcome == "equal":
        return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                          state=ClaimState.CONFIRMED, reasons=["equal"],
                          facts=_facts_for("quantity", proposal_evidence),
                          required=True), proposal_evidence
    return ClaimCheck(claim_id="quantity", kind=ClaimKind.QUANTITY,
                      state=ClaimState.CONTRADICTION, reasons=[outcome],
                      facts=_facts_for("quantity", proposal_evidence),
                      required=True), proposal_evidence


def _evaluate_unit(proposal_evidence, expected_unit):
    if not proposal_evidence:
        return ClaimCheck(claim_id="unit", kind=ClaimKind.UNIT,
                          state=ClaimState.NOT_FOUND, reasons=["no_proposal"],
                          required=True), []
    evidenced_units = set()
    for item in proposal_evidence:
        quote = item["quote"]
        texts = [quote]
        if "<tr" in quote.lower():
            rows = re.findall(r"<tr.*?>(.*?)</tr>", quote,
                              re.DOTALL | re.IGNORECASE)
            texts = [" | ".join(
                _plain_page(cell).strip() for cell in re.findall(
                    r"<t[dh][^>]*>(.*?)</t[dh]>", row,
                    re.DOTALL | re.IGNORECASE)) for row in rows] or texts
        for text in texts:
            evidenced_units.update(m.unit for m in iter_measurements(text))
            evidenced_units.update(m.unit for m in iter_paired_measurements(text))
            for chunk in text.split("|"):
                evidenced_units.update(unit for unit, _ in paired_units(chunk.strip()))
                hit = canonical_unit(chunk.strip())
                if hit is not None:
                    evidenced_units.add(hit[0])
    expected_hit = canonical_unit(str(expected_unit or ""))
    if expected_hit is not None and evidenced_units:
        if expected_hit[0] not in evidenced_units:
            return ClaimCheck(claim_id="unit", kind=ClaimKind.UNIT,
                              state=ClaimState.CONTRADICTION,
                              reasons=["unit_mismatch"],
                              facts=_facts_for("unit", proposal_evidence),
                              required=True), proposal_evidence
        return ClaimCheck(claim_id="unit", kind=ClaimKind.UNIT,
                          state=ClaimState.CONFIRMED, reasons=["unit_cited"],
                          facts=_facts_for("unit", proposal_evidence),
                          required=True), proposal_evidence
    if evidenced_units:
        return ClaimCheck(claim_id="unit", kind=ClaimKind.UNIT,
                          state=ClaimState.CONFIRMED, reasons=["unit_cited"],
                          facts=_facts_for("unit", proposal_evidence),
                          required=True), proposal_evidence
    return ClaimCheck(claim_id="unit", kind=ClaimKind.UNIT,
                      state=ClaimState.NOT_FOUND, reasons=["unit_not_bound"],
                      required=True), []


def _evaluate_method(proposal_evidence, expected_work, required):
    if not proposal_evidence:
        return ClaimCheck(claim_id="method", kind=ClaimKind.METHOD,
                          state=ClaimState.NOT_FOUND, reasons=["no_proposal"],
                          required=required), []
    actual_terms = _compact_terms(" ".join(
        item["quote"] for item in proposal_evidence))
    expected_terms = _compact_terms(str(expected_work or ""))
    if not expected_terms or not (expected_terms & actual_terms):
        return ClaimCheck(claim_id="method", kind=ClaimKind.METHOD,
                          state=ClaimState.REVIEW_REQUIRED,
                          reasons=["method_equivalence_unproven"],
                          required=required), proposal_evidence
    missing = sorted(term for term in expected_terms
                     if len(term) >= 5 and term not in actual_terms)
    if len(missing) >= 2:
        return ClaimCheck(claim_id="method", kind=ClaimKind.METHOD,
                          state=ClaimState.REVIEW_REQUIRED,
                          reasons=["method_terms_missing"], required=required), proposal_evidence
    return ClaimCheck(claim_id="method", kind=ClaimKind.METHOD,
                      state=ClaimState.CONFIRMED, reasons=["method_cited"],
                      facts=_facts_for("method", proposal_evidence),
                      required=required), proposal_evidence


def parse_audit_response(raw, context, comment_required=True, *, expected_volume=None,
                         expected_unit="", formula="", quote_sources=None,
                         position_type="", expected_work="", expected_scope="",
                         claim_specs=None):
    """Validate claim proposals and evaluate every requirement in code."""
    wire = _wire_claims(raw)
    specs = {spec.claim_id: spec for spec in (claim_specs or [])}
    known_ids = set(specs) or {"presence", "quantity", "unit", "method",
                               "attribute", "workbook", "comment"}
    grouped: dict[str, list[dict]] = {}
    rejected: list[str] = []
    for proposal in wire.get("claims", []):
        try:
            proposal = LlmClaimProposal.model_validate(proposal).model_dump()
        except ValueError as exc:
            rejected.append(f"Некорректное предложение: {exc}")
            continue
        claim_id = proposal.get("claim_id")
        if claim_id not in known_ids:
            rejected.append(f"Неизвестное требование: {claim_id}")
            continue
        kind = {"presence": "work", "quantity": "volume", "unit": "unit",
                "method": "technology", "attribute": "comment",
                "workbook": "comment", "comment": "comment"}.get(
                    claim_id, "comment")
        try:
            items = _proposal_evidence(proposal, quote_sources, context, kind)
            if expected_scope:
                wanted_scope = _scope_key(expected_scope)
                for item in items:
                    locator_scope = item.get("locator", {}).get("scope_key", "")
                    scopes = {_scope_key(locator_scope)} if locator_scope else set(
                        _known_scopes(_plain_page(item["quote"])))
                    if scopes and wanted_scope not in scopes:
                        raise ValueError("Источник относится к другому участку")
        except (EvidenceValidationError, QuoteValidationError, ValueError) as exc:
            rejected.append(f"{claim_id}: {exc}")
            continue
        grouped.setdefault(claim_id, []).extend(items)
    consumption_rate, consumption_unit = detect_consumption(expected_work, formula)
    checks: list[ClaimCheck] = []
    evidence: list[dict] = []
    presence_check, presence_evidence = _evaluate_presence(
        grouped.get("presence", []), expected_work, position_type, context)
    checks.append(presence_check)
    evidence.extend(presence_evidence)
    quantity_check, quantity_evidence = _evaluate_quantity(
        grouped.get("quantity", []), expected_volume, expected_unit,
        expected_work)
    checks.append(quantity_check)
    evidence.extend(quantity_evidence)
    unit_proposals = grouped.get("unit", [])
    if not unit_proposals and quantity_check.state in {"confirmed", "contradiction"}:
        unit_proposals = grouped.get("quantity", [])
    unit_check, unit_evidence = _evaluate_unit(unit_proposals, expected_unit)
    checks.append(unit_check)
    for item in unit_evidence:
        if item not in evidence:
            evidence.append(item)
    method_required = position_type.casefold() == "работа" and bool(
        str(expected_work).strip())
    method_check, method_evidence = _evaluate_method(
        grouped.get("method", []), expected_work, method_required)
    if method_required or grouped.get("method"):
        checks.append(method_check)
        evidence.extend(method_evidence)
    for spec in specs.values():
        if spec.claim_id in {"presence", "quantity", "unit", "method"}:
            continue
        items = grouped.get(spec.claim_id, [])
        checks.append(predicate_check(spec, items))
        evidence.extend(item for item in items if item not in evidence)
    geometry = geometry_product(specs, checks, grouped, expected_scope)
    if geometry is not None:
        checks.append(geometry)
    comment_items = grouped.get("attribute", []) + grouped.get("comment", [])
    if comment_required:
        mapped_comments = [check for check in checks if ":comment:" in check.claim_id]
        if mapped_comments:
            required_comments = [check for check in mapped_comments if check.required]
            closed = all(check.state in {ClaimState.CONFIRMED, ClaimState.NOT_APPLICABLE}
                         for check in required_comments)
            state = ClaimState.CONFIRMED if closed else ClaimState.REVIEW_REQUIRED
            checks.append(ClaimCheck(claim_id="comment", kind=ClaimKind.ATTRIBUTE,
                                     state=state, reasons=["comment_predicates_evaluated"],
                                     required=False))
        elif comment_items:
            checks.append(ClaimCheck(claim_id="comment", kind=ClaimKind.ATTRIBUTE,
                                     state=ClaimState.CONFIRMED,
                                     reasons=["comment_cited"], required=False))
            evidence.extend(comment_items)
        else:
            checks.append(ClaimCheck(claim_id="comment", kind=ClaimKind.ATTRIBUTE,
                                     state=ClaimState.NOT_FOUND,
                                     reasons=["comment_not_cited"], required=False))
    details = str(wire.get("discrepancy_details") or "")
    if rejected:
        with span("validate-claim-proposals", as_type="chain") as observation:
            observation.update(output={"rejected": rejected})
            if evidence:
                observation.update(metadata={"accepted_claims": sorted(grouped)})
    if not evidence and "не доказывает" not in details:
        details = (details + " В переданных фрагментах подходящих сведений не найдено; это не доказывает их отсутствие в PDF.").strip()
    by_id = {check.claim_id: check for check in checks}
    work_found = by_id["presence"].state == ClaimState.CONFIRMED
    volume_confirmed = by_id["quantity"].state == ClaimState.CONFIRMED
    unit_confirmed = by_id["unit"].state == ClaimState.CONFIRMED
    comment_confirmed = (by_id.get("comment") is not None
                         and by_id["comment"].state == ClaimState.CONFIRMED)
    if not comment_required:
        comment_confirmed = True
    found_pages = sorted({item["page"] for item in evidence})
    presence_pages = sorted({item["page"] for item in presence_evidence})
    quantity_pages = sorted({item["page"] for item in quantity_evidence})
    if len(presence_pages) == 1:
        found_page = str(presence_pages[0])
    elif len(quantity_pages) == 1:
        found_page = str(quantity_pages[0])
    elif presence_pages:
        found_page = str(presence_pages[0])
    else:
        found_page = "-"
    quantity_texts = _quantity_binding_quotes(quantity_evidence)
    fact, binding = _bind_quantity_fact(
        quantity_texts, expected_work, expected_unit) if quantity_texts else (None, "")
    if binding != "bound":
        fact = None
    found_volume = format(fact.value, "f") if fact is not None and fact.value is not None else "-"
    found_unit = fact.unit if fact is not None else "-"
    status = claim_status(checks)
    if status == "Ошибка" and not details:
        missing = [check.claim_id for check in checks
                   if check.required and check.state != ClaimState.CONFIRMED]
        details = "Не подтверждены: " + ", ".join(missing) + "."
    method_state = by_id.get("method").state if by_id.get("method") else None
    technology = {"status": "not_applicable", "expected": "", "actual": "",
                  "cross_pages": []}
    if position_type.casefold() == "работа" and str(expected_work).strip():
        if method_state == ClaimState.CONFIRMED:
            technology = {"status": "confirmed", "expected": str(expected_work),
                          "actual": " ".join(
                              item["quote"] for item in method_evidence)[:500],
                          "cross_pages": []}
        elif method_state in {ClaimState.NOT_FOUND, ClaimState.REVIEW_REQUIRED}:
            technology = {"status": "unconfirmed", "expected": str(expected_work),
                          "actual": "", "cross_pages": []}
    data = {
        "status": status,
        "work_found": work_found,
        "volume_confirmed": volume_confirmed,
        "unit_confirmed": unit_confirmed,
        "comment_confirmed": comment_confirmed,
        "found_volume": found_volume,
        "found_unit": found_unit,
        "found_page": found_page,
        "found_pages": found_pages,
        "evidence": [{key: item[key] for key in ("page", "quote", "kind", "source_id", "locator")
                      if key in item} for item in evidence],
        "discrepancy_details": details,
        "search_complete": work_found,
        "technology": technology,
        "quantity_basis": ("consumption" if consumption_rate is not None
                           else "direct" if found_volume != "-" else "unknown"),
        "consumption": ({"rate": format(consumption_rate, "f"),
                         "unit": consumption_unit}
                        if consumption_rate is not None else None),
        "checks": [check.model_dump(mode="json") for check in checks],
        "evaluation_version": "evidence-claims-v1",
    }
    return AuditResult.from_legacy(data).to_legacy()


def audit_text_pages(index, target_indices, ask, work_name, volume, reference="", comment="",
                     max_contexts=6, max_context_chars=10000, max_total_chars=60000, max_pages=4, unit="",
                     formula="", search_available=None, expected_scope=None, confirm=None):
    """Check references first; the first located work stops the search."""
    if min(max_contexts, max_context_chars, max_total_chars, max_pages) <= 0:
        raise ValueError("Лимиты поиска должны быть положительными")
    primary = list(dict.fromkeys(i for i in target_indices if 0 <= i < len(index.texts)))
    scope, related = index.related_context(primary, work_name, volume, formula, comment,
                                            expected_scope=expected_scope)
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

    def _checks(result):
        return {check["claim_id"]: check for check in result.get("checks", [])
                if isinstance(check, dict) and check.get("claim_id")}

    def _merge_claims(base, extra):
        """Merge validated checks by claim_id; confirmed facts never regress."""
        merged = _checks(base)
        for claim_id, check in _checks(extra).items():
            current = merged.get(claim_id)
            if current is None:
                merged[claim_id] = check
                continue
            facts = list(current.get("facts", []))
            facts.extend(fact for fact in check.get("facts", []) if fact not in facts)
            states = {check.get("state"), current.get("state")}
            if (states == {"confirmed", "contradiction"}
                    or "source_conflict" in states):
                merged[claim_id] = {**current, "state": "source_conflict",
                                    "facts": facts,
                                    "reasons": ["conflicting_applicable_facts"]}
                continue
            order = {"confirmed": 3, "contradiction": 3,
                     "review_required": 1, "not_found": 0, "not_applicable": 3}
            chosen = (check if order.get(check.get("state"), 0) >=
                      order.get(current.get("state"), 0) else current)
            merged[claim_id] = {**chosen, "facts": facts}
        return [merged[key] for key in sorted(merged)]

    def _summarize(checks, evidence, details):
        by_id = {check["claim_id"]: check for check in checks}
        work_found = by_id.get("presence", {}).get("state") == "confirmed"
        volume_confirmed = by_id.get("quantity", {}).get("state") == "confirmed"
        unit_confirmed = by_id.get("unit", {}).get("state") == "confirmed"
        comment = by_id.get("comment", {})
        comment_confirmed = comment.get("state") == "confirmed" if comment else True
        status = claim_status(checks)
        presence_pages = sorted({item["page"] for item in evidence
                                 if item.get("kind") == "work"})
        quantity_quotes = [item["quote"] for item in evidence
                           if item.get("kind") == "volume"]
        fact, binding = _bind_quantity_fact(
            quantity_quotes, work_name, unit) if quantity_quotes else (None, "")
        if binding != "bound":
            fact = None
        return {
            "status": status,
            "work_found": work_found,
            "volume_confirmed": volume_confirmed,
            "unit_confirmed": unit_confirmed,
            "comment_confirmed": comment_confirmed,
            "found_volume": format(fact.value, "f") if fact is not None and fact.value is not None else "-",
            "found_unit": fact.unit if fact is not None else "-",
            "found_page": str(presence_pages[0]) if len(presence_pages) == 1 else "-",
            "found_pages": sorted({item["page"] for item in evidence}),
            "evidence": evidence,
            "discrepancy_details": details,
            "search_complete": work_found,
            "checks": checks,
            "evaluation_version": "evidence-claims-v1",
        }

    def finish(result):
        result = dict(result)
        result["contexts"] = contexts
        if result.get("work_found") and result.get("found_page", "-") != "-":
            found_index = int(result["found_page"]) - 1
            if primary and found_index not in primary:
                adjacent = any(abs(found_index - page) == 1 for page in primary)
                if adjacent:
                    refs = ", ".join(str(page + 1) for page in primary)
                    note = f"Работа найдена на стр. {result['found_page']}, рядом со ссылкой ВОР (стр. {refs}). "
                else:
                    note = f"Работа найдена на стр. {result['found_page']}, вне ссылки ВОР. "
                result["discrepancy_details"] = note + result["discrepancy_details"]
            if any(i in failures or i in index.pending_ocr for i in primary):
                if result["status"] != "Ошибка":
                    result["status"] = INCOMPLETE
                result["search_complete"] = False
                result["discrepancy_details"] += " Не все страницы ссылки ВОР прочитаны; найденные доказательства сохранены."
        return result

    def confirm_claims(located):
        nonlocal contexts, characters, confirmation_used
        pending = [claim_id for claim_id, check in _checks(located).items()
                   if check.get("required", True)
                   and check.get("state") in {"not_found", "review_required"}]
        if confirmation_used or not pending:
            return located
        located_page = located.get("found_page", "-")
        work_pages = list(dict.fromkeys(
            ([int(located_page) - 1] if str(located_page).isdigit() else [])
            + [item["page"] - 1 for item in located.get("evidence", [])
               if item.get("kind") == "work"]))[:min(2, max_pages)]
        formula_numbers = _numbers(formula)
        with span("rank-quantity-pages", as_type="retriever",
                  input={"work": work_name, "volume": volume, "unit": unit,
                         "formula": formula, "pending_claims": pending}) as observation:
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
            result["discrepancy_details"] += " Работа найдена, но бюджет добора утверждений исчерпан."
            return result
        per_page = min(max_context_chars, max_total_chars - characters) // len(pages)
        if per_page < 1:
            result = dict(located)
            result.update(status=INCOMPLETE, search_complete=False)
            return result
        context = {}
        for i in pages:
            quotes = list(dict.fromkeys(item["quote"] for item in located.get("evidence", [])
                                        if item.get("kind") == "work" and item.get("page") == i + 1))
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
        with span("confirm-claims-context", as_type="chain", input=context) as observation:
            verified = confirm(context, pending) if confirm is not None else ask(context)
            observation.update(output=verified,
                               status_message=f"{verified['status']}; добор: {', '.join(pending)}",
                               level=result_level(verified["status"]))
        if verified["status"] == TECHNICAL_ERROR:
            result = dict(located)
            result.update(status=("Ошибка" if located["status"] == "Ошибка" else INCOMPLETE),
                          search_complete=False)
            result["discrepancy_details"] += " Добор утверждений не завершён: " + verified["discrepancy_details"]
            return result
        merged_checks = _merge_claims(located, verified)
        evidence = list(located.get("evidence", []))
        for item in verified.get("evidence", []):
            if item not in evidence:
                evidence.append(item)
        details = (located.get("discrepancy_details", "")
                   + " " + verified.get("discrepancy_details", "")).strip()
        merged = _summarize(merged_checks, evidence, details)
        merged["technology"] = verified.get("technology") or located.get("technology", {})
        merged["quantity_basis"] = verified.get("quantity_basis") or located.get("quantity_basis", "unknown")
        merged["consumption"] = verified.get("consumption") or located.get("consumption")
        return merged

    groups = []
    if primary:
        for start in range(0, len(primary), max_pages):
            groups.append(primary[start:start + max_pages])
    while groups or candidates:
        if contexts and search_available is not None and not search_available():
            break  # Keep an HTTP attempt for quantity confirmation; never grow the budget.
        from_reference = bool(groups)
        seeds = groups.pop(0) if groups else candidates[:min(2, max_pages)]
        if not from_reference and seeds and index.tables is not None and any(
                table.get("quality") == "usable" for table in index.tables[seeds[0]]):
            seeds = seeds[:1]
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
            # A VOR page reference may point at the wrong physical pages
            # (volume sheet numbers vs PDF file order). "Not in these
            # fragments" is never final while ranked candidates remain:
            # keep searching them instead of stopping after context 1.
            # Only a technical error without a recovery flag stops early.
            if result["status"] == TECHNICAL_ERROR and result.get("recoverable") is not True:
                return finish(result)
            continue
        if result["work_found"]:
            # First confirmed work stops the search: one bounded confirmation
            # at most; a mismatched quantity/unit is a finding, never a
            # reason to look for another work.
            with span("confirm-claims", as_type="chain", input=result) as observation:
                confirmed = confirm_claims(result)
                observation.update(output=confirmed, status_message=(
                    f"{confirmed['status']}; объём: {confirmed['found_volume']}; ед.: {confirmed['found_unit']}"),
                    level=result_level(confirmed["status"]))
            if confirmed["status"] == TECHNICAL_ERROR:
                return finish(confirmed)
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
