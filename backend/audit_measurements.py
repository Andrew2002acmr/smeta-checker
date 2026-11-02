"""Measurement parsing and comparison: pure Decimal/unit logic, no PDF/LLM."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_THOUSANDS_RE = re.compile(r"(?<!\w)(\d{1,3}(?:[ \u00a0\u2009]\d{3})+)(?!\w)")
_NUMBER_RE = re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?")
_MEASUREMENT_RE = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s+"
    r"(м3|м³|м2|м²|мм|см|км|м|mm|m|шт\.?|pcs|кг|т|н|кн|л/с|л|компл|пог\.?\s*м|"
    r"точк[аи]|пункт[а-я]*|наблюден(?:ие|ия|ий)|"
    r"г/м2|г/м²|кг/м2|кг/м²|м3/м2|м³/м²|кн/м)\b"
    r"|(?<![\w.,])(\d+(?:[.,]\d+)?)"
    r"(м3|м³|м2|м²|мм|см|км|шт\.?|pcs|кг|т|н|кн|л/с|л|компл|пог\.?\s*м|"
    r"г/м2|г/м²|кг/м2|кг/м²|м3/м2|м³/м²|кн/м)\b",
    re.IGNORECASE)
_UNIT_FIRST_RE = re.compile(
    r"\b(м3|м³|м2|м²|мм|см|км|шт\.?|pcs|кг|т|н|кн|л/с|л|компл|пог\.?\s*м|"
    r"точк[аи]|пункт[а-я]*|наблюден(?:ие|ия|ий)|"
    r"г/м2|г/м²|кг/м2|кг/м²|м3/м2|м³/м²|кн/м)\s*"
    r"(\d+(?:[.,]\d+)?)(?![\w.,])"
    r"|\b(м|mm|m)\s+(\d+(?:[.,]\d+)?)(?![\w.,])",
    re.IGNORECASE)
_OPERATOR_RES = (
    (re.compile(r"^\s*не\s+менее\b", re.IGNORECASE), "ge"),
    (re.compile(r"^\s*не\s+более\b", re.IGNORECASE), "le"),
    (re.compile(r"^\s*(?:>=|≥)"), "ge"),
    (re.compile(r"^\s*(?:<=|≤)"), "le"),
)
_RANGE_RE = re.compile(
    r"(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*[–—-]\s*(\d+(?:[.,]\d+)?)(?![\w.,-])")
_DIRECTED_RE = re.compile(
    r"(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)(?![\w.,-])")
_VARIANT_RE = re.compile(
    r"(?<![\w.,])\d+\s*\(\s*\d+\s*\)\s*[–—-]\s*\d+(?![\w.,])")

_UNIT_ALIASES = {
    "мм": ("мм", "length"), "см": ("см", "length"), "м": ("м", "length"),
    "км": ("км", "length"),
    "mm": ("мм", "length"), "m": ("м", "length"),
    "см2": ("см2", "area"), "м2": ("м2", "area"),
    "см3": ("см3", "volume"), "м3": ("м3", "volume"), "л": ("л", "volume"),
    "л/с": ("л/с", "flow"),
    "г": ("г", "mass"), "кг": ("кг", "mass"), "т": ("т", "mass"),
    "шт": ("шт", "count"), "pcs": ("шт", "count"), "компл": ("шт", "count"),
    "н": ("н", "force"), "кн": ("кн", "force"),
    "г/м2": ("г/м2", "areal_density"), "кг/м2": ("кг/м2", "areal_density"),
    "м3/м2": ("м3/м2", "consumption"), "кн/м": ("кн/м", "line_load"),
    # Geophysics observation points: точка/пункт/наблюдение are one unit.
    "точка": ("точка", "observation"), "точки": ("точка", "observation"),
    "пункт": ("точка", "observation"),
    "наблюдение": ("точка", "observation"),
    "наблюдения": ("точка", "observation"),
    "наблюдений": ("точка", "observation"),
}
_UNIT_FACTORS = {
    ("мм", "length"): (Decimal("0.001"), "м"),
    ("см", "length"): (Decimal("0.01"), "м"),
    ("м", "length"): (Decimal(1), "м"),
    ("км", "length"): (Decimal(1000), "м"),
    ("см2", "area"): (Decimal("0.0001"), "м2"),
    ("м2", "area"): (Decimal(1), "м2"),
    ("см3", "volume"): (Decimal("0.000001"), "м3"),
    ("м3", "volume"): (Decimal(1), "м3"),
    ("л", "volume"): (Decimal("0.001"), "м3"),
    ("л/с", "flow"): (Decimal(1), "л/с"),
    ("г", "mass"): (Decimal("0.001"), "кг"),
    ("кг", "mass"): (Decimal(1), "кг"),
    ("т", "mass"): (Decimal(1000), "кг"),
    ("шт", "count"): (Decimal(1), "шт"),
    ("н", "force"): (Decimal(1), "н"),
    ("кн", "force"): (Decimal(1000), "н"),
    ("г/м2", "areal_density"): (Decimal("0.001"), "кг/м2"),
    ("кг/м2", "areal_density"): (Decimal(1), "кг/м2"),
    ("м3/м2", "consumption"): (Decimal(1), "м3/м2"),
    ("кн/м", "line_load"): (Decimal(1), "кн/м"),
    ("точка", "observation"): (Decimal(1), "точка"),
}
_KIND_DIMENSIONS = {
    "item_count": {"count"},
    "coverage_area": {"area"},
    "areal_consumption": {"consumption", "areal_density", "line_load"},
    "supplied_need": {"count", "mass", "volume", "area", "length"},
    "geometric_envelope": {"volume", "area"},
    "material_volume": {"volume"},
    "measured_work": {"volume", "area", "length", "mass", "count", "flow", "observation"},
}
_AMOUNT_TOLERANCE = Decimal("1e-9")


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


def normalize_unit_text(text: str) -> str:
    """Canonical unit spelling without touching adjacent numbers."""
    cleaned = unicodedata.normalize("NFKC", str(text or ""))
    cleaned = re.sub(r"[.\s]+$", "", cleaned.strip().lower().replace("ё", "е"))
    cleaned = cleaned.replace("²", "2").replace("³", "3")
    cleaned = re.sub(r"\bм\s+([23])\b", r"м\1", cleaned)
    cleaned = re.sub(r"\bпог\s*м\b", "пог. м", cleaned)
    cleaned = re.sub(r"\bпог\.?\s*м\b", "м", cleaned)
    # "точка наблюдения/наблюдений" is the same observation unit.
    cleaned = re.sub(r"^точк[аи]\s+наблюдени[йя]\s*$", "точка", cleaned)
    cleaned = re.sub(r"^пункт[а-я]*\s+наблюдени[йя]\s*$", "точка", cleaned)
    if re.fullmatch(r"пункт[а-я]*", cleaned):
        return "пункт"
    return cleaned


def canonical_unit(text: str) -> tuple[str, str] | None:
    """Return (canonical unit, dimension) for the closed supported set."""
    normalized = normalize_unit_text(text)
    return _UNIT_ALIASES.get(normalized)


def paired_units(text: str) -> tuple[tuple[str, str], ...]:
    """Ordered alternative units, not a physical rate such as кг/м2 or л/с."""
    if canonical_unit(text) is not None:
        return ()
    parts = re.split(r"\s*/\s*", str(text).strip())
    if not 2 <= len(parts) <= 3:
        return ()
    units = tuple(canonical_unit(part) for part in parts)
    return units if all(units) else ()


def paired_measurements(values: str, units: str) -> list["Measurement"]:
    """Zip explicitly ordered value/unit cells without choosing by expected value."""
    labels = paired_units(units)
    parts = re.split(r"\s*/\s*", _squeeze_thousands(str(values).strip()))
    if not labels or len(parts) != len(labels) or any(
            re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", part.strip()) is None for part in parts):
        return []
    return [Measurement(raw=f"{value} {unit}", status="exact", value=_decimal(value.strip()),
                        operator="eq", unit=unit, dimension=dimension)
            for value, (unit, dimension) in zip(parts, labels)]


_PAIR_UNIT_ATOM = r"(?:м[23²³]|мм|см|км|шт\.?|pcs|компл\.?|кг|м|т)"
_PAIR_NUMBER_ATOM = r"[+-]?\d+(?:[.,]\d+)?"
_PAIR_UNIT_RE = re.compile(
    rf"(?<!\w)({_PAIR_UNIT_ATOM}\s*/\s*{_PAIR_UNIT_ATOM}(?:\s*/\s*{_PAIR_UNIT_ATOM})?)(?!\w)", re.I)
_PAIR_VALUES = rf"{_PAIR_NUMBER_ATOM}\s*/\s*{_PAIR_NUMBER_ATOM}(?:\s*/\s*{_PAIR_NUMBER_ATOM})?"


def has_paired_units(text: str) -> bool:
    return any(paired_units(m.group(1)) for m in _PAIR_UNIT_RE.finditer(str(text)))


def iter_paired_measurements(text: str) -> list["Measurement"]:
    """Recognize same-row шт./т | 114/23,53 and inline paired schedules.

    A slash in a known dimensional unit remains a rate. Missing units or
    unequal component counts never manufacture a numeric fact.
    """
    normalized = _squeeze_thousands(unicodedata.normalize("NFKC", str(text)))
    cells = [part.strip() for part in normalized.split("|")]
    found = []
    if len(cells) > 1:
        for index, cell in enumerate(cells):
            if not paired_units(cell):
                continue
            for neighbour in (index + 1, index - 1):
                if 0 <= neighbour < len(cells):
                    found.extend(paired_measurements(cells[neighbour], cell))
        if found:
            return found
    for match in _PAIR_UNIT_RE.finditer(normalized):
        units = match.group(1)
        if not paired_units(units):
            continue
        after = re.match(rf"\s*[:=]?\s*({_PAIR_VALUES})(?![\w/.,])", normalized[match.end():])
        before = re.search(rf"(?<![\w/.,])({_PAIR_VALUES})\s*$", normalized[:match.start()])
        if after:
            found.extend(paired_measurements(after.group(1), units))
        elif before:
            found.extend(paired_measurements(before.group(1), units))
    if found:
        return found
    # Inline dual values: 114 шт./23,53 т. Require the slash to be the
    # entire separator between two independently dimensioned measurements.
    atoms = iter_measurements(normalized)
    for first, second in zip(atoms, atoms[1:]):
        start = normalized.find(first.raw)
        end = normalized.find(second.raw, start + len(first.raw))
        if start >= 0 and end >= 0 and re.fullmatch(r"[.\s]*/\s*", normalized[start + len(first.raw):end]):
            found.extend((first, second))
    return found


def convert_to_base(value: Decimal, canonical: str, dimension: str) -> Decimal:
    factor, _ = _UNIT_FACTORS[(canonical, dimension)]
    return value * factor


def dimension_allows_kind(dimension: str, kind: str) -> bool:
    if not kind:
        return True
    allowed = _KIND_DIMENSIONS.get(kind)
    if allowed is None:
        return False
    return dimension in allowed


def close_amounts(expected: Decimal, actual: Decimal) -> bool:
    base = max(abs(expected), abs(actual), Decimal(1))
    return abs(expected - actual) <= base * _AMOUNT_TOLERANCE


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(_squeeze_thousands(unicodedata.normalize("NFKC", token)).replace(",", "."))
    except Exception:
        return None


def parse_measurement(
    text: str, *, unit: str = "", kind: str = "", property_label: str = ""
) -> "Measurement":
    """Parse one VOR/PD condition, preserving number/unit/operator/kind roles."""
    raw = unicodedata.normalize("NFKC", str(text or ""))
    unit_text = unicodedata.normalize("NFKC", str(unit or ""))
    measurement = raw if raw.strip() else unit_text
    measurement = measurement.strip()
    if not measurement:
        return Measurement(raw=raw, status="unknown", reason="missing_value")
    if _VARIANT_RE.search(measurement):
        return Measurement(raw=raw, status="variant", reason="variant_designation")
    directed = _DIRECTED_RE.search(measurement)
    unit_hit = canonical_unit(unit_text or measurement)
    if directed and not unit_hit:
        first = _decimal(directed.group(1))
        second = _decimal(directed.group(2))
        if first is not None and second is not None:
            parts = (
                Measurement(raw=raw, status="exact", value=first, operator="eq",
                            unit="", dimension="", kind=kind,
                            property_label=property_label or "directed/1"),
                Measurement(raw=raw, status="exact", value=second, operator="eq",
                            unit="", dimension="", kind=kind,
                            property_label=property_label or "directed/2"),
            )
            return Measurement(raw=raw, status="directed_pair", reason="directed_pair",
                               parts=parts, kind=kind,
                               property_label=property_label)
    operator = "eq"
    remainder = measurement
    for pattern, found in _OPERATOR_RES:
        if pattern.match(measurement):
            operator = found
            remainder = pattern.sub("", measurement, count=1)
            break
    interval = _RANGE_RE.search(remainder)
    if interval and operator == "eq":
        low = _decimal(interval.group(1))
        high = _decimal(interval.group(2))
        if low is not None and high is not None:
            unit_hit = canonical_unit(remainder[interval.end():] or unit_text)
            if unit_hit is None:
                return Measurement(raw=raw, status="unknown",
                                   reason="unsupported_unit", kind=kind,
                                   property_label=property_label)
            canonical, dimension = unit_hit
            if not dimension_allows_kind(dimension, kind):
                return Measurement(raw=raw, status="unknown",
                                   reason="incompatible_quantity_kind", unit=canonical,
                                   dimension=dimension, kind=kind,
                                   property_label=property_label)
            low, high = (low, high) if low <= high else (high, low)
            return Measurement(raw=raw, status="range", operator="range",
                               lower=convert_to_base(low, canonical, dimension),
                               upper=convert_to_base(high, canonical, dimension),
                               unit=canonical, dimension=dimension, kind=kind,
                               property_label=property_label)
    tokens = _NUMBER_RE.findall(_squeeze_thousands(remainder))
    if not tokens:
        return Measurement(raw=raw, status="unknown", reason="missing_value",
                           kind=kind, property_label=property_label)
    if len(tokens) != 1:
        return Measurement(raw=raw, status="unknown",
                           reason="ambiguous_numbers", kind=kind,
                           property_label=property_label)
    value = _decimal(tokens[0])
    if value is None:
        return Measurement(raw=raw, status="unknown", reason="unsupported_number",
                           kind=kind, property_label=property_label)
    unit_hit = canonical_unit(remainder) or canonical_unit(unit_text)
    if unit_text and canonical_unit(unit_text) is None:
        return Measurement(raw=raw, status="unknown",
                           reason="unsupported_unit", kind=kind,
                           property_label=property_label)
    if unit_hit is None:
        if remainder.strip() != tokens[0].strip():
            return Measurement(raw=raw, status="unknown",
                               reason="unsupported_unit", kind=kind,
                               property_label=property_label)
        return Measurement(raw=raw, status="exact", value=value,
                           operator=operator, kind=kind,
                           property_label=property_label)
    canonical, dimension = unit_hit
    if not dimension_allows_kind(dimension, kind):
        return Measurement(raw=raw, status="unknown",
                           reason="incompatible_quantity_kind", unit=canonical,
                           dimension=dimension, kind=kind,
                           property_label=property_label)
    return Measurement(raw=raw, status="exact", value=convert_to_base(value, canonical, dimension),
                       operator=operator, unit=canonical, dimension=dimension,
                       kind=kind, property_label=property_label)


def iter_measurements(text: str) -> list["Measurement"]:
    """Atomic number+unit pairs in extraction order, without set semantics."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    squeezed = _squeeze_thousands(normalized)
    hits: list[tuple[int, "Measurement"]] = []
    for match in _MEASUREMENT_RE.finditer(squeezed):
        number = match.group(1) if match.group(1) is not None else match.group(3)
        unit_text = match.group(2) if match.group(2) is not None else match.group(4)
        unit_hit = canonical_unit(unit_text or "")
        value = _decimal(number or "")
        if unit_hit is None or value is None:
            continue
        canonical, dimension = unit_hit
        hits.append((match.start(), Measurement(
            raw=match.group(0), status="exact", value=value,
            operator="eq", unit=canonical, dimension=dimension)))
    for match in _UNIT_FIRST_RE.finditer(squeezed):
        unit_text = match.group(1) if match.group(1) is not None else match.group(3)
        number = match.group(2) if match.group(2) is not None else match.group(4)
        unit_hit = canonical_unit(unit_text or "")
        value = _decimal(number or "")
        if unit_hit is None or value is None:
            continue
        if any(start <= match.start() < start + len(raw)
               for start, raw in ((item[0], item[1].raw) for item in hits)):
            continue
        canonical, dimension = unit_hit
        hits.append((match.start(), Measurement(
            raw=match.group(0), status="exact", value=value,
            operator="eq", unit=canonical, dimension=dimension)))
    hits.sort(key=lambda item: item[0])
    return [item for _, item in hits]


@dataclass(frozen=True)
class Measurement:
    raw: str
    status: str
    value: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    operator: str = "eq"
    unit: str = ""
    dimension: str = ""
    kind: str = ""
    property_label: str = ""
    reason: str = ""
    parts: tuple["Measurement", ...] = ()


def compare_measurements(expected: Measurement, actual: Measurement) -> dict[str, Any]:
    """Compare one expected condition against one actual fact.

    Returns {state, reason}; unknown inputs stay explicit and never match.
    """
    if expected.status == "variant" or actual.status == "variant":
        return {"state": "review_required", "reason": "variant_designation"}
    if expected.status == "directed_pair" or actual.status == "directed_pair":
        expected_parts = expected.parts or (expected,)
        actual_parts = actual.parts or (actual,)
        if len(actual_parts) == 2 and len(expected_parts) == 1:
            # Points/measurements pair (9/45): parts carry no unit, so
            # compare by value. A second-part hit means the VOR number
            # counts measurements, not points — needs estimator review.
            from decimal import Decimal as _Decimal
            expected_value = getattr(expected_parts[0], "value", None)
            hits = [
                pos for pos, part in enumerate(actual_parts)
                if isinstance(getattr(part, "value", None), _Decimal)
                and isinstance(expected_value, _Decimal)
                and close_amounts(expected_value, part.value)
            ]
            if hits == [0]:
                return {"state": "confirmed", "reason": "directed_pair_equal"}
            if hits == [1]:
                return {"state": "review_required",
                        "reason": "directed_pair_second_matches"}
            if set(hits) == {0, 1}:
                return {"state": "confirmed", "reason": "directed_pair_equal"}
            return {"state": "contradiction", "reason": "value_mismatch"}
        if len(expected_parts) != 2 or len(actual_parts) != 2:
            return {"state": "review_required", "reason": "directed_pair_mismatch"}
        states = [compare_measurements(item_expected, item_actual)["state"]
                  for item_expected, item_actual in zip(expected_parts, actual_parts)]
        if all(state == "confirmed" for state in states):
            return {"state": "confirmed", "reason": "directed_pair_equal"}
        if "contradiction" in states:
            return {"state": "contradiction", "reason": "directed_pair_mismatch"}
        return {"state": "review_required", "reason": "directed_pair_mismatch"}
    if expected.status == "unknown" or actual.status == "unknown":
        reason = expected.reason or actual.reason or "unknown_measurement"
        if reason == "incompatible_quantity_kind":
            return {"state": "contradiction", "reason": reason}
        return {"state": "review_required", "reason": reason}
    if expected.dimension != actual.dimension:
        return {"state": "contradiction", "reason": "incompatible_unit"}
    if expected.kind and actual.kind and expected.kind != actual.kind:
        return {"state": "contradiction", "reason": "incompatible_quantity_kind"}
    if expected.status == "range" and actual.status == "range":
        assert expected.lower is not None and expected.upper is not None
        assert actual.lower is not None and actual.upper is not None
        if expected.lower <= actual.lower and actual.upper <= expected.upper:
            return {"state": "confirmed", "reason": "range_within_expected"}
        if actual.upper < expected.lower or actual.lower > expected.upper:
            return {"state": "contradiction", "reason": "range_disjoint"}
        return {"state": "review_required", "reason": "range_partial_overlap"}
    if actual.status == "range":
        assert actual.lower is not None and actual.upper is not None
        if expected.operator == "eq":
            assert expected.value is not None
            if actual.lower <= expected.value <= actual.upper:
                if close_amounts(actual.lower, actual.upper):
                    return {"state": "confirmed", "reason": "equal"}
                return {"state": "review_required", "reason": "range_partial_overlap"}
            return {"state": "contradiction", "reason": "range_disjoint"}
        if expected.operator == "ge":
            assert expected.value is not None
            if actual.lower >= expected.value or close_amounts(actual.lower, expected.value):
                return {"state": "confirmed", "reason": "minimum_satisfied"}
            if actual.upper < expected.value:
                return {"state": "contradiction", "reason": "minimum_violated"}
            return {"state": "review_required", "reason": "range_partial_overlap"}
        assert expected.value is not None
        if actual.upper <= expected.value or close_amounts(actual.upper, expected.value):
            return {"state": "confirmed", "reason": "maximum_satisfied"}
        if actual.lower > expected.value:
            return {"state": "contradiction", "reason": "maximum_violated"}
        return {"state": "review_required", "reason": "range_partial_overlap"}
    assert actual.value is not None
    if expected.status == "range":
        assert expected.lower is not None and expected.upper is not None
        if expected.lower <= actual.value <= expected.upper:
            return {"state": "confirmed", "reason": "range_within_expected"}
        return {"state": "contradiction", "reason": "range_disjoint"}
    assert expected.value is not None
    if expected.operator == "eq":
        if close_amounts(expected.value, actual.value):
            return {"state": "confirmed", "reason": "equal"}
        return {"state": "contradiction", "reason": "value_mismatch"}
    if expected.operator == "ge":
        if actual.value >= expected.value or close_amounts(actual.value, expected.value):
            return {"state": "confirmed", "reason": "minimum_satisfied"}
        return {"state": "contradiction", "reason": "minimum_violated"}
    if actual.value <= expected.value or close_amounts(actual.value, expected.value):
        return {"state": "confirmed", "reason": "maximum_satisfied"}
    return {"state": "contradiction", "reason": "maximum_violated"}
