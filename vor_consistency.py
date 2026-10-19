"""Workbook self-consistency: value / formula / display-text checks.

Pure layer over the parsed ``vor_cells`` snapshot: compares the claimed
volume against the evaluated D-expression and the human-visible E-text.
No LLM, no PDF access; every finding names concrete cell addresses.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_ADDRESS_RE = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")


def _to_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _display_numbers(cached: Any) -> set[Decimal]:
    if not isinstance(cached, str):
        return set()
    out: set[Decimal] = set()
    for token in _NUMBER_RE.findall(cached):
        value = _to_decimal(token)
        if value is not None:
            out.add(value)
    return out


def _addresses_in_display(formula: str | None) -> list[str]:
    if not isinstance(formula, str) or not formula.startswith("="):
        return []
    return [
        f"{col.upper()}{row}"
        for col, row in _ADDRESS_RE.findall(formula)
    ]


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
    Returns findings with cell addresses, never raises on bad input.
    """
    findings: list[dict[str, Any]] = []
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
    tol_base = max([abs(v) for v in known] + [Decimal(1)])
    tol = tol_base * Decimal("1e-9")

    def _matches(value: Decimal) -> str | None:
        for candidate, label in known.items():
            if abs(candidate - value) <= tol:
                return label
        return None

    for value in sorted(shown):
        if _matches(value) is None:
            findings.append({
                "kind": "display-operand-mismatch",
                "display_cell": display_cell,
                "value": format(value, "f"),
                "message": (
                    f"в тексте расчёта {display_cell or 'E'} показано число "
                    f"{format(value, 'f')}, которого нет среди операндов "
                    f"формулы {calc.get('expression', '')}"
                ),
            })
    for address in _addresses_in_display(
            display_formula if isinstance(display_formula, str) else None):
        ref = (cells or {}).get(address, {})
        ref_formula = ref.get("formula")
        ref_value = _to_decimal(str(
            ref.get("cached") if ref.get("cached") is not None else ref.get("plain")))
        dep_cells = {
            str(dep.get("cell"))
            for dep in calc.get("dependencies", []) or []
            if isinstance(dep, Mapping) and dep.get("cell")
        }
        if address in dep_cells:
            continue
        if ref_value is not None and _matches(ref_value) is not None:
            continue
        findings.append({
            "kind": "display-cell-not-operand",
            "display_cell": display_cell,
            "value": address,
            "message": (
                f"текст расчёта {display_cell or 'E'} ссылается на {address}"
                f"{f' ({ref_formula})' if ref_formula else ''}, "
                f"но формула количества {calc.get('cell', '')} "
                "эту ячейку не использует"
            ),
        })
    return findings


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
        base_cells = sorted({
            str(d.get("cell")) for d in calc.get("dependencies", []) or []
            if isinstance(d, dict) and str(d.get("cell") or "").startswith("D")})
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
                str(d.get("cell")) for d in other_calc.get("dependencies", []) or []
                if isinstance(d, dict) and str(d.get("cell") or "").startswith("D")}
            if not (set(base_cells) & other_base):
                continue
            divisors = set()
            for token in re.findall(r"/\s*(\d+(?:[.,]\d+)?)", other_expr):
                value = _to_decimal(token)
                if value is not None:
                    divisors.add(value)
            if m_value in divisors or not divisors:
                continue
            findings.append({
                "kind": "scheme-divisor-mismatch",
                "cell": m_cells[0],
                "value": format(m_value, "f"),
                "position": getattr(work, "number", ""),
                "other_position": getattr(other, "number", ""),
                "message": (
                    f"позиция №{getattr(work, 'number', '')} считает по схеме "
                    f"{m_cells[0]} = {format(m_value, 'f')}, а позиция "
                    f"№{getattr(other, 'number', '')} делит на "
                    f"{', '.join(format(v, 'f') for v in sorted(divisors))}; "
                    "основание выбора схем стоит проверить"),
            })
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
        if any(re.search(rf"(?<!\d){token}(?!\d)", str(t or ""))
               for t in page_texts.values()):
            continue
        for page, text in page_texts.items():
            body = str(text or "")
            for match in _NUMBER_RE.finditer(body):
                other = _to_decimal(match.group(0))
                if other is None or other == value or abs(other) < 10:
                    continue
                if abs(other - value) / abs(value) > Decimal("0.05"):
                    continue
                after = body[match.end():match.end() + 12]
                if not re.match(r"\s*м(?![²2³3])", after):
                    continue
                start = max(0, match.start() - 60)
                space = body.find(" ", start, match.start())
                if space >= 0:
                    start = space + 1
                raw = body[start:match.end() + 40]
                clean = re.sub(r"<[^>]+>", " ", raw)
                clean = re.sub(r"\s+", " ", clean).strip()
                words = clean.split(" ")
                snippet = " ".join(words[:16])
                findings.append({
                    "kind": "length-operand-conflicts",
                    "cell": cell,
                    "value": format(value, "f"),
                    "message": (
                        f"операнд {cell} = {format(value, 'f')} не встретился "
                        f"на связанных страницах, но на стр. {page} есть "
                        f"похожее число: «{snippet.strip()}»"),
                })
                break
            else:
                continue
            break
    return findings
