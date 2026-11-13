"""Workbook self-consistency: value / formula / display-text checks.

Pure layer over the parsed ``vor_cells`` snapshot: compares the claimed
volume against the evaluated D-expression and the human-visible E-text.
No LLM, no PDF access; every finding names concrete cell addresses.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from audit_measurements import _numbers, close_amounts, format_ru

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_ADDRESS_RE = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_PRODUCT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*[×x*]\s*(\d+(?:[.,]\d+)?)\s*$")

# English -> Russian Excel function names for display, after
# https://inner.su/articles/tablitsa-funktsiy-excel-russkiy-i-angliyskiy/
# The evaluator only understands ROUND; unknown names pass through unchanged.
_RU_FUNCTIONS = {
    # Поиск и ссылки
    "VLOOKUP": "ВПР",
    "HLOOKUP": "ГПР",
    "XLOOKUP": "ПРОСМОТРX",
    "XMATCH": "ПОИСКПОЗX",
    "LOOKUP": "ПРОСМОТР",
    "INDEX": "ИНДЕКС",
    "MATCH": "ПОИСКПОЗ",
    "OFFSET": "СМЕЩ",
    "INDIRECT": "ДВССЫЛ",
    "CHOOSE": "ВЫБОР",
    "ROW": "СТРОКА",
    "COLUMN": "СТОЛБЕЦ",
    "TRANSPOSE": "ТРАНСП",
    "FILTER": "ФИЛЬТР",
    "SORT": "СОРТ",
    "SORTBY": "СОРТПО",
    "UNIQUE": "УНИК",
    # Логические
    "IF": "ЕСЛИ",
    "IFS": "ЕСЛИМН",
    "IFERROR": "ЕСЛИОШИБКА",
    "IFNA": "ЕСНД",
    "AND": "И",
    "OR": "ИЛИ",
    "NOT": "НЕ",
    "SWITCH": "ПЕРЕКЛЮЧ",
    "TRUE": "ИСТИНА",
    "FALSE": "ЛОЖЬ",
    # Математические
    "SUM": "СУММ",
    "SUMIF": "СУММЕСЛИ",
    "SUMIFS": "СУММЕСЛИМН",
    "SUMPRODUCT": "СУММПРОИЗВ",
    "PRODUCT": "ПРОИЗВЕД",
    "ROUND": "ОКРУГЛ",
    "ROUNDUP": "ОКРУГЛВВЕРХ",
    "ROUNDDOWN": "ОКРУГЛВНИЗ",
    "INT": "ЦЕЛОЕ",
    "MOD": "ОСТАТ",
    "POWER": "СТЕПЕНЬ",
    "SQRT": "КОРЕНЬ",
    "ABS": "ABS",
    # Статистические
    "AVERAGE": "СРЗНАЧ",
    "AVERAGEIF": "СРЗНАЧЕСЛИ",
    "AVERAGEIFS": "СРЗНАЧЕСЛИМН",
    "COUNT": "СЧЁТ",
    "COUNTA": "СЧЁТЗ",
    "COUNTIF": "СЧЁТЕСЛИ",
    "COUNTIFS": "СЧЁТЕСЛИМН",
    "COUNTBLANK": "СЧИТАТЬПУСТОТЫ",
    "MAX": "МАКС",
    "MIN": "МИН",
    "MEDIAN": "МЕДИАНА",
    "MAXIFS": "МАКСЕСЛИ",
    "MINIFS": "МИНЕСЛИ",
    # Текстовые
    "CONCAT": "СЦЕП",
    "CONCATENATE": "СЦЕПИТЬ",
    "TEXTJOIN": "ОБЪЕДИНИТЬ",
    "LEFT": "ЛЕВСИМВ",
    "RIGHT": "ПРАВСИМВ",
    "MID": "ПСТР",
    "LEN": "ДЛСТР",
    "TRIM": "СЖПРОБЕЛЫ",
    "FIND": "НАЙТИ",
    "SEARCH": "ПОИСК",
    "SUBSTITUTE": "ПОДСТАВИТЬ",
    "REPLACE": "ЗАМЕНИТЬ",
    "TEXT": "ТЕКСТ",
    "VALUE": "ЗНАЧЕН",
    "UPPER": "ПРОПИСН",
    "LOWER": "СТРОЧН",
    "PROPER": "ПРОПНАЧ",
    # Дата и время
    "TODAY": "СЕГОДНЯ",
    "NOW": "ТДАТА",
    "DATE": "ДАТА",
    "YEAR": "ГОД",
    "MONTH": "МЕСЯЦ",
    "DAY": "ДЕНЬ",
    "HOUR": "ЧАС",
    "MINUTE": "МИНУТЫ",
    "SECOND": "СЕКУНДЫ",
    "WEEKDAY": "ДЕНЬНЕД",
    "DATEDIF": "РАЗНДАТ",
    "WORKDAY": "РАБДЕНЬ",
    "NETWORKDAYS": "ЧИСТРАБДНИ",
    "EOMONTH": "КОНМЕСЯЦА",
    "EDATE": "ДАТАМЕС",
}


def ru_formula(expression):
    """Render a stored formula the way Russian Excel shows it.

    ``=ROUND(D18*0.44,0)`` becomes ``=ОКРУГЛ(D18*0,44;0)``: function
    names translated, decimal points to commas, argument commas to
    semicolons. Addresses stay untouched.
    """
    text = str(expression or "")
    if not text:
        return ""
    for name, ru in _RU_FUNCTIONS.items():
        text = re.sub(rf"\b{name}\b", ru, text, flags=re.IGNORECASE)
    out: list[str] = []
    depth = 0
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            # Excel escapes a quote by doubling it ("").
            if in_string and text[index + 1 : index + 2] == '"':
                out.append('""')
                index += 2
                continue
            in_string = not in_string
            out.append(char)
        elif in_string:
            out.append(char)
        elif char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            out.append(char)
        elif char == "," and depth > 0:
            out.append(";")
        elif char == ".":
            out.append(",")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _to_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ".").replace(" ", ""))
    except InvalidOperation, ValueError, AttributeError:
        return None


def _display_numbers(cached: Any) -> set[Decimal]:
    if not isinstance(cached, str):
        return set()
    return set(_numbers(cached))


def _addresses_in_display(formula: str | None) -> list[str]:
    if not isinstance(formula, str) or not formula.startswith("="):
        return []
    return [f"{col.upper()}{row}" for col, row in _ADDRESS_RE.findall(formula)]


def check_display_consistency(
    calculation: Mapping[str, Any] | None,
    formula_text: str,
    formula_refs: Mapping[str, Any] | None,
    cells: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compare D-formula operands against the E display text.

    The D-expression computes the claimed volume; the E-text is what the
    estimator reads. Each number in the E cached text must match either a
    D dependency value or a D literal; each ``=``-referenced cell in the
    E display formula must be a D dependency (or evaluate equal to one).
    When both sides diverge at once it is one disease ("shown arithmetic
    does not match the quantity formula"), reported as a single finding
    with both arithmetics; lone divergences keep their own messages.
    Returns findings with cell addresses, never raises on bad input.
    """
    findings: list[dict[str, Any]] = []
    number_hits: list[dict[str, Any]] = []
    cell_hits: list[dict[str, Any]] = []
    calc = calculation if isinstance(calculation, Mapping) else {}
    if calc.get("status") != "calculated":
        return findings
    display_cell = (formula_refs or {}).get("display_cell", "")
    display_formula = (formula_refs or {}).get("display_formula")
    entry = (cells or {}).get(display_cell, {}) if display_cell else {}
    cached = entry.get("cached", formula_text)
    if cached is None:
        cached = formula_text
    shown = _display_numbers(cached if isinstance(cached, str) else formula_text)
    if not shown:
        return findings
    known: dict[Decimal, str] = {}
    for dep in calc.get("dependencies", []) or []:
        if not isinstance(dep, Mapping):
            continue
        value = _to_decimal(str(dep.get("value")))
        if value is None:
            continue
        label = dep.get("cell") or dep.get("expression") or "literal"
        known.setdefault(value, str(label))
    claimed = _to_decimal(str(calc.get("value")))
    if claimed is not None:
        known.setdefault(claimed, str(calc.get("cell") or "claimed"))

    def _matches(value: Decimal) -> str | None:
        for candidate, label in known.items():
            if close_amounts(candidate, value):
                return label
        return None

    for value in sorted(shown):
        if _matches(value) is None:
            number_hits.append(
                {
                    "kind": "display-operand-mismatch",
                    "display_cell": display_cell,
                    "value": format(value, "f"),
                    "message": (
                        f"в тексте расчёта {display_cell or 'E'} показано число "
                        f"{format(value, 'f').replace('.', ',')}, которого нет среди операндов "
                        f"формулы {ru_formula(calc.get('expression', ''))}"
                    ),
                }
            )
    for address in _addresses_in_display(
        display_formula if isinstance(display_formula, str) else None
    ):
        ref = (cells or {}).get(address, {})
        ref_formula = ref.get("formula")
        ref_value = _to_decimal(
            str(
                ref.get("cached") if ref.get("cached") is not None else ref.get("plain")
            )
        )
        dep_cells = {
            str(dep.get("cell"))
            for dep in calc.get("dependencies", []) or []
            if isinstance(dep, Mapping) and dep.get("cell")
        }
        if address in dep_cells:
            continue
        if ref_value is not None and _matches(ref_value) is not None:
            continue
        cell_hits.append(
            {
                "kind": "display-cell-not-operand",
                "display_cell": display_cell,
                "value": address,
                "message": (
                    f"текст расчёта {display_cell or 'E'} ссылается на {address}"
                    f"{f' ({ru_formula(ref_formula)})' if ref_formula else ''}, "
                    f"но формула количества {calc.get('cell', '')} "
                    "эту ячейку не использует"
                ),
            }
        )
    if number_hits and cell_hits:
        merged = _merge_display_divergence(
            display_cell, str(calc.get("cell", "")), calc,
            cached if isinstance(cached, str) else formula_text,
            number_hits, cell_hits)
        if merged is not None:
            return [merged]
    findings.extend(number_hits)
    findings.extend(cell_hits)
    return findings


def _display_product(text: str) -> tuple[str, str, Decimal] | None:
    """Split a shown ``a*b`` product; anything else is not arithmetic.

    Operands come back as written (631,2 not 631,20); only the product
    is computed.
    """
    match = _PRODUCT_RE.match(text or "")
    if not match:
        return None
    try:
        value = (Decimal(match.group(1).replace(",", "."))
                 * Decimal(match.group(2).replace(",", ".")))
    except Exception:
        return None
    return match.group(1), match.group(2), value


def _short_ru(value: Decimal) -> str:
    """format_ru without forced trailing zeros (278 not 278,00)."""
    text = format_ru(value)
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    return text


def _merge_display_divergence(display_cell: str, quantity_cell: str,
                              calc: Mapping[str, Any], shown_text: str,
                              number_hits: list[dict[str, Any]],
                              cell_hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fuse number+cell hits into one verdict with both arithmetics.

    Returns None when the shown text is not a plain product, letting the
    caller fall back to the two separate messages.
    """
    product = _display_product(shown_text)
    if product is None:
        return None
    first, second, shown_value = product
    claimed = _to_decimal(str(calc.get("value")))
    if claimed is None:
        return None
    sources = ", ".join(hit["value"] for hit in cell_hits)
    return {
        "kind": "display-text-diverges",
        "display_cell": display_cell,
        "value": format(shown_value, "f"),
        "message": (
            f"расчёт {display_cell or 'E'} не сходится с количеством "
            f"{quantity_cell or 'D'}: в тексте {first} × "
            f"{second} = {_short_ru(shown_value)} (из {sources}), "
            f"а формула {ru_formula(calc.get('expression', ''))} даёт "
            f"{_short_ru(claimed)}"
        ),
    }


def check_scheme_divisors(positions) -> list[dict]:
    """Flag anchor-scheme divisor mismatches within one site section.

    Connection works reference an M-cell scheme divisor (M23/M35); the
    sibling забивные-анкер material divides by a hardcoded literal.
    When both exist for the same base (D22/D34 plate count) and the
    values differ, the choice of scheme needs engineering review.
    Structural: same base cell + M-ref vs literal divisor.
    """
    findings: list[dict] = []
    items = list(positions or [])
    for work in items:
        calc = getattr(work, "calculation", None) or {}
        expr = calc.get("expression") or ""
        if "/M" not in expr.replace(" ", "") and "M" not in expr:
            continue
        m_refs = sorted(set(_ADDRESS_RE.findall(expr)))
        m_cells = [f"{c.upper()}{r}" for c, r in m_refs if c.upper() == "M"]
        if not m_cells:
            continue
        base_cells = sorted(
            {
                str(d.get("cell"))
                for d in calc.get("dependencies", []) or []
                if isinstance(d, dict) and str(d.get("cell") or "").startswith("D")
            }
        )
        if not base_cells:
            continue
        m_value = None
        for dep in calc.get("dependencies", []) or []:
            if str(dep.get("cell") or "") in m_cells:
                m_value = _to_decimal(str(dep.get("value")))
                break
        if m_value is None:
            continue
        for other in items:
            if getattr(other, "index", -1) <= getattr(work, "index", -1):
                continue
            other_calc = getattr(other, "calculation", None) or {}
            other_expr = other_calc.get("expression") or ""
            if not other_expr or "M" in other_expr:
                continue
            other_base = {
                str(d.get("cell"))
                for d in other_calc.get("dependencies", []) or []
                if isinstance(d, dict) and str(d.get("cell") or "").startswith("D")
            }
            if not (set(base_cells) & other_base):
                continue
            divisors = set()
            for token in re.findall(r"/\s*(\d+(?:[.,]\d+)?)", other_expr):
                value = _to_decimal(token)
                if value is not None:
                    divisors.add(value)
            if m_value in divisors or not divisors:
                continue
            findings.append(
                {
                    "kind": "scheme-divisor-mismatch",
                    "cell": m_cells[0],
                    "value": format(m_value, "f"),
                    "position": getattr(work, "number", ""),
                    "other_position": getattr(other, "number", ""),
                    "message": (
                        f"позиция №{getattr(work, 'number', '')} считает по схеме "
                        f"{m_cells[0]} = {format(m_value, 'f').replace('.', ',')}, а позиция "
                        f"№{getattr(other, 'number', '')} делит на "
                        f"{', '.join(format(v, 'f').replace('.', ',') for v in sorted(divisors))}; "
                        "основание выбора схем стоит проверить"
                    ),
                }
            )
            break
    return findings


def check_length_operands(calculation, page_texts) -> list[dict]:
    """Flag L-operands whose value conflicts with related page texts.

    L-cells feed scheme divisors (M35 = f(L30, L35, L34)); a length like
    L35 = 172 that never appears on the related pages while a close
    number (171 м) does is a data inconsistency worth flagging.
    Only L-cells ≥ 10 are checked; small counts are too noisy.
    """
    findings: list[dict] = []
    calc = calculation if isinstance(calculation, dict) else {}
    if calc.get("status") != "calculated" or not page_texts:
        return findings
    for dep in calc.get("dependencies", []) or []:
        if not isinstance(dep, dict) or dep.get("origin") != "vor_cell":
            continue
        cell = str(dep.get("cell") or "")
        if not cell.startswith("L"):
            continue
        value = _to_decimal(str(dep.get("value")))
        if value is None or abs(value) < 10:
            continue
        token = format(value.normalize(), "f").replace(".", r"[.,]")
        if any(
            re.search(rf"(?<!\d){token}(?!\d)", str(t or ""))
            for t in page_texts.values()
        ):
            continue
        for page, text in page_texts.items():
            body = str(text or "")
            for match in _NUMBER_RE.finditer(body):
                other = _to_decimal(match.group(0))
                if other is None or other == value or abs(other) < 10:
                    continue
                if abs(other - value) / abs(value) > Decimal("0.05"):
                    continue
                after = body[match.end() : match.end() + 12]
                if not re.match(r"\s*м(?![²2³3])", after):
                    continue
                start = max(0, match.start() - 60)
                space = body.find(" ", start, match.start())
                if space >= 0:
                    start = space + 1
                raw = body[start : match.end() + 40]
                clean = re.sub(r"<[^>]+>", " ", raw)
                clean = re.sub(r"\s+", " ", clean).strip()
                words = clean.split(" ")
                snippet = " ".join(words[:16])
                findings.append(
                    {
                        "kind": "length-operand-conflicts",
                        "cell": cell,
                        "value": format(value, "f"),
                        "message": (
                            f"операнд {cell} = {format(value, 'f')} не встретился "
                            f"на связанных страницах, но на стр. {page} есть "
                            f"похожее число: «{snippet.strip()}»"
                        ),
                    }
                )
                break
            else:
                continue
            break
    return findings
