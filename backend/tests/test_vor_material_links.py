from copy import deepcopy
import pytest

from audit_reports import build_user_report, DISCREPANCY, MANUAL
from vor_material_links import material_links


def tasks():
    source = dict(sheet="ВОР", sections=[dict(address="A17", text="Улица")])
    parent = dict(idx=0, number="1", w_name="Устройство основания из ЩПС, h = 12 см",
                  w_vol="431.61", w_unit="м2", position_type="Работа",
                  position_source={**source, "row": 18})
    material = dict(idx=1, number="2", w_name="Щебеночно-песчаная смесь М800 типа 0/31,5 мм",
                    w_vol="65.259432", w_unit="м3", position_type="Материал",
                    position_source={**source, "row": 19},
                    calculation=dict(status="calculated", value="65.259432",
                                     expression="=(431.61*0.12)*1.26",
                                     dependencies=[dict(value=v) for v in ["431.61", "0.12", "1.26"]]))
    return [parent, material]


def test_links_area_and_thickness_without_claiming_normative_rate():
    pair = tasks()
    assert material_links(pair)[1]["row"] == 18
    pair[1]["calculation"]["dependencies"][0] = dict(cell="$D$18", value="431.61")
    assert material_links(pair)[1]["number"] == "1"


def test_rejects_unrelated_rows_wrong_calculations_and_missing_thickness():
    for change in [
        lambda p: p[0].update(w_name="Устройство асфальтобетонного покрытия, h = 12 см"),
        lambda p: p[1]["position_source"].update(sections=[]),
        lambda p: p[1].update(w_vol="66"),
        lambda p: p[1]["calculation"].update(cached_mismatch=True),
        lambda p: p[0].update(w_name="Устройство основания из ЩПС"),
        lambda p: p[1]["calculation"].update(dependencies=[dict(value="999"), dict(value="0.12")]),
    ]:
        pair = tasks()
        change(pair)
        assert material_links(pair) == {}


@pytest.mark.parametrize("evaluation", [None, "evidence-claims-v1"])
def test_missing_material_is_explained_but_actual_conflicts_remain(evaluation):
    pair = tasks()
    source = dict(status="НЕ НАЙДЕНО", work_found=False, found_volume="431.61", found_unit="м2",
                  evaluation_version=evaluation, evidence=[{}],
                  checks=[dict(claim_id="quantity", state="contradiction", reasons=["unit_mismatch"])])
    before = deepcopy(source)
    rows = [{"№": "1", "Статус аудита": "OK"}, {"№": "2", "Статус аудита": source["status"]}]
    report = build_user_report(rows, [dict(status="OK", work_found=True), source], pair)
    material = next(r for r in report if r["№"] == "2")
    assert material["Статус аудита"] == MANUAL
    assert "Отдельная строка материала в ПД не требуется" in material["Что не так"]
    assert "Коэффициенты расхода требуют проверки" in material["Что не так"]
    assert source == before
    # A name match without a bound unit/quantity is not a complete material row.
    source.update(work_found=True, found_unit="")
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == MANUAL
    source.update(work_found=False)
    source["workbook_notes"] = [dict(kind="formula-mismatch", message="Арифметика не совпала")]
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == DISCREPANCY
    source.pop("workbook_notes")
    source.update(work_found=True, unit_confirmed=True, found_volume="60", found_unit="м3")
    source["checks"][0].update(reasons=["value_mismatch"])
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == DISCREPANCY
