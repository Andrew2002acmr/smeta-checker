"""Narrow presentation policy: source precision and additional VOR name dimensions."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from audit_measurements import parse_measurement

def review_source(source, task):
    data = dict(source)
    checks = [dict(c) for c in (data.get("checks") or [])]
    data["checks"] = checks
    if task.get("derived_material") and (
        not data.get("work_found")
        or data.get("found_unit") in {None, "", "-"}
        or data.get("found_volume") in {None, "", "-"}
    ):
        # An unrelated candidate measurement is not the material's PD quantity.
        data.update(derived_material_pending=True, found_volume=None, found_unit=None,
                    volume_confirmed=False, unit_confirmed=False)
        for check in checks:
            if check.get("claim_id") in {"presence", "quantity", "unit"}:
                check.update(state="not_applicable", required=False, reasons=["derived_material"])
        if not any(c.get("claim_id") == "derived:rate" for c in checks):
            checks.append(dict(claim_id="derived:rate", state="review_required", required=True,
                               reasons=["normative_rate_unchecked"]))
    quantity = next((c for c in checks if c.get("claim_id") == "quantity"), {})
    unit_ok = data.get("unit_confirmed") or any(c.get("claim_id") == "unit" and c.get("state") == "confirmed" for c in checks)
    if data.get("work_found") and unit_ok and quantity.get("state") == "contradiction" and quantity.get("reasons") == ["value_mismatch"]:
        expected_unit, actual_unit = str(task.get("w_unit", "")), str(data.get("found_unit", ""))
        expected, actual = str(task.get("w_vol", "")).strip(), str(data.get("found_volume", "")).strip()
        a, b = parse_measurement(expected, unit=expected_unit), parse_measurement(actual, unit=actual_unit)
        # Round only like-for-like continuous quantities, never counts or inequalities.
        if (a.status == b.status == "exact" and a.operator == b.operator == "eq" and a.dimension == b.dimension and a.dimension in {"mass", "length", "area", "volume"} and a.unit == b.unit and a.value is not None and b.value is not None and expected_unit.strip().casefold() == actual_unit.strip().casefold() and re.fullmatch(r"[+]?\d+[.,]\d+", actual)):
            try:
                x, y = Decimal(expected.replace(",", ".")), Decimal(actual.replace(",", "."))
                quantum = Decimal(1).scaleb(y.as_tuple().exponent)
                if x != y and x.quantize(quantum, rounding=ROUND_HALF_UP) == y and abs(x-y) <= max(abs(x), abs(y)) * Decimal("0.001"):
                    quantity.update(state="confirmed", reasons=["equal_at_pd_precision"])
                    data["volume_confirmed"] = True
                    data["rounding_note"] = "Количество совпадает с учётом точности записи в ПД."
            except InvalidOperation:
                pass
    if data.get("work_found") and unit_ok:
        for check in checks:
            if str(check.get("claim_id", "")).startswith("geometry:name:") and check.get("state") in {"not_found", "review_required"}:
                check.update(state="not_applicable", required=False, reasons=["additional_vor_dimension"])
        if any("additional_vor_dimension" in c.get("reasons", []) for c in checks):
            data["vor_detail_note"] = "Размеры, дополнительно указанные в названии ВОР, не противоречат общей строке ПД; их отсутствие в ней не является расхождением."
    return data
