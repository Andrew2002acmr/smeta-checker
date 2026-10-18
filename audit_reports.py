"""User-facing audit exports; raw audit data and row order are never mutated."""

import io
import re
from decimal import Decimal
from urllib.parse import quote

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Inches, Pt
import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from audit_core import INCOMPLETE, TECHNICAL_ERROR
from audit_models import AuditResult, VorPosition
from pdf_binding import split_pdf_refs

DISCREPANCY = "Обнаружено расхождение"
MANUAL = "Требуется ручная проверка"
NOT_CHECKED = "Проверка не выполнена"
STATUS_ORDER = {DISCREPANCY: 0, MANUAL: 1, NOT_CHECKED: 2, "ОК": 3}
STATUS_COLORS = {DISCREPANCY: "FEE2E2", MANUAL: "FEF3C7",
                 NOT_CHECKED: "E5E7EB", "ОК": "DCFCE7"}
REPORT_COLUMNS = ["№", "Наименование работ", "Статус аудита", "Что не так",
                  "Наименование файла", "Где проверить", "Что сделать"]


def _text(value):
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in {"", "nan", "none", "null", "-"} else text


def _basename(value):
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _quantity(value, unit):
    value, unit = _text(value), _text(unit)
    return value if not unit or value.endswith(unit) else f"{value} {unit}".strip()


def _number(value, unit):
    value = _text(value)
    if unit and value.endswith(unit):
        value = value[:-len(unit)].strip()
    if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value):
        return Decimal(value.replace(",", "."))
    return None


def _as_task_dict(task):
    if isinstance(task, VorPosition):
        return task.legacy_task()
    return task


def _as_source_dict(source):
    if isinstance(source, AuditResult):
        data = source.to_legacy()
        data["found_page"] = (
            int(source.found_page) if source.found_page.isdigit() else None
        )
        return data
    return source


def build_user_report(df, tracking, tasks):
    """Project explicit presentation fields; align tracking by position, not df index."""
    tracking = [_as_source_dict(source) for source in tracking]
    tasks = [_as_task_dict(task) for task in tasks]
    if len(df) != len(tracking) or len(df) != len(tasks):
        raise ValueError("Данные позиций и источников отчёта не согласованы")
    records = []
    for (_, row), source, task in zip(df.iterrows(), tracking, tasks):
        files = [_basename(ref) for ref in split_pdf_refs(task.get("file_refs", ""))]
        if source.get("pdf_name"):
            resolved = _basename(source["pdf_name"])
            files = [next((name for name in files if name.casefold() == resolved.casefold()), resolved)]
        files = list(dict.fromkeys(files))
        pages = sorted(set(source.get("found_pages") or []))
        location = ("стр. " + ", ".join(map(str, pages))) if pages else "Место не установлено"
        if not pages and source.get("vor_pages"):
            location += "; ссылка ВОР: стр. " + ", ".join(map(str, source["vor_pages"]))
        problems, actions = [], []
        raw_status = row.get("Статус аудита")
        status = MANUAL
        expected_volume, expected_unit = _text(task.get("w_vol")), _text(task.get("w_unit"))
        actual_volume, actual_unit = _text(source.get("found_volume")), _text(source.get("found_unit"))
        if task.get("bind_error"):
            status = NOT_CHECKED
            problems.append("Не удалось однозначно определить исходный документ для позиции.")
            actions.append("Проверьте имя файла в ВОР и наличие документа; укажите один исходный файл и повторите проверку.")
        elif not source.get("work_found"):
            if raw_status == TECHNICAL_ERROR:
                status = NOT_CHECKED
                problems.append("Проверку не удалось завершить. Соответствие проекту не установлено.")
                actions.append("Повторите проверку; при повторном сбое сверьте позицию с исходным документом вручную.")
            else:
                problems.append("Недостаточно подтверждений работы в ПД. Это не доказывает её отсутствие в документе.")
                actions.append("Найдите работу в исходном документе и сверьте позицию ВОР вручную.")
        else:
            before = _number(expected_volume, expected_unit)
            after = _number(actual_volume, actual_unit)
            same_unit = expected_unit.casefold().replace("³", "3").replace("²", "2") == actual_unit.casefold().replace("³", "3").replace("²", "2")
            calculation = source.get("volume_calculation")
            actual_description = _quantity(actual_volume, actual_unit)
            if calculation:
                area, thickness = calculation["area"], calculation["thickness"]
                actual_description += (
                    f" (расчёт по ПД: {area['value']} {area['unit']} × "
                    f"{thickness['value']} {thickness['unit']})")
            if not source.get("volume_confirmed"):
                if before is not None and after is not None and before != after and same_unit:
                    status = DISCREPANCY
                    delta = format(abs(before - after), "f")
                    origin = "по расчёту из ПД" if calculation else "в ПД"
                    problems.append(f"В ВОР указано {_quantity(expected_volume, expected_unit)}, {origin} — {actual_description}. Разница — {delta} {expected_unit}.")
                else:
                    problems.append(f"Объём ВОР ({_quantity(expected_volume, expected_unit)}) не подтверждён ПД" + (f"; найдено {actual_description}." if actual_volume else "."))
                actions.append("Сверьте объём и расчёт по ПД; при подтверждении расхождения уточните ВОР.")
            if not source.get("unit_confirmed"):
                problems.append(f"Единица ВОР «{expected_unit}» не подтверждена ПД" + (f"; найдена «{actual_unit}»." if actual_unit else "."))
                if actual_unit and expected_unit and not same_unit and "/" not in actual_unit and "/" not in expected_unit:
                    status = DISCREPANCY
                actions.append("Проверьте единицы и необходимость пересчёта количества.")
            if not source.get("comment_confirmed"):
                comment = _text(task.get("w_comment"))
                problems.append(f"Условие ВОР{(' «' + comment + '»') if comment else ''} не подтверждено ПД.")
                actions.append("Сверьте условие с описанием работы в ПД; не меняйте ВОР без подтверждения.")
            found_page = source.get("found_page")
            if found_page and source.get("vor_pages") and found_page not in source["vor_pages"]:
                status = DISCREPANCY
                problems.append("Работа найдена вне страниц, указанных в ВОР (стр. " + ", ".join(map(str, source["vor_pages"])) + ").")
                actions.append("Проверьте и уточните ссылку на страницы в ВОР.")
            if not task.get("target_indices") or "недопустимые" in task.get("loc_desc", ""):
                status = DISCREPANCY
                problems.append("Ссылка ВОР на страницы отсутствует или содержит недопустимые номера: " + (_text(task.get("w_page")) or "не указана") + ".")
                actions.append("Укажите действительные номера страниц исходного PDF.")
            if raw_status in {INCOMPLETE, TECHNICAL_ERROR}:
                problems.append("Проверка завершена не полностью; остальные сведения требуют ручной сверки.")
                actions.append("Сверьте недостающие сведения вручную или повторите проверку.")
            if not problems and raw_status == "ОК":
                status = "ОК"
            elif not problems:
                problems.append("Соответствие позиции проекту требует ручного подтверждения.")
                actions.append("Сверьте описание работы и ссылку на ПД.")
        records.append(dict(zip(REPORT_COLUMNS, [row.iloc[0], task["w_name"], status,
            " ".join(problems) if problems else "Расхождений не обнаружено.",
            "\n".join(files) or "Не указан", location,
            " ".join(dict.fromkeys(actions)) if actions else "Не требуется."])))
    report = pd.DataFrame(records, columns=REPORT_COLUMNS)
    return report.sort_values("Статус аудита", key=lambda col: col.map(STATUS_ORDER), kind="stable").reset_index(drop=True)


def _source_link(row):
    name = row["Наименование файла"]
    if name == "Не указан" or "\n" in name:
        return None
    match = re.match(r"стр\. (\d+)", row["Где проверить"])
    return quote(name) + (f"#page={match[1]}" if match else "")


def generate_excel_report(report):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        report.to_excel(writer, index=False, sheet_name="Аудит")
        sheet = writer.sheets["Аудит"]
        side = Side(style="thin", color="D3D3D3")
        border = Border(left=side, right=side, top=side, bottom=side)
        for cells in sheet:
            for cell in cells:
                # All document-supplied text is literal, never an Excel formula.
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.border = border
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.font = Font(color="FFFFFF", bold=True, size=10)
        for i, (_, row) in enumerate(report.iterrows(), 2):
            sheet.cell(i, 3).fill = PatternFill("solid", fgColor=STATUS_COLORS[row["Статус аудита"]])
            link = _source_link(row)
            if link:
                sheet.cell(i, 5).hyperlink = link
                sheet.cell(i, 5).font = Font(color="0563C1", underline="single")
        for i, width in enumerate((8, 48, 28, 65, 32, 35, 60), 1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        sheet.freeze_panes = "C2"
        sheet.auto_filter.ref = sheet.dimensions
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = "1:1"
    return out.getvalue()


def generate_word_report(report, meta):
    doc = docx.Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Inches(0.6)
    doc.styles["Normal"].font.size = Pt(9)
    doc.add_heading("Результаты аудита ВОР и ПД", 0)
    doc.add_paragraph(f"Объект: {meta.get('object_name', 'Не указано')}\nОснование: {meta.get('basis_code', 'ПД')}")
    counts = report["Статус аудита"].value_counts()
    doc.add_paragraph(f"Всего позиций: {len(report)}. " + "; ".join(f"{status}: {counts.get(status, 0)}" for status in STATUS_ORDER))
    table = doc.add_table(rows=1, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ("№", "Наименование работ", "Статус аудита", "Результат и действия")):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for _, row in report.iterrows():
        cells = table.add_row().cells
        cells[0].text = str(row["№"])
        cells[1].text = str(row["Наименование работ"])
        cells[2].text = row["Статус аудита"]
        cells[2]._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{STATUS_COLORS[row["Статус аудита"]]}"/>'))
        cells[3].text = row["Что не так"]
        paragraph = cells[3].add_paragraph("Где проверить: ")
        link = _source_link(row)
        if link:
            hyperlink = OxmlElement("w:hyperlink")
            hyperlink.set(qn("r:id"), doc.part.relate_to(link, RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
            run = OxmlElement("w:r")
            properties = OxmlElement("w:rPr")
            style = OxmlElement("w:u")
            style.set(qn("w:val"), "single")
            properties.append(style)
            run.append(properties)
            text = OxmlElement("w:t")
            text.text = row["Наименование файла"]
            run.append(text)
            hyperlink.append(run)
            paragraph._p.append(hyperlink)
        else:
            paragraph.add_run(row["Наименование файла"])
        paragraph.add_run("; " + row["Где проверить"] + ".")
        cells[3].add_paragraph("Что сделать: " + row["Что сделать"])
    for word_row in table.rows:
        for cell, width in zip(word_row.cells, (0.4, 1.5, 1.2, 3.9)):
            cell.width = Inches(width)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
