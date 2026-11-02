"""Conservative links between calculated VOR materials and their parent work.

These are workbook relationships, never confirmation of a normative rate.
No model calls, guessed densities or summation across unrelated sections.
"""
from decimal import Decimal, InvalidOperation
import re


def _number(value):
    try:
        result = Decimal(str(value).replace(",", ".").replace(" ", ""))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _family(name):
    name = str(name).lower().replace("ё", "е")
    if "щпс" in name or re.search(r"щебеночн\w*[\s-]+песчан", name):
        return "щпс"
    for family in ("асфальтобетон", "бетон", "щебен", "песок"):
        if family in name:
            return family
    return ""


def material_links(tasks):
    links = {}
    by_sheet = {}
    for task in tasks:
        source = task.get("position_source") or {}
        if source.get("sheet") and isinstance(source.get("row"), int):
            by_sheet.setdefault(source["sheet"], []).append(task)
    for sheet_tasks in by_sheet.values():
        ordered = sorted(sheet_tasks, key=lambda t: t["position_source"]["row"])
        for i, task in enumerate(ordered):
            calc = task.get("calculation") or {}
            if (str(task.get("position_type", "")).casefold() != "материал"
                    or calc.get("status") != "calculated" or calc.get("cached_mismatch")
                    or calc.get("error")):
                continue
            value, calculated = _number(task.get("w_vol")), _number(calc.get("value"))
            if value is None or calculated is None or value <= 0:
                continue
            if abs(value - calculated) > max(abs(value), Decimal(1)) * Decimal("0.000000001"):
                continue
            family = _family(task.get("w_name"))
            if not family:
                continue
            # Nearest preceding work; material siblings may occur between rows.
            parent = next((t for t in reversed(ordered[:i])
                           if str(t.get("position_type", "")).casefold() == "работа"), None)
            if parent is None or _family(parent.get("w_name")) != family:
                continue
            source, parent_source = task["position_source"], parent["position_source"]
            if source.get("sections") != parent_source.get("sections"):
                continue
            parent_value = _number(parent.get("w_vol"))
            deps = calc.get("dependencies") or []
            parent_cell = f"D{parent_source['row']}"
            literal_match = parent_value is not None and parent_value > 0 and any(
                _number(d.get("value")) == parent_value for d in deps)
            cell_match = any(str(d.get("cell", "")).replace("$", "").upper() == parent_cell for d in deps)
            if not (cell_match or literal_match):
                continue
            # Area -> volume requires the work's stated thickness in the formula.
            parent_unit = str(parent.get("w_unit", "")).replace("²", "2").replace("³", "3").strip()
            material_unit = str(task.get("w_unit", "")).replace("²", "2").replace("³", "3").strip()
            if parent_unit != material_unit:
                thickness = re.search(r"\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b", parent.get("w_name", ""), re.I)
                if parent_unit != "м2" or material_unit != "м3" or not thickness:
                    continue
                depth = _number(thickness[1]) / {"мм": 1000, "см": 100, "м": 1}[thickness[2].lower()]
                if not any(_number(d.get("value")) == depth for d in deps):
                    continue
            links[task.get("idx")] = {
                "index": parent.get("idx"),
                "number": parent.get("number") or str(parent.get("idx", 0) + 1), "name": parent.get("w_name", ""),
                "row": parent_source["row"], "sheet": parent_source["sheet"],
                "expression": calc.get("expression", ""),
            }
    return links
