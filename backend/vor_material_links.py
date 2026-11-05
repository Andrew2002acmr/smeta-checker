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
    if re.search(r"бортов\w*\s+кам(?:н|ен)|кам\w*\s+бортов", name):
        return "бортовой камень"
    if "щпс" in name or re.search(r"щебеночн\w*[\s-]+песчан", name):
        return "щпс"
    for family in ("асфальтобетон", "бетон", "щебен", "песок"):
        if family in name:
            return family
    if "эмульс" in name and ("битум" in name or re.search(r"\bэбд[а-я]*\b", name)):
        return "эмульсия"
    if "битум" in name:
        return "битум"
    if "подгрунтовоч" in name:
        return "подгрунтовка"
    return ""


def _compatible_family(work, material):
    # A priming work without a named binder can own either resource. This does
    # not make bitumen and emulsion interchangeable when the binder is named.
    return work == material or (work == "подгрунтовка" and material in {"эмульсия", "битум"})


def material_links(tasks):
    from vor_material_ownership import material_owners
    structural=material_owners(tasks)
    links = {}
    by_sheet = {}
    for task in tasks:
        source = task.get("position_source") or {}
        if source.get("sheet") and isinstance(source.get("row"), int):
            region = (source.get("document_sha256"), source["sheet"], source.get("header_row"))
            by_sheet.setdefault(region, []).append(task)
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
            if parent is None:
                continue
            parent_family=_family(parent.get('w_name'))
            owner=structural.get(task.get('idx')) or {}
            generic_base=(not parent_family and owner.get('index')==parent.get('idx')
                and re.match(r'^\s*Устройство\s+(?:слоя\s+)?основания\b',parent.get('w_name',''),re.I)
                and str(parent.get('w_unit','')).strip() in {'м2','м²'}
                and str(task.get('w_unit','')).strip() in {'м3','м³'})
            if not _compatible_family(parent_family,family) and not generic_base:
                continue
            source, parent_source = task["position_source"], parent["position_source"]
            if source.get("sections") != parent_source.get("sections"):
                continue
            # Workbook structure establishes ownership, not numeric equality.
            # Rounded or incorrect formula bases must retain their real parent
            # so the numeric checker can expose the difference separately.
            parent_unit = str(parent.get("w_unit", "")).replace("²", "2").replace("³", "3").strip()
            material_unit = str(task.get("w_unit", "")).replace("²", "2").replace("³", "3").strip()
            if not parent_unit or not material_unit:
                continue
            area_to_asphalt_mass = (family == 'асфальтобетон' and parent_unit == 'м2'
                                    and material_unit in {'т', 'кг'})
            # A workbook multiplier alone cannot establish conversion from a
            # product's installed length to its volume. Keep its structural
            # owner through material_owners, without a formal derivation link.
            if parent_unit != material_unit and not area_to_asphalt_mass:
                thickness = re.search(r"\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b", parent.get("w_name", ""), re.I)
                if parent_unit != "м2" or material_unit != "м3" or not thickness:
                    continue
                depth=_number(thickness[1])
                if depth is None or depth<=0:
                    continue
            links[task.get("idx")] = {
                "index": parent.get("idx"),
                "number": parent.get("number") or str(parent.get("idx", 0) + 1), "name": parent.get("w_name", ""),
                "row": parent_source["row"], "sheet": parent_source["sheet"],
                "expression": calc.get("expression", ""),
            }
            if generic_base:
                links[task.get('idx')]['relation_basis']='explicit_workbook_owner_generic_base'
                links[task.get('idx')]['material_compatibility_verified']=False
    return links
