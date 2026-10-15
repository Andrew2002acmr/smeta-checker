"""Page selection and audit orchestration, independent of Streamlit and PDF objects."""

import json
import math
import re
from collections import Counter
from decimal import Decimal
import unicodedata

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
    return {"status": status, "work_found": False, "volume_confirmed": False,
            "unit_confirmed": False, "comment_confirmed": False,
            "found_volume": "-", "found_unit": "-", "found_page": "-",
            "found_pages": [], "evidence": [], "search_complete": False,
            "requests": 0, "discrepancy_details": str(message)}


def technical_error(message):
    return empty_result(TECHNICAL_ERROR, message)


class BudgetExceeded(Exception):
    pass


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
        # UTF-8 bytes are a conservative upper bound for byte-tokenized input.
        # Without provider usage (including failed attempts), keep this charge.
        reserve = len(prompt.encode("utf-8")) + self.max_output_tokens
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

    def can_retry(self, prompt):
        """A retry must leave one equally sized attempt for another context."""
        reserve = len(prompt.encode("utf-8")) + self.max_output_tokens
        return (self.requests + 2 <= self.max_requests
                and self.tokens + 2 * reserve <= self.max_total_tokens)


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


def _terms(text):
    words = re.findall(r"[a-zа-яё]{3,}", str(text).lower().replace("ё", "е"))
    result = set()
    for word in words:
        if word in _STOP_WORDS:
            continue
        stem = re.sub(r"(иями|ами|ого|ему|ыми|ими|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)$", "", word)
        result.add((stem if len(stem) >= 3 else word)[:6])
    return result


def _numbers(text):
    """Comparable decimal tokens without losing significant fractional digits."""
    return {Decimal(token.replace(",", "."))
            for token in re.findall(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?", str(text))}


_TABLE_MARKERS = re.compile(r"объем|объём|количеств|ед\.\s*изм|таблиц|спецификац", re.IGNORECASE)


class PageTextIndex:
    def __init__(self, texts, errors=None, pending_ocr=(), table_rows=None):
        self.texts = tuple(texts)
        self.errors = dict(errors or {})
        self.pending_ocr = tuple(sorted(set(pending_ocr)))
        self.table_rows = tuple(tuple(row) for row in table_rows) if table_rows is not None else None
        self.terms = tuple(_terms(text) for text in self.texts)
        frequency = Counter(term for terms in self.terms for term in terms)
        self.weights = {term: math.log(1 + len(self.texts) / count) for term, count in frequency.items()}

    def _score(self, text, work_name, comment, reference, volume, prefer_volume=False):
        terms = _terms(text)
        score = sum(4 * self.weights.get(t, 1) for t in terms & _terms(work_name))
        score += sum(self.weights.get(t, 1) for t in terms & _terms(comment))
        numbers = _numbers(text)
        if numbers & _numbers(volume):
            score += 1000 if prefer_volume else 4
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

    def support_candidates(self, work_name, volume, unit, excluded=()):
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
            score = self._score(text, work_name, "", "", volume)
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

    def fragment(self, idx, work_name, comment, reference, volume, limit, prefer_volume=False):
        """Keep source-order, non-overlapping intervals and a small page header."""
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
        intervals = [(0, header_end)] if header_end else []
        separator = "\n[...]\n"
        # A highest-scoring window must fit in full, even with a distant header.
        size = min(1400, max(1, limit - header_end - (len(separator) if header_end else 0)))
        step = max(1, size - min(200, size // 4))
        starts = range(0, len(text), step)
        windows = [(self._score(text[start:start + size], work_name, comment, reference, volume, prefer_volume),
                    start, min(start + size, len(text))) for start in starts]
        windows.sort(key=lambda item: (-item[0], item[1]))

        def merged(parts):
            result = []
            for start, end in sorted(parts):
                if result and start <= result[-1][1]:
                    result[-1] = (result[-1][0], max(end, result[-1][1]))
                else:
                    result.append((start, end))
            return result

        for _, start, end in windows:
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
                    right -= used - limit
        return separator.join(text[start:end] for start, end in intervals)


class QuoteValidationError(ValueError):
    def __init__(self, page, quote):
        super().__init__(f"Цитата не найдена в переданном тексте стр. {page}")
        self.quote = quote


def _quote_text(text):
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans({
        "\u00ad": "", "‐": "-", "‑": "-", "–": "-", "—": "-", "−": "-",
        "«": '"', "»": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
    }))
    return re.sub(r"\s+", " ", text).strip().casefold().replace("ё", "е")


def parse_audit_response(raw, context, comment_required=True):
    """Accept only evidence from the exact page fragments supplied to the model."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Модель вернула пустой ответ")
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("В ответе модели нет JSON")
    data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    required = {"work_found", "volume_confirmed", "unit_confirmed",
                "comment_confirmed", "found_volume", "found_unit", "found_page",
                "evidence", "discrepancy_details"}
    if not isinstance(data, dict) or not required <= data.keys():
        raise ValueError("В ответе модели отсутствуют обязательные поля")
    for field in ("work_found", "volume_confirmed", "unit_confirmed", "comment_confirmed"):
        if not isinstance(data[field], bool):
            raise ValueError(f"Поле {field} должно быть логическим")
    for field in ("found_volume", "found_unit", "discrepancy_details"):
        value = data[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            raise ValueError(f"Некорректное поле {field}")
        data[field] = str(value).strip() if value is not None else ""
    data["found_volume"] = data["found_volume"] or "-"
    data["found_unit"] = data["found_unit"] or "-"
    if not isinstance(data["evidence"], list):
        raise ValueError("Доказательства должны быть списком цитат")
    evidence = []
    for item in data["evidence"]:
        if not isinstance(item, dict):
            raise ValueError("Некорректное доказательство")
        page, quote, kind = item.get("page"), item.get("quote"), item.get("kind")
        if isinstance(page, str) and re.fullmatch(r"\d+", page.strip()):
            page = int(page.strip())
        if isinstance(page, bool) or not isinstance(page, int) or page not in context:
            raise ValueError(f"Цитата ссылается на страницу {page}, которой не было в запросе")
        if kind not in {"work", "volume", "unit", "comment"}:
            raise ValueError("Неизвестный вид доказательства")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 1200:
            raise ValueError("Некорректная цитата")
        normalized_quote = _quote_text(quote)
        if not normalized_quote or not any(normalized_quote in _quote_text(piece)
                                           for piece in context[page].split("\n[...]\n")):
            raise QuoteValidationError(page, quote)
        evidence.append({"page": page, "quote": quote.strip(), "kind": kind})
    data["evidence"] = evidence
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
        if not any(item["page"] == page and item["kind"] == "work" for item in evidence):
            raise ValueError("Найденная работа не подкреплена цитатой с указанной страницы")
        data["found_page"] = str(page)
        for flag, kind, field in (("volume_confirmed", "volume", "found_volume"),
                                  ("unit_confirmed", "unit", "found_unit")):
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
                raise ValueError(f"Поле {field} не подкреплено доказательством")
            if kind == "volume" and data[field] != "-":
                quoted_numbers = _numbers(" ".join(quotes))
                if quoted_numbers and not _numbers(data[field]) <= quoted_numbers:
                    raise ValueError("Фактический объем отсутствует в цитате")
        if comment_required and data["comment_confirmed"] and not any(e["kind"] == "comment" for e in evidence):
            raise ValueError("Подтверждение примечания не подкреплено цитатой")
    if not comment_required:
        data["comment_confirmed"] = True
    data["found_pages"] = sorted({e["page"] for e in evidence})
    data["status"] = "ОК" if all(data[f] for f in (
        "work_found", "volume_confirmed", "unit_confirmed", "comment_confirmed")) else "Ошибка"
    if data["status"] == "Ошибка" and not data["discrepancy_details"]:
        missing = [label for flag, label in (
            ("work_found", "работа"), ("volume_confirmed", "объём"),
            ("unit_confirmed", "единица измерения"), ("comment_confirmed", "примечание"))
            if not data[flag]]
        data["discrepancy_details"] = "Не подтверждены: " + ", ".join(missing) + "."
    data["search_complete"] = data["work_found"]
    return data


def audit_text_pages(index, target_indices, ask, work_name, volume, reference="", comment="",
                     max_contexts=6, max_context_chars=10000, max_total_chars=60000, max_pages=4, unit=""):
    """Check references first; preserve mismatches and bound alternative locations."""
    if min(max_contexts, max_context_chars, max_total_chars, max_pages) <= 0:
        raise ValueError("Лимиты поиска должны быть положительными")
    primary = list(dict.fromkeys(i for i in target_indices if 0 <= i < len(index.texts)))
    with span("rank-candidate-pages", as_type="retriever", input={
            "work": work_name, "volume": volume, "comment": comment, "reference": reference}) as observation:
        candidates = index.candidates(work_name, comment, reference, volume, primary)
        degraded = [i for i in primary if i in index.errors or i in index.pending_ocr]
        observation.update(output={"primary_pages": [i + 1 for i in primary],
                                   "ranked_pages": [i + 1 for i in candidates],
                                   "pending_ocr": [i + 1 for i in index.pending_ocr],
                                   "reading_errors": {i + 1: e for i, e in index.errors.items()}},
                           status_message=(f"Ссылка: {[i + 1 for i in primary]}; кандидатов: {len(candidates)}; "
                                           f"pending OCR: {len(index.pending_ocr)}; сбоев: {len(index.errors)}"),
                           level="WARNING" if degraded else "DEFAULT")
    visited, contexts, characters = set(), 0, 0
    failures = {i: error for i, error in index.errors.items() if i in primary}
    best, last_failure, locations = None, None, 0
    confirmation_used = False

    def quality(result):
        # A complete finding outranks an unresolved description. A concrete
        # mismatch on a referenced page already terminates before this choice.
        on_reference = int(result["found_page"]) - 1 in primary
        actual_known = result["found_volume"] != "-" and result["found_unit"] != "-"
        return (on_reference and actual_known, actual_known, on_reference,
                sum(bool(result.get(flag)) for flag in
                    ("volume_confirmed", "unit_confirmed", "comment_confirmed")))

    def finish(result):
        result = dict(result)
        result["contexts"] = contexts
        if result.get("work_found"):
            if int(result["found_page"]) - 1 not in primary:
                if result["status"] not in {TECHNICAL_ERROR, INCOMPLETE}:
                    result["status"] = "Ошибка"
                result["discrepancy_details"] = (
                    f"Работа найдена на стр. {result['found_page']}, вне ссылки ВОР. "
                    + result["discrepancy_details"])
            if any(i in failures or i in index.pending_ocr for i in primary):
                result["status"] = INCOMPLETE
                result["search_complete"] = False
                result["discrepancy_details"] += " Не все страницы ссылки ВОР прочитаны; найденные доказательства сохранены."
        return result

    def confirm_quantities(located):
        nonlocal contexts, characters, confirmation_used
        # Actual differing quantities/units are findings, not failed searches.
        if confirmation_used or (located["found_volume"] != "-" and located["found_unit"] != "-"):
            return located
        work_pages = list(dict.fromkeys(
            [int(located["found_page"]) - 1]
            + [e["page"] - 1 for e in located["evidence"] if e["kind"] == "work"]))[:min(2, max_pages)]
        with span("rank-quantity-pages", as_type="retriever",
                  input={"work": work_name, "volume": volume, "unit": unit}) as observation:
            quantity_pages = index.support_candidates(work_name, volume, unit)
            observation.update(output={"ranked_pages": [i + 1 for i in quantity_pages]},
                               status_message=f"Кандидатов: {len(quantity_pages)}")
        support = [i for i in quantity_pages if i not in work_pages]
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
            fragment = index.fragment(i, work_name, "", reference, volume, capacity, prefer_volume=True)
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
        if verified["status"] in {TECHNICAL_ERROR, INCOMPLETE} or not verified.get("work_found"):
            result = dict(located)
            result.update(status=verified["status"] if verified["status"] == TECHNICAL_ERROR else INCOMPLETE,
                          search_complete=False)
            result["discrepancy_details"] += " Подтверждение связанных данных не завершено: " + verified["discrepancy_details"]
            return result
        if not any(e["kind"] == "work" and e["page"] == int(located["found_page"])
                   for e in verified["evidence"]):
            result = dict(located)
            result.update(status=INCOMPLETE, search_complete=False)
            result["discrepancy_details"] += " Добор не подтвердил связь с первоначально найденной работой."
            return result
        result = dict(verified)
        result["found_page"] = located["found_page"]
        evidence = [e for e in located["evidence"] if e["kind"] == "work"]
        evidence += [e for e in verified["evidence"] if e not in evidence]
        result["evidence"] = evidence
        result["found_pages"] = sorted({e["page"] for e in evidence})
        return result

    groups = []
    if primary:
        for start in range(0, len(primary), max_pages):
            groups.append(primary[start:start + max_pages])
    while groups or candidates:
        from_reference = bool(groups)
        seeds = groups.pop(0) if groups else candidates[:min(2, max_pages)]
        candidates = [i for i in candidates if i not in seeds]
        pages = list(seeds)
        # Neighbours support referenced pages, but are not a semantic relation
        # between arbitrary search hits in an assembled PDF.
        for seed in seeds if from_reference else ():
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
            context = {i + 1: index.fragment(i, work_name, comment, reference, volume, per_page)
                       for i in readable}
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
        if result["status"] in {TECHNICAL_ERROR, INCOMPLETE}:
            last_failure = result
            if result.get("recoverable"):
                continue
            if best is not None:
                retained = dict(best)
                retained.update(status=result["status"], search_complete=False)
                retained["discrepancy_details"] += " Проверка следующего контекста не завершена: " + result["discrepancy_details"]
                return finish(retained)
            return finish(result)
        if result["work_found"]:
            locations += 1
            with span("confirm-quantities", as_type="chain", input=result) as observation:
                confirmed = confirm_quantities(result)
                observation.update(output=confirmed, status_message=(
                    f"{confirmed['status']}; объём: {confirmed['found_volume']}; ед.: {confirmed['found_unit']}"),
                    level=result_level(confirmed["status"]))
            if confirmed["status"] == TECHNICAL_ERROR:
                return finish(confirmed)
            if best is None or quality(confirmed) > quality(best):
                best = confirmed
            on_reference = int(confirmed["found_page"]) - 1 in primary
            actual_known = confirmed["found_volume"] != "-" and confirmed["found_unit"] != "-"
            if (on_reference and actual_known) or locations >= 2:
                return finish(best)
    if best is not None:
        if best["found_volume"] == "-" or best["found_unit"] == "-":
            best = dict(best)
            best.update(status=INCOMPLETE, search_complete=False)
            best["discrepancy_details"] += " Найденная работа сохранена; количество или единица не установлены."
        return finish(best)
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
