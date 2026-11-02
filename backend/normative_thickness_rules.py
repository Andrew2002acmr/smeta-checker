"""Literal norm thickness and adjustment references, without applicability inference."""

import re
from decimal import Decimal
from typing import TypedDict


class ThicknessLiteral(TypedDict):
    value: Decimal
    quote: str


class AdjustmentLiteral(TypedDict):
    step: Decimal
    step_quote: str
    reference_quote: str
    allow_positive: bool
    allow_negative: bool


CODE = r"\d{2}-\d{2}-\d{3}-\d{2,3}"
THICKNESS = re.compile(
    r"(?:толщин[а-яё]*(?:\s+сло[яй])?|слоем)\s+(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b", re.I
)
STEP = re.compile(
    r"(?:на\s+кажд[а-яё]*\s+(?P<a>\d+(?:[.,]\d+)?)\s*(?P<au>мм|см|м)\s+изменени[а-яё]*\s+толщин[а-яё]*"
    r"|при\s+изменени[а-яё]*\s+толщин[а-яё]*\s+"
    r"(?:сло[яй](?:\s+(?:покрыти[яй]|основани[яй]))?|покрыти[яй]|основани[яй])"
    r"\s+на\s+(?P<b>\d+(?:[.,]\d+)?)\s*(?P<bu>мм|см|м)\b)",
    re.I,
)


def definition_text(text):
    return str(text).split("Состав работ:", 1)[0]


def source_definition(source):
    label = source.get("label", "")
    codes = set(re.findall(CODE, label))
    family = re.search(r"\b(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\b", label, re.I)
    origin = source.get("origin", "")
    if (
        source.get("kind") != "normative"
        or len(codes) != 1
        or not family
        or not source.get("source_id")
        or not origin
        or origin.startswith("ocr")
        or origin in {"uncertain_table", "native_definition_ambiguous"}
        or "ресурсы" in label.casefold()
    ):
        return None
    code = next(iter(codes))
    if code not in source.get("text", ""):
        return None
    return dict(
        code=code,
        table_code=code.rsplit("-", 1)[0],
        source_id=source["source_id"],
        family=family[0].casefold(),
        text=source["text"],
        version_id=source.get("version_id"),
    )


def base_thickness(text) -> ThicknessLiteral | None:
    from normative_parameters import measure

    matches = list(THICKNESS.finditer(definition_text(text)))
    values = {measure(m[1], m[2].lower()) for m in matches}
    if len(values) != 1 or None in values:
        return None
    value = next(iter(values))
    if not value or value[0] != "length" or value[1] <= 0:
        return None
    return ThicknessLiteral(value=value[1], quote=matches[0][0])


def adjustment_rule(item, base) -> AdjustmentLiteral | None:
    """Same PDF/family and an explicit base code/range, including other tables."""
    from normative_parameters import clean, measure

    if (
        not item.get("source_id")
        or item["source_id"] != base.get("source_id")
        or not item.get("family")
        or str(item["family"]).casefold() != str(base.get("family", "")).casefold()
        or item.get("version_id") != base.get("version_id")
        or any(
            not re.fullmatch(CODE, str(v.get("code", "")))
            or v.get("table_code") != v["code"].rsplit("-", 1)[0]
            for v in (item, base)
        )
        or item["code"] == base["code"]
    ):
        return None
    text = definition_text(item.get("text", ""))
    steps = list(STEP.finditer(text))
    if len(steps) != 1:
        return None
    step = steps[0]
    amount = measure(step["a"] or step["b"], (step["au"] or step["bu"]).lower())
    if not amount or amount[1] <= 0:
        return None
    literal = re.sub(r"[‐‑‒–—−]", "-", clean(text))
    reference = re.search(r"\bк\s+норм[а-яё]*\s+(.{1,600})", literal)
    positive = bool(re.search(r"\bдобавлять\b", literal))
    negative = bool(re.search(r"\bисключать\b", literal))
    if (
        not reference
        or not (positive or negative)
        or re.search(
            r"\b(?:не|нельзя|запрещено|запрещается|если|кроме|исключая)\b|за\s+исключением|при\s+условии",
            literal,
        )
        or len(re.findall(r"\bпри\b", literal)) != (1 if step["b"] else 0)
    ):
        return None
    tail = reference[1]
    code = base["code"]
    linked = False
    for lower, upper in re.findall(
        r"(" + CODE + r")\s*(?:по|-)\s*(" + CODE + r")", tail
    ):
        if lower.rsplit("-", 1)[0] == upper.rsplit("-", 1)[0] == base["table_code"]:
            linked |= (
                int(lower.rsplit("-", 1)[1])
                <= int(code.rsplit("-", 1)[1])
                <= int(upper.rsplit("-", 1)[1])
            )
    linked |= code in re.findall(r"\b" + CODE + r"\b", tail)
    if not linked:
        return None
    return AdjustmentLiteral(
        step=amount[1],
        step_quote=step[0],
        reference_quote=text,
        allow_positive=positive,
        allow_negative=negative,
    )


def literal_measure(source, code):
    """Resolve an unambiguous measure, or an explicitly code-qualified measure."""
    from normative_parameters import measure

    text = definition_text(source.get("text", ""))
    lines = re.findall(r"Измеритель\s*:\s*([^\n]+)", text, re.I)
    if len(lines) != 1:
        return None
    parts = lines[0].split(";")
    if len(parts) > 1:
        parts = [
            p
            for p in parts
            if re.search(r"\(норма\s+" + re.escape(code) + r"\)", p, re.I)
        ]
    if len(parts) != 1:
        return None
    match = re.match(
        r"\s*(\d+(?:[.,]\d+)?)\s*(м[23²³]?|шт\.?|т|кг|га)\b", parts[0], re.I
    )
    return measure(match[1], match[2]) if match else None
