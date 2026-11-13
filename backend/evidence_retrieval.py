"""Offline evidence-retrieval channels and delivery measurement (stage 6).

Pure retrieval math plus code-owned checks. No PDF extraction, OCR, network,
or provider calls: ``prepare`` builds one snapshot per VOR position, ``compare``
reuses those snapshots for every ranking method. Gold manual transcriptions
are expectations only and are never injected as indexed text.

Ranking unit is the physical PDF page. Budgets mirror
``audit_core.audit_text_pages`` defaults: at most 4 pages and 10000 fragment
characters per position in a single ranking round (the 6-attempt LLM loop is
not executed offline; both the ranking list and the delivered context are
recorded so the cut is auditable).
"""

from __future__ import annotations

import math
import re
import unicodedata

from audit_measurements import (
    _numbers as _measure_numbers,
    close_amounts,
    compare_measurements,
    iter_measurements,
    parse_measurement,
)

MAX_PAGES = 4
MAX_CONTEXT_CHARS = 10000
MAX_CONTEXTS = 6  # parity label only: offline compare runs one ranking round

BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60

LOSS_REASONS = (
    "extraction_missing",
    "unusable_ocr",
    "not_ranked",
    "context_dropped",
    "binding_unresolved",
    "validation_rejected",
    "measurement_basis_unresolved",
    "source_conflict",
    "transport_error",
)

# --- Token stream: behavior mirror of audit_core._terms ----------------------
# Kept local (no core edits): set(token_stream(text)) must equal _terms(text);
# enforced by tests/test_evidence_retrieval.py::TokenParityTests.
_STOP_WORDS = set(
    "для при или как под над без работы работ объем объема количество проект "
    "документация лист страница согласно устройство выполнение установка монтаж "
    "демонтаж категория категории сложности условия условиях полевых "
    "выполненная выполненных применением".split()
)
_CODE_RE = re.compile(
    r"(?<!\w)(?=[a-zа-яё0-9-]*[0-9])[a-zа-яё0-9]+(?:-[a-zа-яё0-9]+)*(?!\w)"
)
_UNIT_RE = re.compile(r"[a-zа-яё]+/[a-zа-яё]+")


def token_stream(text: str) -> list[str]:
    """Token multiset with repetitions (BM25 needs term frequency).

    Same dictionary, stemming, and stop words as ``audit_core._terms``;
    only set semantics are lifted to a list.
    """
    text = unicodedata.normalize("NFKC", str(text)).lower().replace("ё", "е")
    tokens: list[str] = []
    for word in re.findall(r"[a-zа-яё]{3,}", text):
        if word in _STOP_WORDS:
            continue
        stem = re.sub(
            r"(иями|ами|ого|ему|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ов|ев"
            r"|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)$",
            "",
            word,
        )
        tokens.append((stem if len(stem) >= 3 else word)[:6])
    for code in _CODE_RE.findall(text):
        if len(code) >= 2 and any(c.isalpha() or c == "-" for c in code):
            tokens.append(code)
    tokens.extend(_UNIT_RE.findall(text))
    return tokens


def token_set(text: str) -> set[str]:
    """Set view; parity target for ``audit_core._terms``."""
    return set(token_stream(text))


# --- Ranking channels --------------------------------------------------------
def bm25_rank(doc_streams: list[list[str]], query_text: str) -> list[int]:
    """Page indices by BM25, descending; ties break by page number.

    k1=1.2, b=0.75, IDF = ln(1 + (N-df+0.5)/(df+0.5)). Zero-score pages
    do not enter the channel.
    """
    query_terms = set(token_stream(query_text))
    count = len(doc_streams)
    if count == 0 or not query_terms:
        return []
    lengths = [len(stream) for stream in doc_streams]
    average = sum(lengths) / count if count else 0.0
    doc_freq: dict[str, int] = {}
    for stream in doc_streams:
        for term in set(stream):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    scored: list[tuple[float, int]] = []
    for idx, stream in enumerate(doc_streams):
        if not stream:
            continue
        freq: dict[str, int] = {}
        for term in stream:
            freq[term] = freq.get(term, 0) + 1
        total = 0.0
        for term in query_terms:
            occurrences = freq.get(term, 0)
            if not occurrences:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
            denom = occurrences + BM25_K1 * (
                1 - BM25_B + BM25_B * (lengths[idx] / average if average else 0.0)
            )
            total += idf * occurrences * (BM25_K1 + 1) / denom if denom else 0.0
        if total > 0:
            scored.append((total, idx))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [idx for _, idx in scored]


def _distinct_codes(text: str) -> set[str]:
    lowered = unicodedata.normalize("NFKC", str(text)).lower().replace("ё", "е")
    return {
        code
        for code in _CODE_RE.findall(lowered)
        if len(code) >= 2 and any(c.isalpha() or c == "-" for c in code)
    }


def _distinct_measures(text: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for item in iter_measurements(text):
        if item.value is None or not item.unit:
            continue
        found.add((format(item.value, "f"), item.unit))
    return found


def exact_rank(
    texts: list[str],
    work_name: str,
    comment: str,
    volume: str,
    unit: str,
) -> tuple[list[int], dict[int, tuple[int, int]]]:
    """Pages with a normalized number+unit pair or mark/code hit.

    Page score is the lexicographic pair (distinct matched marks/codes,
    distinct matched measurements). Zero pages never enter the channel.
    """
    query_codes = _distinct_codes("%s %s" % (work_name, comment))
    expected = _distinct_measures("%s %s" % (volume, unit))
    expected |= _distinct_measures(comment)
    ranked: list[tuple[tuple[int, int], int]] = []
    details: dict[int, tuple[int, int]] = {}
    for idx, text in enumerate(texts):
        if not text.strip():
            continue
        page_codes = _distinct_codes(text)
        page_measures = _distinct_measures(text)
        code_hits = len(query_codes & page_codes)
        measure_hits = 0
        for value, code_unit in expected:
            for actual_value, actual_unit in page_measures:
                if actual_unit != code_unit:
                    continue
                try:
                    from decimal import Decimal

                    if close_amounts(Decimal(value), Decimal(actual_value)):
                        measure_hits += 1
                        break
                except Exception:
                    if value == actual_value:
                        measure_hits += 1
                        break
        if code_hits or measure_hits:
            ranked.append(((code_hits, measure_hits), idx))
            details[idx] = (code_hits, measure_hits)
    ranked.sort(key=lambda item: ((-item[0][0], -item[0][1]), item[1]))
    return [idx for _, idx in ranked], details


def rrf_fuse(rank_lists: list[list[int]]) -> list[int]:
    """Fuse channel page ranks: sum of 1/(60+rank), equal weights.

    Ranks are 1-based positions inside each channel list. Ties break by
    source page number. Pages absent from every channel stay absent.
    """
    fused: dict[int, float] = {}
    for ranked in rank_lists:
        for position, idx in enumerate(ranked, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + position)
    return sorted(fused, key=lambda idx: (-fused[idx], idx))


# --- Shared scope filter (quantity-independent) ------------------------------
def scope_mention_re(scope: str) -> "re.Pattern[str]":
    return re.compile(
        r"(?<![A-Za-zА-Яа-яЁё0-9])"
        + re.escape(scope)
        + r"(?![A-Za-zА-Яа-яЁё0-9])"
    )


def apply_scope_filter(
    fragment: str,
    expected_scope: str,
    other_scopes: list[str],
) -> tuple[str, int, bool]:
    """Drop rows naming only another scope; never drop a whole dual page.

    A line survives when it names the expected scope or names no scope at
    all. With unknown expected scope nothing is filtered and the page is
    flagged ambiguous instead of borrowing a convenient group.
    Returns (filtered text, dropped line count, ambiguous flag).
    """
    if not expected_scope:
        ambiguous = any(
            scope_mention_re(other).search(fragment) for other in other_scopes
        )
        return fragment, 0, ambiguous
    wanted = scope_mention_re(expected_scope)
    others = [scope_mention_re(other) for other in other_scopes if other]
    kept: list[str] = []
    dropped = 0
    for line in fragment.split("\n"):
        if wanted.search(line):
            kept.append(line)
            continue
        if any(pattern.search(line) for pattern in others):
            dropped += 1
            continue
        kept.append(line)
    return "\n".join(kept), dropped, False


def assemble_context(
    index: object,
    ranked_pages: list[int],
    primary_pages: list[int],
    work_name: str,
    comment: str,
    reference: str,
    volume: str,
    formula: str,
    expected_scope: str,
    other_scopes: list[str],
) -> dict[str, object]:
    """Select at most 4 pages / 10000 chars with existing fragment windows.

    The VOR reference stays the search start but never caps the top-up from
    the channel ranking. Records both the ranking list and the delivered
    context; they are not the same artifact.
    """
    ordered = list(dict.fromkeys(
        [i for i in primary_pages if 0 <= i < len(index.texts)]
        + [i for i in ranked_pages if 0 <= i < len(index.texts)]
    ))[:MAX_PAGES]
    per_page = MAX_CONTEXT_CHARS // len(ordered) if ordered else 0
    fragments: dict[int, str] = {}
    filtered: dict[int, str] = {}
    dropped_total = 0
    ambiguous = False
    for idx in ordered:
        raw = index.fragment(
            idx, work_name, comment, reference, volume, per_page,
            formula_numbers=_measure_numbers(formula or ""),
        )
        fragments[idx] = raw
        kept, dropped, page_ambiguous = apply_scope_filter(
            raw, expected_scope, other_scopes)
        filtered[idx] = kept
        dropped_total += dropped
        ambiguous = ambiguous or page_ambiguous
    context = {idx + 1: filtered[idx] for idx in ordered}
    chars = sum(len(text) for text in filtered.values())
    return {
        "ranking_1based": [idx + 1 for idx in ranked_pages],
        "ordered_1based": [idx + 1 for idx in ordered],
        "first_1based": [idx + 1 for idx in ordered
                         if idx in set(primary_pages)],
        "context": context,
        "fragments_raw": {idx + 1: fragments[idx] for idx in ordered},
        "context_chars": chars,
        "scope_dropped_lines": dropped_total,
        "scope_ambiguous": ambiguous,
    }


# --- Delivery chain -----------------------------------------------------------
def _normalized_contains(haystack: str, needle: str) -> bool:
    """Verbatim containment, then an OCR-spacing-tolerant fallback.

    The fallback mirrors ``audit_core._compact_terms`` (2-3 letter-token
    joins): single-character OCR splits (``П лощадь``) must not mark a
    genuinely extracted passage as missing. The fallback still requires
    the numbers/units of the probe to occur literally, so a wrong-group
    or wrong-number proposal can never pass on spacing tolerance alone.
    """
    from audit_core import _compact_terms, _quote_text

    normalized = _quote_text(needle)
    if bool(normalized) and normalized in _quote_text(haystack):
        return True
    terms = _compact_terms(needle)
    if not terms:
        return False
    hay_terms = _compact_terms(haystack)
    if not terms <= hay_terms:
        return False
    numbers = _measure_numbers(needle)
    return bool(numbers) and all(
        number in _measure_numbers(haystack) for number in numbers)


def _page_locator_source_id(index: object, idx: int) -> str:
    locators = getattr(index, "locators", None)
    if locators is not None and 0 <= idx < len(locators):
        for locator in locators[idx]:
            if isinstance(locator, dict) and locator.get("source_id"):
                return str(locator["source_id"])
    return "page-%d" % (idx + 1)


def evaluate_chain(
    evidence_id: str,
    evidence: dict,
    index: object,
    ranking_0based: list[int],
    assembly: dict,
    expected_volume: str,
    expected_unit: str,
) -> dict:
    """One gold evidence ID through extraction to code acceptance.

    Loss is the first failing stage; codes are machine-readable and stable:
    extraction_missing, unusable_ocr, not_ranked, context_dropped,
    binding_unresolved, validation_rejected, measurement_basis_unresolved,
    source_conflict, transport_error (offline runs never produce the last).
    """
    from audit_core import (
        EvidenceValidationError,
        QuoteValidationError,
        _bind_quantity_fact,
        _proposal_evidence,
    )

    page = int(evidence.get("page") or 0)
    idx = page - 1
    texts = list(getattr(index, "texts", ()))
    errors = dict(getattr(index, "errors", {}) or {})
    pending = set(getattr(index, "pending_ocr", ()) or ())
    context = assembly["context"]
    fragments_raw = assembly["fragments_raw"]
    rank_of = (
        ranking_0based.index(idx) + 1 if idx in ranking_0based else None
    )
    chain: dict[str, object] = {
        "evidence_id": evidence_id,
        "source_id": _page_locator_source_id(index, idx),
        "page_1based": page,
        "type": evidence.get("type"),
        "extracted": False,
        "usable": False,
        "rank": rank_of,
        "in_fragment": False,
        "in_first_context": False,
        "in_full_context": False,
        "bound": False,
        "sufficient": False,
        "loss": None,
    }

    kind = str(evidence.get("type") or "")
    quote = str(evidence.get("quote") or "")
    fields: dict = {}
    if kind == "table_cells":
        fields = dict(evidence.get("fields") or {})
        runtime_probe = _runtime_quantity_row(
            index, idx, dict(evidence.get("fields") or {}))
        # Gold fields carry empty slots (trailing "| ") that never occur
        # verbatim; the runtime grid row is the checkable text. Missing
        # runtime row keeps the fields-join so the chain records
        # binding_unresolved downstream instead of a false extraction hit.
        probe = runtime_probe if runtime_probe is not None else " | ".join(
            str(fields.get(key) or "") for key in (
                "position", "description", "unit", "quantity", "mass_kg")
        )
    elif kind.startswith("visual_"):
        probe = ""
    else:
        probe = quote

    if not (0 <= idx < len(texts)) or not texts[idx].strip():
        chain["loss"] = "extraction_missing"
        return chain
    if kind.startswith("visual_"):
        cells = evidence.get("cells") or {}
        cell_numbers = _measure_numbers(" ".join(str(v) for v in cells.values()))
        if not any(
            number in _measure_numbers(texts[idx]) for number in cell_numbers
        ):
            chain["loss"] = "extraction_missing"
            return chain
    elif not _normalized_contains(texts[idx], probe):
        chain["loss"] = "extraction_missing"
        return chain
    chain["extracted"] = True

    if idx in errors or idx in pending:
        chain["loss"] = "unusable_ocr"
        return chain
    chain["usable"] = True

    if rank_of is None:
        chain["loss"] = "not_ranked"
        return chain

    raw_fragment = fragments_raw.get(page, "")
    if kind.startswith("visual_"):
        chain["in_fragment"] = bool(raw_fragment.strip())
    else:
        chain["in_fragment"] = _normalized_contains(raw_fragment, probe)
    if not chain["in_fragment"]:
        chain["loss"] = "context_dropped"
        return chain

    in_full = page in context and (
        kind.startswith("visual_")
        or _normalized_contains(str(context[page]), probe)
    )
    chain["in_delivered_context"] = bool(
        page in context and (
            kind.startswith("visual_")
            or _normalized_contains(str(context[page]), probe)))
    chain["in_raw_page_text"] = bool(chain.get("extracted"))
    chain["in_full_context"] = bool(in_full)
    chain["in_first_context"] = bool(
        in_full and page in set(assembly.get("first_1based") or [])
    )
    if not in_full:
        chain["loss"] = "context_dropped"
        return chain

    if kind.startswith("visual_"):
        # Visual transcriptions are extraction expectations, never indexed
        # quotes: no verbatim binding exists offline.
        chain["loss"] = "binding_unresolved"
        return chain

    source_id = _page_locator_source_id(index, idx)
    # The probe is validated in its own addressable form: table rows keep
    # their pipe-joined grid text (cell slots preserved, not HTML), while
    # native quotes validate against the raw page text (spacing-tolerant).
    # Windowed-context cuts are a harness artifact, never proof the
    # evidence is absent.
    table_rows = getattr(index, "table_rows", None)
    validation_page = (
        " | ".join(
            (row if isinstance(row, str) else " | ".join(
                str(cell or "") for cell in row))
            for row in (table_rows[idx] if table_rows is not None
                        and 0 <= idx < len(table_rows)
                        and table_rows[idx] is not None else ()))
        if kind == "table_cells" else None)
    locator = {"page": page, "quote": probe, "source_id": source_id}
    quote_sources = {source_id: {"page": page, "quote": probe,
                                 "locator": locator}}
    proposal = {"claim_id": "benchmark", "source_ids": [source_id]}
    full_page_context = dict(context)
    full_page_context[page] = (
        validation_page if validation_page is not None
        else (texts[idx] if 0 <= idx < len(texts)
              else str(context.get(page, ""))))
    try:
        entries = _proposal_evidence(
            proposal, quote_sources, full_page_context, "work")
    except (EvidenceValidationError, QuoteValidationError, ValueError):
        # The gold probe is present in the extracted page (spacing-tolerant)
        # but not verbatim-bindable through the production quote validator:
        # an OCR-spacing/segmentation gap in the extraction, not a ranking
        # or delivery failure. Kept distinct from validation_rejected.
        chain["loss"] = "binding_unresolved"
        chain["reason"] = "quote_not_verbatim_bindable"
        return chain
    if not entries:
        chain["loss"] = "binding_unresolved"
        return chain

    if kind == "table_cells":
        row_text = _runtime_quantity_row(index, idx, fields)
        if row_text is None:
            chain["loss"] = "binding_unresolved"
            return chain
        fact, reason = _bind_quantity_fact([row_text])
        expected = parse_measurement(
            str(fields.get("quantity") or ""),
            unit=str(fields.get("unit") or ""),
        )
    else:
        # Work/presence evidence carries incidental measurements (e.g. an
        # overlap width inside a laying instruction): binding the only
        # measurement and quantity-comparing it against the position volume
        # manufactures contradictions. Only quantity-bearing evidence
        # (table_cells, or a probe whose measurement matches the expected
        # number) goes through measurement comparison; other probes prove
        # presence when they bind, not quantity.
        fact, reason = _bind_quantity_fact([probe])
        if fact is not None:
            from decimal import Decimal

            try:
                matches_expected = any(
                    close_amounts(Decimal(format(item.value, "f")),
                                  Decimal(str(expected_volume).replace(",", ".")))
                    for item in iter_measurements(probe)
                    if item.value is not None)
            except Exception:
                matches_expected = False
            if not matches_expected:
                chain["bound"] = True
                chain["presence_only"] = True
                return chain
        expected = parse_measurement(expected_volume, unit=expected_unit)
    if fact is None:
        chain["loss"] = (
            "measurement_basis_unresolved"
            if reason == "ambiguous_measurements"
            else "binding_unresolved"
        )
        return chain
    chain["bound"] = True
    if expected.status == "unknown":
        chain["loss"] = "measurement_basis_unresolved"
        return chain
    actual = parse_measurement(
        "%s %s" % (format(fact.value, "f"), fact.unit) if fact.value is not None
        else (fact.raw or ""),
        unit=fact.unit or "",
    )
    verdict = compare_measurements(expected, actual)
    if verdict.get("state") == "confirmed":
        chain["sufficient"] = True
        return chain
    chain["loss"] = (
        "source_conflict"
        if verdict.get("state") == "contradiction"
        and kind != "table_cells"
        else "validation_rejected"
    )
    chain["verdict"] = verdict.get("state")
    chain["reason"] = verdict.get("reason")
    return chain


def _runtime_quantity_row(
    index: object, idx: int, fields: dict
) -> str | None:
    """Pipe-joined runtime table row carrying the expected quantity+unit.

    Uses only extracted table rows, never gold cell text: row/header
    binding coverage must come from the real grid.
    """
    table_rows = getattr(index, "table_rows", None)
    if table_rows is None or idx >= len(table_rows):
        return None
    wanted_numbers = _measure_numbers(str(fields.get("quantity") or ""))
    for row in table_rows[idx]:
        # table_rows stores pipe-joined row strings; never iterate a string
        # cell-by-cell (that shreds the quantity into single characters).
        joined = row if isinstance(row, str) else " | ".join(
            str(cell or "") for cell in row)
        row_numbers = _measure_numbers(joined)
        if not (wanted_numbers & row_numbers):
            continue
        # Row acceptance mirrors the production binder: a bare quantity in
        # its own cell pairs with a neighbouring unit cell, so requiring an
        # inline atomic number+unit pair would reject genuine schedule rows
        # (e.g. `| шт. | 165 |`). Only rows the production code itself binds
        # count as row-bound evidence.
        from audit_core import _bind_quantity_fact  # noqa: PLC0415
        fact, _ = _bind_quantity_fact([joined])
        if fact is not None:
            return joined
    return None


def row_header_binding(
    index: object, page_1based: int, fields: dict
) -> dict[str, bool]:
    """Row/header coverage from the real grid, not from gold text."""
    idx = page_1based - 1
    row_text = _runtime_quantity_row(index, idx, fields)
    texts = list(getattr(index, "texts", ()))
    page_text = texts[idx] if 0 <= idx < len(texts) else ""
    description = str(fields.get("description") or "")
    content = [w for w in token_stream(description) if len(w) >= 4]
    header_hit = (
        len(set(content) & token_set(page_text)) >= 2
        if content
        else False
    )
    return {"row_bound": row_text is not None, "header_bound": bool(header_hit)}
