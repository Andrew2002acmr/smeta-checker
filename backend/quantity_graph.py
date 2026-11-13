"""Quantity graph: typed amount nodes with provenance and cross-checks.

Every VOR position gets a ``kind`` (direct / geometry / consumption /
count), operands with explicit origins (VOR cell / PDF quote / unknown),
an optional independent cross-check (e.g. schedule quantity) and a list
of structured uncertainties. The crushed-stone case (V = S x q) is the
first consumer of this mechanism, not a special branch.
"""

from __future__ import annotations

import re


from decimal import Decimal
from typing import Any

from audit_core import (
    _compact_terms,
    _known_scopes,
    _mention_res,
    _plain_page,
    _schedule_item_rows,
    _scope_key,
    _OPERATION_STOP_TERMS,
)
from audit_measurements import _numbers, format_ru, iter_measurements


def _material_skeleton(text: str) -> str:
    """Consonant skeleton for cross-form material matching (Щебень/щебня)."""
    return "".join(
        c for c in _plain_page(text).lower()
        if c in "бвгджзйклмнпрстфхцчшщ"
    )
def _material_word(text):
    """Material word after расход/норма wording, e.g. щебня in the rate."""
    import re
    match = re.search(r"(?:расход|норма)(?:\s+\S+){0,2}?\s+(\S+)",
                      _plain_page(text), re.IGNORECASE)
    return [match.group(1)] if match else []

def _material_terms(text: str) -> set[str]:
    return {
        t for t in _compact_terms(text)
        if not any(c.isdigit() for c in t) and t not in _OPERATION_STOP_TERMS
    }


def classify_quantity(work_name, comment, unit, formula, calculation):
    """Classify how a VOR amount is built: direct/geometry/consumption/count.

    Structural rules on the workbook formula, not keywords:
    - consumption: a rate unit (м3/м2, …) names V = S x q;
    - count: ROUND(...) of a discrete count in шт/comпл;
    - geometry: a bare product of two workbook cells in м3/м2;
    - direct: a plain cell reference or a bare number.
    Returns (kind, explanation). Unknown shapes fail closed as direct
    with an explanation, never as a guessed class.
    """
    from audit_core import detect_consumption

    expression = (calculation or {}).get("expression") or ""
    deps = [d for d in (calculation or {}).get("dependencies", [])
            if d.get("origin") == "vor_cell"]
    rate, _ = detect_consumption(f"{work_name}\n{comment}", formula)
    if rate is not None:
        return "consumption", "норма расхода V = S × q"
    unit_norm = (unit or "").strip().lower().replace("²", "2").replace("³", "3")
    if expression.upper().startswith("=ROUND(") and unit_norm in {"шт", "шт.", "компл"}:
        return "count", "поштучный расчёт ROUND(...)"
    cells = [d.get("cell", "?") for d in deps]
    if re.fullmatch(r"\s*=\s*[A-Z]+\d+\s*\*\s*[A-Z]+\d+\s*", expression or ""):
        return "geometry", f"произведение ячеек {' × '.join(cells)}"
    if re.fullmatch(r"\s*=\s*[A-Z]+\d+\s*", expression or "") or not expression:
        return "direct", "прямая ссылка или число"
    has_product = "*" in (expression or "")
    if has_product and len(deps) >= 1 and unit_norm in {"м3", "м2"}:
        return "geometry", "произведение базовой величины на коэффициент"
    return "direct", "неопознанная форма — проверяется как прямое число"


def link_consumption_schedule(
    work_name: str,
    comment: str,
    scope: str,
    schedule_texts: list[tuple[int, str]],
) -> dict[str, Any] | None:
    """Link a consumption position to its schedule row, code-first.

    ``schedule_texts`` is [(1-based page, raw HTML/text)]. Returns
    {page, item, volume, scope} for the unique matching row or None.
    A row matches on shared material terms (or skeleton overlap for
    inflected forms) within the expected scope. Ambiguity fails closed.
    """
    if not scope:
        return None
    want = _material_terms(f"{work_name} {comment}")
    want_skeleton = _material_skeleton(f"{work_name} {comment}")
    candidates = []
    for page, raw in schedule_texts:
        known = _known_scopes(_plain_page(raw))
        mention_res = _mention_res(known)
        for header, joined, row, unit_at in _schedule_item_rows(raw, known, mention_res):
            if _scope_key(header) != _scope_key(scope):
                continue
            shared = want & _material_terms(joined)
            skeleton_hit = (
                _material_skeleton(joined)[:12] in want_skeleton
                or want_skeleton[:12] in _material_skeleton(joined)
            )
            material_hit = any(
                _material_skeleton(word) and _material_skeleton(word) in _material_skeleton(joined)
                for word in _material_word(f"{work_name} {comment}")
            )
            if len(shared) < 2 and not (shared and skeleton_hit) and not material_hit:
                continue
            # Quantity and unit cells only: one atomic amount right after
            # the lone unit cell. Never the row maximum across unrelated
            # columns (codes, marks, or later quantities).
            unit_text = str(row[unit_at] or "").strip()
            after = [cell for cell in row[unit_at + 1:] if cell]
            measurements = []
            for cell in after:
                measurements.extend(iter_measurements(f"{cell} {unit_text}"))
                measurements.extend(iter_measurements(cell))
            if not measurements:
                continue
            amounts = []
            for measurement in measurements:
                value_text = format(measurement.value, "f")
                if any(value_text == format(item, "f")
                       for cell in after for item in _numbers(cell)):
                    amounts.append(measurement.value)
            if not amounts:
                continue
            if len({format(item, "f") for item in amounts}) != 1:
                continue
            item = next(
                (cell for cell in row[:3] if cell and len(cell) > 8), joined[:80]
            )
            candidates.append(
                {"page": page, "item": item,
                 "volume": amounts[0], "scope": header}
            )
    if len(candidates) != 1:
        return None
    return candidates[0]


def reconcile_consumption(
    claimed: Decimal,
    area: Decimal | None,
    rate: Decimal,
    schedule_volume: Decimal | None,
) -> dict[str, Any]:
    """Compare S x q arithmetic against the schedule quantity.

    Returns delta (claimed - schedule), delta_percent, schedule-derived
    rate q_pd = schedule/area, matches flag and structured uncertainties.
    Nothing is invented: missing inputs yield explicit uncertainties.
    """
    out: dict[str, Any] = {
        "delta": None, "delta_percent": None, "derived_rate": None,
        "matches": None, "uncertainties": [],
    }
    if schedule_volume is None:
        out["uncertainties"].append("schedule-volume-missing")
        return out
    out["delta"] = claimed - schedule_volume
    if schedule_volume:
        out["delta_percent"] = (out["delta"] / schedule_volume * 100).quantize(Decimal("0.01"))
    tol = max(abs(claimed), abs(schedule_volume), Decimal(1)) * Decimal("1e-9")
    out["matches"] = abs(out["delta"]) <= tol
    if area:
        out["derived_rate"] = schedule_volume / area
        if out["derived_rate"] != rate:
            out["uncertainties"].append("rate-differs-from-schedule")
    else:
        out["uncertainties"].append("area-unconfirmed")
    return out


def reconcile_position_schedule(task, track_item):
    """Reconcile one consumption position against its schedule row.

    Code-first post-pass inside the worker: ``task`` carries ``text_index``
    (both runners), ``comment``/``formula``/``volume`` and the resolved
    scope; ``track_item`` carries validated evidence. Mutates nothing;
    returns a consumption dict or None when the position is not a
    consumption case or nothing links. Scope, material, area and rate
    mismatches all fail closed with explicit uncertainties.
    """
    from audit_core import detect_consumption

    rate, unit = detect_consumption(
        f"{task.get('w_name', '')}\n{task.get('w_comment', '')}",
        task.get("w_formula", ""))
    if rate is None:
        return None
    index = task.get("text_index")
    if index is None:
        return {"rate": format_ru(rate), "unit": unit,
                "uncertainties": ["schedule-text-unavailable"]}
    source = task.get("position_source")
    expected_scope = source.get("scope_key", "") if isinstance(source, dict) else None
    scope = ""
    volume_pages = sorted({e["page"] for e in track_item.get("evidence") or []
                           if e.get("kind") == "volume"})
    for page in volume_pages:
        text = index.texts[page - 1] if 0 < page <= len(index.texts) else ""
        known = _known_scopes(_plain_page(text))
        if len(known) == 1:
            scope = next(iter(known.values()))
            break
    if not scope:
        scope, _ = index.related_context(
            task.get("target_indices") or [], task.get("w_name", ""),
            task.get("w_vol", ""), task.get("w_formula", ""), task.get("w_comment", ""),
            expected_scope=expected_scope)
    schedule_texts = [(i + 1, text) for i, text in enumerate(index.texts)
                      if "поставщик" in _plain_page(text).lower()
                      and "наименован" in _plain_page(text).lower()]
    hit = link_consumption_schedule(
        task.get("w_name", ""), task.get("w_comment", ""), scope, schedule_texts)
    chain = None
    chain_positions = task.get("vor_positions")
    if chain_positions is not None:
        from types import SimpleNamespace
        probe = SimpleNamespace(
            calculation=task.get("calculation", {}),
            position_type=task.get("position_type", ""),
            work_name=task.get("w_name", ""),
            comment=task.get("w_comment", ""),
            formula=task.get("w_formula", ""),
            unit=task.get("w_unit", ""),
            volume=task.get("w_vol", ""),
            index=task.get("idx", -1),
            number=task.get("number", ""),
            position_source=task.get("position_source"))
        chain = verify_stock_chain(probe, chain_positions)
    if chain is not None and chain.get("matches"):
        claimed = _numbers(str(chain.get("material_volume", "")))
    else:
        claimed = _numbers(str(task.get("w_vol", "")))
    claimed_value = next(iter(claimed)) if len(claimed) == 1 else None
    area = None
    work_numbers = _numbers(str(task.get("w_vol", "")))
    work_value = next(iter(work_numbers)) if len(work_numbers) == 1 else None
    if work_value is not None and rate:
        base = work_value / rate
        tol = max(abs(base), Decimal(1)) * Decimal("1e-9")
        for dep in (task.get("calculation") or {}).get("dependencies", []):
            if dep.get("origin") != "vor_cell":
                continue
            try:
                candidate = Decimal(str(dep.get("value")))
            except Exception:
                continue
            if abs(candidate - base) <= tol:
                area = candidate
                break
    if claimed_value is None:
        return {"rate": format_ru(rate), "unit": unit,
                "uncertainties": ["claimed-volume-ambiguous"]}
    if hit is None:
        return {"rate": format_ru(rate), "unit": unit,
                "area": format_ru(area) if area is not None else "",
                "uncertainties": ["schedule-row-unlinked"
                                  if scope else "scope-unresolved"]}
    result = reconcile_consumption(claimed_value, area, rate, hit["volume"])
    out: dict[str, Any] = {"rate": format_ru(rate), "unit": unit,
            "area": format_ru(area) if area is not None else "",
            "schedule_volume": format_ru(hit["volume"]),
            "schedule_page": hit["page"], "schedule_item": hit["item"],
            "derived_rate": (format_ru(result["derived_rate"])
                             if result["derived_rate"] is not None else ""),
            "delta": (format_ru(result["delta"])
                      if result["delta"] is not None else ""),
            "delta_percent": (format_ru(result["delta_percent"])
                              if result["delta_percent"] is not None else ""),
            "matches": result["matches"],
            "uncertainties": result["uncertainties"]}
    if chain is not None and chain.get("matches"):
        out["stock_chain"] = {
            "material_number": chain.get("material_number", ""),
            "factor": chain.get("factor", ""),
            "work_volume": format_ru(_numbers(str(task.get("w_vol", ""))).pop()
                                     if len(_numbers(str(task.get("w_vol", "")))) == 1 else ""),
            "material_volume": format_ru(claimed_value),
        }
        out["uncertainties"] = [
            u for u in out["uncertainties"] if u != "rate-differs-from-schedule"]
        out["uncertainties"] = [
            *out["uncertainties"], "rate-justification-open"]
    return out


def _calc_signature(calculation):
    """Structural signature: (vor cells, literals) of a D-expression."""
    deps = (calculation or {}).get("dependencies", []) or []
    cells = sorted(
        str(d.get("cell")) for d in deps
        if isinstance(d, dict) and d.get("origin") == "vor_cell" and d.get("cell"))
    literals = sorted(
        str(d.get("value")) for d in deps
        if isinstance(d, dict) and d.get("origin") == "formula_literal")
    return cells, literals


def _position_scope(position) -> str:
    """Independent VOR section scope; "" means unknown, never inferred."""
    source = getattr(position, "position_source", None)
    if isinstance(source, dict):
        return str(source.get("scope_key") or "")
    return str(getattr(source, "scope_key", "") or "")


def link_stock_chain(position, positions):
    """Link a consumption work to its stocked material, structure-first.

    A work ``=BASE × rate`` pairs with a material ``=BASE × rate × factor``
    that shares every base cell and literal and adds exactly one extra
    literal (the stock factor). Both rows must share the same source
    section/scope; several structural matches fail closed as ambiguous.
    Returns {material_index, factor} or None. No keywords, no PDF.
    """
    calc = getattr(position, "calculation", None) or {}
    if calc.get("status") != "calculated":
        return None
    if str(getattr(position, "position_type", "")).casefold() != "работа":
        return None
    from audit_core import detect_consumption
    rate, _ = detect_consumption(
        f"{getattr(position, 'work_name', '')}\n{getattr(position, 'comment', '')}",
        getattr(position, "formula", ""))
    if rate is None:
        return None
    base_cells, base_literals = _calc_signature(calc)
    if not base_cells or not base_literals:
        return None
    own_unit = str(getattr(position, "unit", "") or "").strip().casefold()
    own_index = getattr(position, "index", -1)
    own_scope = _position_scope(position)
    matches = []
    for other in positions or []:
        if getattr(other, "index", -1) <= own_index:
            continue
        if str(getattr(other, "position_type", "")).casefold() != "материал":
            continue
        if str(getattr(other, "unit", "") or "").strip().casefold() != own_unit:
            continue
        # Same source section/scope only: a stocked material in another
        # group never confirms this work, even with equal numbers.
        if own_scope and _position_scope(other) != own_scope:
            continue
        other_calc = getattr(other, "calculation", None) or {}
        if other_calc.get("status") != "calculated":
            continue
        other_cells, other_literals = _calc_signature(other_calc)
        if other_cells != base_cells:
            continue
        if len(other_literals) != len(base_literals) + 1:
            continue
        if not all(lit in other_literals for lit in base_literals):
            continue
        extra = [lit for lit in other_literals if lit not in base_literals]
        try:
            factor = Decimal(extra[0])
        except Exception:
            continue
        if factor <= 0:
            continue
        matches.append({"material_index": getattr(other, "index", -1),
                        "material_number": getattr(other, "number", ""),
                        "factor": format(factor.normalize(), "f"),
                        "material_volume": getattr(other, "volume", "")})
    if len(matches) != 1:
        # Zero, or several structurally identical materials: ambiguity
        # fails closed instead of taking the first match across sections.
        return None
    return matches[0]


def verify_stock_chain(position, positions):
    """Verify work × factor = material by workbook arithmetic.

    Returns {expected, matches} with Decimal comparison or None when the
    position has no stock pair. Pure workbook math, no model, no PDF.
    """
    link = link_stock_chain(position, positions)
    if link is None:
        return None
    try:
        work_value = Decimal(str(getattr(position, "volume", "")).replace(",", "."))
        material_value = Decimal(
            str(link["material_volume"]).replace(",", "."))
        expected = work_value * Decimal(link["factor"])
    except Exception:
        return {**link, "expected": "", "matches": None,
                "uncertainties": ["chain-arithmetic-failed"]}
    tol = max(abs(expected), Decimal(1)) * Decimal("1e-9")
    matches = abs(expected - material_value) <= tol
    return {**link, "expected": format(expected.normalize(), "f"),
            "matches": matches,
            "uncertainties": [] if matches else ["chain-mismatch"]}
