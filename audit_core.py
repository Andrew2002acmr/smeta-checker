"""Page selection and audit orchestration, independent of Streamlit and PDF objects."""

import json
import math
import re
from collections import Counter

from audit_tracing import span


TECHNICAL_ERROR = "Технический сбой"
INCOMPLETE = "Неполная проверка"


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


def parse_page_reference(value, page_count):
    """Return physical PDF indices, preserving warnings for invalid references."""
    raw = str(value).strip() if value is not None else ""
    if raw.lower() in {"", "nan", "none", "null", "-"}:
        return [], "Страница не указана", []
    # Excel can supply a single page as a float, e.g. 9.0.
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".")[0]
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


class PageTextIndex:
    def __init__(self, texts, errors=None, pending_ocr=()):
        self.texts = tuple(texts)
        self.errors = dict(errors or {})
        self.pending_ocr = tuple(sorted(set(pending_ocr)))
        self.terms = tuple(_terms(text) for text in self.texts)
        frequency = Counter(term for terms in self.terms for term in terms)
        self.weights = {term: math.log(1 + len(self.texts) / count) for term, count in frequency.items()}

    def _score(self, text, work_name, comment, reference, volume, prefer_volume=False):
        terms = _terms(text)
        score = sum(4 * self.weights.get(t, 1) for t in terms & _terms(work_name))
        score += sum(self.weights.get(t, 1) for t in terms & _terms(comment))
        number = str(volume).replace(",", ".").removesuffix(".0")
        numbers = {n.replace(",", ".").removesuffix(".0")
                   for n in re.findall(r"\d+(?:[.,]\d+)?", text)}
        if number in numbers:
            score += 1000 if prefer_volume else 0.5
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
        number = str(volume).replace(",", ".").removesuffix(".0")
        ranked = []
        for idx, text in enumerate(self.texts):
            if idx in excluded or not self.terms[idx] & query:
                continue
            numbers = {n.replace(",", ".").removesuffix(".0")
                       for n in re.findall(r"\d+(?:[.,]\d+)?", text)}
            if number not in numbers:
                continue
            score = self._score(text, work_name, "", "", volume)
            if re.search(r"объем|объём|количеств|ед\.\s*изм|таблиц", text, re.IGNORECASE):
                score += 5
            if unit and (_terms(unit) & self.terms[idx]
                         or re.search(r"(?<!\w)" + re.escape(unit) + r"(?!\w)", text, re.IGNORECASE)):
                score += 3
            ranked.append((score, idx))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [idx for _, idx in ranked]

    def fragment(self, idx, work_name, comment, reference, volume, limit, prefer_volume=False):
        """Rank windows over the entire page locally; never send every chunk."""
        text = self.texts[idx]
        if len(text) <= limit:
            return text
        size = min(1400, limit)
        windows = [(self._score(text[start:start + size], work_name, comment, reference, volume, prefer_volume),
                    start, text[start:start + size])
                   for start in range(0, len(text), max(1, size - 200))]
        windows.sort(key=lambda item: (-item[0], item[1]))
        chosen, remaining = [], limit
        for _, start, window in windows:
            separator = len("\n[...]\n") if chosen else 0
            if remaining <= separator:
                break
            window = window[:remaining - separator]
            chosen.append((start, window))
            remaining -= len(window) + separator
        return "\n[...]\n".join(window for _, window in sorted(chosen))


def _quote_text(text):
    return re.sub(r"\s+", " ", text.replace("\u00ad", "")).strip().casefold().replace("ё", "е")


_MODEL_STATUS_ERROR_ALIASES = frozenset({
    "частичное совпадение", "расхождение", "частичное расхождение",
})


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
    required = {"status", "work_found", "volume_confirmed", "unit_confirmed",
                "comment_confirmed", "found_volume", "found_unit", "found_page",
                "evidence", "discrepancy_details"}
    if not isinstance(data, dict) or not required <= data.keys():
        raise ValueError("В ответе модели отсутствуют обязательные поля")
    status = data["status"]
    if not isinstance(status, str):
        raise ValueError("Некорректный тип статуса в ответе модели")
    status = status.strip()
    if status.casefold() in _MODEL_STATUS_ERROR_ALIASES:
        status = "Ошибка"
    if status.upper().translate(str.maketrans({"O": "О", "K": "К"})) != "ОК" and status.lower() != "ошибка":
        raise ValueError("Неизвестный статус в ответе модели")
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
        if _quote_text(quote) not in _quote_text(context[page]):
            raise ValueError(f"Цитата не найдена в переданном тексте стр. {page}")
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
                numbers = lambda text: {n.replace(",", ".").removesuffix(".0")
                                        for n in re.findall(r"\d+(?:[.,]\d+)?", text)}
                quoted_numbers = numbers(" ".join(quotes))
                if quoted_numbers and not numbers(data[field]) <= quoted_numbers:
                    raise ValueError("Фактический объем отсутствует в цитате")
        if comment_required and data["comment_confirmed"] and not any(e["kind"] == "comment" for e in evidence):
            raise ValueError("Подтверждение примечания не подкреплено цитатой")
    if not comment_required:
        data["comment_confirmed"] = True
    data["found_pages"] = sorted({e["page"] for e in evidence})
    data["status"] = "ОК" if all(data[f] for f in (
        "work_found", "volume_confirmed", "unit_confirmed", "comment_confirmed")) else "Ошибка"
    data["search_complete"] = data["work_found"]
    return data


def audit_text_pages(index, target_indices, ask, work_name, volume, reference="", comment="",
                     max_requests=6, max_context_chars=10000, max_total_chars=60000, max_pages=4, unit=""):
    """Find a location once, then retain its bounded multi-page audit, even on mismatch."""
    if min(max_requests, max_context_chars, max_total_chars, max_pages) <= 0:
        raise ValueError("Лимиты поиска должны быть положительными")
    primary = list(dict.fromkeys(i for i in target_indices if 0 <= i < len(index.texts)))
    with span("rank-candidate-pages", as_type="retriever", input={
            "work": work_name, "volume": volume, "comment": comment, "reference": reference}) as observation:
        candidates = index.candidates(work_name, comment, reference, volume, primary)
        observation.update(output={"primary_pages": [i + 1 for i in primary],
                                   "ranked_pages": [i + 1 for i in candidates],
                                   "pending_ocr": [i + 1 for i in index.pending_ocr],
                                   "reading_errors": {i + 1: e for i, e in index.errors.items()}})
    visited, requests, characters = set(), 0, 0
    failures = {i: error for i, error in index.errors.items() if i in primary}

    def finish(result):
        result = dict(result)
        result["requests"] = requests
        if result.get("work_found"):
            if int(result["found_page"]) - 1 not in primary:
                if result["status"] not in {TECHNICAL_ERROR, INCOMPLETE}:
                    result["status"] = "Ошибка"
                result["discrepancy_details"] = (
                    f"Работа найдена на стр. {result['found_page']}, вне ссылки ВОР. "
                    + result["discrepancy_details"])
            if failures or any(i in index.pending_ocr for i in primary):
                result["status"] = INCOMPLETE
                result["search_complete"] = False
                result["discrepancy_details"] += " Не все страницы ссылки ВОР прочитаны; найденные доказательства сохранены."
        return result

    def confirm_quantities(located):
        nonlocal requests, characters
        # A genuine volume mismatch or missing price coefficient needs no more search.
        if located["found_volume"] != "-" and located["unit_confirmed"]:
            return located
        work_pages = list(dict.fromkeys(
            [int(located["found_page"]) - 1]
            + [e["page"] - 1 for e in located["evidence"] if e["kind"] == "work"]))[:min(2, max_pages)]
        with span("rank-quantity-pages", as_type="retriever",
                  input={"work": work_name, "volume": volume, "unit": unit}) as observation:
            quantity_pages = index.support_candidates(work_name, volume, unit)
            observation.update(output={"ranked_pages": [i + 1 for i in quantity_pages]})
        support = [i for i in quantity_pages if i not in work_pages]
        pages = work_pages + support[:max_pages - len(work_pages)]
        if len(pages) == len(work_pages) and not any(i in quantity_pages for i in work_pages):
            return located
        if requests >= max_requests or characters >= max_total_chars:
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
            prefix = "\n".join(quotes)[:per_page]
            separator = "\n[...]\n" if prefix else ""
            capacity = per_page - len(prefix) - len(separator)
            fragment = index.fragment(i, work_name, "", reference, volume, capacity, prefer_volume=True) if capacity > 0 else ""
            context[i + 1] = prefix + (separator + fragment if fragment else "")
        characters += sum(len(text) for text in context.values())
        visited.update(pages)
        requests += 1
        with span("confirm-quantity-context", as_type="chain", input=context) as observation:
            verified = ask(context)  # exactly one bounded confirmation, never a page/chunk loop
            observation.update(output=verified)
        if verified["status"] in {TECHNICAL_ERROR, INCOMPLETE} or not verified.get("work_found"):
            result = dict(located)
            result.update(status=verified["status"] if verified["status"] == TECHNICAL_ERROR else INCOMPLETE,
                          search_complete=False)
            result["discrepancy_details"] += " Подтверждение связанных данных не завершено: " + verified["discrepancy_details"]
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
        seeds = groups.pop(0) if groups else candidates[:min(2, max_pages)]
        candidates = [i for i in candidates if i not in seeds]
        pages = list(seeds)
        for seed in seeds:
            for neighbor in (seed - 1, seed + 1):
                if 0 <= neighbor < len(index.texts) and neighbor not in pages and len(pages) < max_pages:
                    pages.append(neighbor)
        readable = [i for i in pages if index.texts[i].strip()]
        for i in pages:
            if i in index.errors:
                failures[i] = index.errors[i]
        if not readable or not any(i not in visited for i in readable):
            continue
        if requests >= max_requests or characters >= max_total_chars:
            break
        limit = min(max_context_chars, max_total_chars - characters)
        per_page = limit // len(readable)
        if per_page < 1:
            break
        with span("select-context", as_type="retriever",
                  input={"pages": [i + 1 for i in readable], "per_page_chars": per_page}) as observation:
            context = {i + 1: index.fragment(i, work_name, comment, reference, volume, per_page)
                       for i in readable}
            observation.update(output=context, metadata={"context_chars": sum(map(len, context.values()))})
        visited.update(readable)
        characters += sum(len(text) for text in context.values())
        requests += 1
        with span("locate-work", as_type="chain", input=context) as observation:
            result = ask(context)
            observation.update(output=result)
        if result["status"] in {TECHNICAL_ERROR, INCOMPLETE}:
            return finish(result)
        if result["work_found"]:
            with span("confirm-quantities", as_type="chain", input=result) as observation:
                confirmed = confirm_quantities(result)
                observation.update(output=confirmed)
            return finish(confirmed)
    reason = "Работа не найдена в отобранных фрагментах; это не доказывает ее отсутствие в PDF."
    if requests >= max_requests or characters >= max_total_chars:
        reason += " Исчерпан бюджет поиска на позицию."
    reason += f" Проверено контекстов: {requests}; страниц с фрагментами: {len(visited)} из {len(index.texts)}."
    if index.pending_ocr:
        reason += f" Не распознано сканов: {len(index.pending_ocr)}."
    if failures:
        reason += " Сбои чтения: " + "; ".join(f"стр. {i + 1}: {error}" for i, error in failures.items())
    return finish(empty_result(TECHNICAL_ERROR if failures else INCOMPLETE, reason))
