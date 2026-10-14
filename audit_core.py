"""Page selection and audit orchestration, independent of Streamlit and PDF objects."""

import json
import math
import re
from collections import Counter


TECHNICAL_ERROR = "Технический сбой"


def technical_error(message):
    return {"status": TECHNICAL_ERROR, "found_volume": "-", "found_page": "-",
            "discrepancy_details": str(message)}


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
    def __init__(self, texts, errors=None):
        self.texts = tuple(texts)
        self.errors = dict(errors or {})
        self.terms = tuple(_terms(text) for text in self.texts)
        frequency = Counter(term for terms in self.terms for term in terms)
        self.weights = {term: math.log(1 + len(self.texts) / count) for term, count in frequency.items()}

    def candidates(self, work_name, comment, reference, volume, excluded=()):
        excluded = set(excluded)
        query = _terms(work_name) | _terms(comment)
        number = str(volume).replace(",", ".").removesuffix(".0")
        sections = re.findall(r"(?:подраздел|раздел)\s+(\d+(?:\.\d+)+)", reference.lower())
        ranked = []
        for idx, terms in enumerate(self.terms):
            if idx in excluded:
                continue
            score = sum(self.weights[t] for t in terms & query)
            page_numbers = {n.replace(",", ".").removesuffix(".0")
                            for n in re.findall(r"\d+(?:[.,]\d+)?", self.texts[idx])}
            if number in page_numbers:
                score += 0.5
            if any(section in self.texts[idx] for section in sections):
                score += 4
            ranked.append((score, idx))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [idx for _, idx in ranked]


def page_chunks(text, size=6000, overlap=600):
    """Send every part of a page, rather than silently truncating the request."""
    if size <= overlap or overlap < 0:
        raise ValueError("Invalid chunk size")
    for start in range(0, len(text), size - overlap):
        yield text[start:start + size]
        if start + size >= len(text):
            break


def parse_audit_response(raw, allowed_pages):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Модель вернула пустой ответ")
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    # raw_decode tolerates surrounding prose but never repairs broken JSON fields.
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("В ответе модели нет JSON")
    data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(data, dict):
        raise ValueError("Ответ модели должен быть JSON-объектом")
    required = {"status", "found_volume", "found_page", "discrepancy_details"}
    if not required <= data.keys():
        raise ValueError("В ответе модели отсутствуют обязательные поля")
    status = data["status"]
    if not isinstance(status, str):
        raise ValueError("Некорректный тип статуса в ответе модели")
    status = status.strip()
    if status.upper().translate(str.maketrans({"O": "О", "K": "К"})) == "ОК":
        data["status"] = "ОК"
    elif status.lower() == "ошибка":
        data["status"] = "Ошибка"
    else:
        raise ValueError("Неизвестный статус в ответе модели")
    for field in ("found_volume", "discrepancy_details"):
        value = data[field]
        if value is not None and not isinstance(value, (str, int, float)):
            raise ValueError(f"Некорректное поле {field}")
        data[field] = str(value).strip() if value is not None else ""
    data["found_volume"] = data["found_volume"] or "-"
    page = data["found_page"]
    if page is None or page == "-" or page == "":
        data["found_page"] = "-"
    elif isinstance(page, bool) or not re.fullmatch(r"\d+", str(page).strip()):
        raise ValueError("found_page должен содержать только номер страницы или '-'")
    else:
        page = int(str(page).strip())
        if page not in allowed_pages:
            raise ValueError(f"Модель указала стр. {page}, которой не было в запросе")
        data["found_page"] = str(page)
    if data["status"] == "ОК" and data["found_page"] == "-":
        raise ValueError("Статус ОК без подтвержденной страницы")
    return data


def audit_text_pages(index, target_indices, ask, work_name, volume, reference="", comment=""):
    """Only strings cross into API worker threads. ask returns a normalized result."""
    primary = list(dict.fromkeys(target_indices))
    candidates = index.candidates(work_name, comment, reference, volume, primary)
    checked, failures = [], []
    partial_result = None

    def finish(result):
        result = dict(result)
        if result["found_page"] != "-" and int(result["found_page"]) - 1 not in primary:
            result["status"] = "Ошибка"
            result["discrepancy_details"] = (
                f"Данные найдены на стр. {result['found_page']}, вне ссылки ВОР. "
                + result["discrepancy_details"])
        if failures:
            result["status"] = TECHNICAL_ERROR
            result["discrepancy_details"] += " Неполная проверка: " + "; ".join(failures)
        return result

    for idx in primary + candidates:
        text = index.texts[idx]
        if idx in index.errors:
            failures.append(f"Стр. {idx + 1}: {index.errors[idx]}")
        if not text.strip():
            continue
        checked.append(idx + 1)
        for chunk in page_chunks(text):
            result = ask(idx + 1, chunk)
            if result["status"] == TECHNICAL_ERROR:
                failures.append(f"Стр. {idx + 1}: {result['discrepancy_details']}")
                # Stop on an unavailable provider, but keep any work location
                # already established before this technical failure.
                return finish(partial_result) if partial_result else technical_error("; ".join(failures))
            if result["found_page"] != "-":
                if result["status"] == "ОК":
                    return finish(result)
                # A work name with a different volume is not the final answer:
                # continue until the claimed data is found or the PDF is exhausted.
                if partial_result is None:
                    partial_result = dict(result)
    if partial_result:
        partial_result["discrepancy_details"] += (
            f" Заявленные данные не подтверждены на доступных страницах PDF. "
            f"Проверено страниц с текстом: {len(checked)} из {len(index.texts)}.")
        return finish(partial_result)
    if failures:
        return technical_error("Неполная проверка. " + "; ".join(failures))
    return {"status": "Ошибка", "found_volume": "-", "found_page": "-",
            "discrepancy_details": (
                f"Работа не найдена после проверки всего PDF ({len(index.texts)} стр.). "
                f"Проверено страниц с текстом: {len(checked)}; остальные страницы не содержат текста.")}
