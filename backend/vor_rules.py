import re
import datetime
import openpyxl
from openpyxl.utils import get_column_letter
import io
from vor_formula import has_formula_text
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls


def safe_calc_formula(expr_str):
    """Безопасно вычисляет математическую формулу из строки."""
    clean = str(expr_str).replace("=", "").replace(",", ".").replace("^", "**").strip()
    # Разрешены только цифры, знаки +, -, *, /, скобки, точки и пробелы
    if not re.match(r"^[0-9+\-*/().\s]+$", clean):
        return None, "Формула содержит недопустимые символы или текст"
    try:
        val = eval(clean, {"__builtins__": None}, {})
        return round(float(val), 6), None  # Возвращает точное число
    except Exception as e:
        return None, f"Ошибка вычисления формулы: {e}"


def vor_sheet_names(file_source) -> list[str]:
    """Workbook sheet names in order; used for explicit sheet choice."""
    wb = openpyxl.load_workbook(file_source, data_only=True, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


_ISSUE_ORDER = {
    "ROW_POS": 0,
    "ROW_INVALID": 1,
    "SUB_LINK": 2,
    "LINK_CHECK": 3,
    "SECTION": 4,
    "SECTION_STYLE": 5,
    "SECTION_EMPTY": 6,
    "STATIC": 7,
    "VAR": 8,
    "HEADER_BAN": 9,
    "COL_L_LIMIT": 10,
}


_GROUP_TITLES = [
    (
        "ROW_POS",
        "Наименование работы/ресурса не заполнено",
        "Не заполнено название работы",
        "Впишите название работы или ресурса в колонку B",
    ),
    (
        "ROW_POS",
        "Единица измерения не заполнена",
        "Не заполнена единица измерения",
        "Впишите единицу измерения в колонку C — например м3, м2, шт",
    ),
    (
        "ROW_POS",
        "Объем должен быть больше 0",
        "Объём нулевой или отрицательный",
        "Впишите объём числом больше нуля в колонку D",
    ),
    (
        "ROW_POS",
        "Объем должен быть числом больше 0",
        "Объём не число",
        "Впишите объём числом больше нуля в колонку D",
    ),
    (
        "ROW_POS",
        "Некорректное значение объема",
        "Объём не число",
        "Впишите объём числом больше нуля в колонку D — например 231 или 9/45",
    ),
    (
        "ROW_POS",
        "не совпадает с объемом в графе D",
        "Формула не сходится с объёмом",
        "Исправьте формулу в колонке E или объём в колонке D, чтобы сошлись",
    ),
    (
        "ROW_POS",
        "Путь к файлу должен оканчиваться на .pdf",
        "Файл не PDF",
        "Укажите в колонке G имя PDF-файла с окончанием .pdf",
    ),
    (
        "ROW_POS",
        "Идентификатор не заполнен",
        "Не заполнен идентификатор",
        "Впишите идентификатор позиции в колонку J — например Ф78",
    ),
    (
        "ROW_POS",
        "Дубликат идентификатора",
        "Повторяющийся идентификатор",
        "Замените дубли на идентификаторы, которых ещё нет в ведомости",
    ),
    (
        "ROW_POS",
        "Тип позиции не заполнен",
        "Не заполнен тип позиции",
        "Впишите тип позиции в колонку K — например Работа или Материал",
    ),
    (
        "ROW_INVALID",
        "Не удалось отнести строку",
        "Строка непонятного вида",
        "Приведите строку к виду раздела, позиции или продолжения ссылок",
    ),
    (
        "SUB_LINK",
        "не привязана к позиции работы выше",
        "Ссылки без позиции",
        "Переместите строку ссылок сразу под нужную позицию или удалите",
    ),
    (
        "LINK_CHECK",
        "Номера страниц должны быть только числами",
        "Номера страниц не числами",
        "Оставьте в колонке H только номера через один пробел — например 9 10",
    ),
    (
        "LINK_CHECK",
        "не заполнены номера страниц в столбце H",
        "Нет номеров страниц",
        "Впишите в колонку H номера страниц из файла — например 9 10",
    ),
    (
        "LINK_CHECK",
        "Ссылка на документацию пуста",
        "Пустая ссылка на документацию",
        "Впишите ссылку в колонку F или очистите её",
    ),
    (
        "SECTION",
        "Пропущено двоеточие",
        "В заголовке раздела нет двоеточия",
        "Исправьте заголовок: «Раздел», двоеточие, номер с точкой, название",
    ),
    (
        "SECTION",
        "Заголовок раздела должен быть в формате",
        "Заголовок раздела не по шаблону",
        "Исправьте заголовок: «Раздел», двоеточие, номер с точкой, название",
    ),
    (
        "SECTION_STYLE",
        "полужирным шрифтом",
        "Раздел не выделен жирным",
        "Выделите заголовок раздела полужирным (Ctrl+B в Excel)",
    ),
    (
        "SECTION_EMPTY",
        "колонки B–K должны быть пустыми",
        "Лишние данные в строке раздела",
        "Очистите колонки B–K в строке раздела, оставьте только колонку A",
    ),
    (
        "HEADER_BAN",
        "не должна содержать текст 'Заголовок'",
        "Слово «Заголовок» в строке данных",
        "Уберите слово «Заголовок» из строки 18",
    ),
    (
        "COL_L_LIMIT",
        "не должно быть данных",
        "Посторонние данные справа от таблицы",
        "Уберите данные правее столбца K или перенесите на другой лист",
    ),
]


def _group_for(row):
    kind = str(row.get("type") or "")
    error = str(row.get("error") or "")
    for type_, needle, title, hint in _GROUP_TITLES:
        if kind == type_ and needle in error:
            key = f"{type_}:{needle}"
            return key, title, hint
    if kind in {"STATIC", "VAR"}:
        return "HEADER", "Шапка и реквизиты", "Заполните шапку по шаблону"
    return f"{kind}:misc", "Прочие замечания", "Исправьте по тексту замечания"


def summarize_format_rows(rows):
    """Keep findings (no OK), ordered for a human: positions and formulas
    first, sections next, header requisites after, stray-column noise last.
    Each finding gets a stable ``group`` key plus title/hint shared by the
    whole group, so the UI can collapse hundreds of identical rows.
    """
    issues = [row for row in rows if row.get("status") != "ОК"]
    for row in issues:
        key, title, hint = _group_for(row)
        row["group"] = key
        row["group_title"] = title
        row["group_hint"] = hint

    def row_number(row):
        match = re.search(r"\d+", str(row.get("coord") or ""))
        return int(match.group()) if match else 10**9

    issues.sort(
        key=lambda row: (
            _ISSUE_ORDER.get(row.get("type"), 99),
            0 if row.get("status") == "Ошибка" else 1,
            row_number(row),
            str(row.get("coord") or ""),
        )
    )
    errors = sum(1 for row in issues if row.get("status") == "Ошибка")
    return issues, errors


def validate_vor_formatting(file_source, sheet_name=None):
    """
    Комплексная проверка ВОР:
    1. Шапка и служебные ячейки (строки 1–16).
    2. Запрет данных правее столбца K (столбец L и далее).
    3. Запрет слова 'Заголовок' в строке 18.
    4. Динамическая проверка разделов, позиций и строк привязки документов (со строки 17 до конца файла).

    Без имени листа проверяется первый лист по порядку (sheetnames[0]),
    а не активный: активный — это «открытый последним в Excel».
    """
    wb = openpyxl.load_workbook(file_source, data_only=True)
    if sheet_name is None or str(sheet_name).strip() == "":
        ws = wb[wb.sheetnames[0]]
    else:
        ws = wb[sheet_name]
    results = []

    def get_val(r, c):
        v = ws.cell(row=r, column=c).value
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return v

    def add_res(coord, c_type, expected, actual, status, err_msg=""):
        results.append(
            {
                "coord": coord,
                "type": c_type,
                "expected": expected,
                "actual": str(actual).strip() if actual != "" else "<Пусто>",
                "status": status,
                "error": err_msg,
            }
        )

    # =========================================================================
    # БЛОК 1. СТРОКИ 1–16 (ШАПКА И РЕКВИЗИТЫ)
    # =========================================================================
    static_rules = [
        # Строка 1
        (
            1,
            1,
            "STATIC",
            "В ячейку A1 впишите ровно «Документ»",
            lambda v: v == "Документ",
            "Должно быть ровно 'Документ'",
        ),
        (
            1,
            4,
            "STATIC",
            "В ячейку D1 впишите ровно «Ведомость объемов работ»",
            lambda v: v == "Ведомость объемов работ",
            "Должно быть ровно 'Ведомость объемов работ'",
        ),
        # Строка 2
        (
            2,
            1,
            "STATIC",
            "В ячейку A2 впишите ровно «Версия»",
            lambda v: v == "Версия",
            "Должно быть ровно 'Версия'",
        ),
        (
            2,
            4,
            "VAR",
            "В ячейку D2 укажите версию: номер, подчёркивание, две цифры — например 3_01",
            lambda v: bool(re.match(r"^\d+_\d{2}$", str(v).strip())),
            "Версия должна быть в формате ЧЧ_ЦЦ, например 3_01",
        ),
        # Строка 4
        (
            4,
            1,
            "STATIC",
            "В ячейку A4 впишите ровно «Наименование стройки»",
            lambda v: v == "Наименование стройки",
            "Должно быть ровно 'Наименование стройки'",
        ),
        (
            4,
            4,
            "VAR",
            "В ячейку D4 впишите полное название стройки (минимум 5 символов)",
            lambda v: 5 <= len(str(v).strip()) <= 255,
            "Наименование стройки не заполнено или слишком короткое",
        ),
        # Строка 5
        (
            5,
            1,
            "STATIC",
            "В ячейку A5 впишите ровно «Наименование объекта капитального строительства»",
            lambda v: v == "Наименование объекта капитального строительства",
            "Должно быть ровно 'Наименование объекта капитального строительства'",
        ),
        (
            5,
            4,
            "VAR",
            "В ячейку D5 впишите полное название объекта (минимум 5 символов)",
            lambda v: 5 <= len(str(v).strip()) <= 255,
            "Наименование объекта не заполнено",
        ),
        # Строка 6
        (
            6,
            1,
            "STATIC",
            "В ячейку A6 впишите ровно «Ведомость объемов работ №»",
            lambda v: v == "Ведомость объемов работ №",
            "Должно быть ровно 'Ведомость объемов работ №'",
        ),
        # Строка 7
        (
            7,
            1,
            "STATIC",
            "В ячейку A7 впишите ровно «Основание((наименование раздела (подраздела) ПД))»",
            lambda v: v == "Основание((наименование раздела (подраздела) ПД))",
            "Должно быть ровно 'Основание((наименование раздела (подраздела) ПД))'",
        ),
        # Строка 8
        (
            8,
            1,
            "STATIC",
            "В ячейку A8 впишите ровно «Дата составления»",
            lambda v: v == "Дата составления",
            "Должно быть ровно 'Дата составления'",
        ),
        # Строка 10
        (
            10,
            1,
            "STATIC",
            "В ячейку A10 впишите ровно «Составил ФИО»",
            lambda v: v == "Составил ФИО",
            "Должно быть ровно 'Составил ФИО'",
        ),
        (
            10,
            4,
            "VAR",
            "В ячейку D10 укажите фамилию и инициалы через пробел — например Иванова И.И.",
            lambda v: bool(
                re.match(r"^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$", str(v).strip())
            ),
            "ФИО должно быть в формате Фамилия И.О., например Иванова И.И.",
        ),
        # Строка 11
        (
            11,
            1,
            "STATIC",
            "В ячейку A11 впишите ровно «Составил должность»",
            lambda v: v == "Составил должность",
            "Должно быть ровно 'Составил должность'",
        ),
        (
            11,
            4,
            "VAR",
            "В ячейку D11 впишите должность составителя",
            lambda v: 3 <= len(str(v).strip()) <= 100,
            "Должность составителя не заполнена",
        ),
        # Строка 12
        (
            12,
            1,
            "STATIC",
            "В ячейку A12 впишите ровно «Проверил ФИО»",
            lambda v: v == "Проверил ФИО",
            "Должно быть ровно 'Проверил ФИО'",
        ),
        (
            12,
            4,
            "VAR",
            "В ячейку D12 укажите фамилию и инициалы проверившего — например Крекова Т.Л.",
            lambda v: bool(
                re.match(r"^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$", str(v).strip())
            ),
            "ФИО проверившего должно быть в формате Фамилия И.О., например Крекова Т.Л.",
        ),
        # Строка 13
        (
            13,
            1,
            "STATIC",
            "В ячейку A13 впишите ровно «Проверил должность»",
            lambda v: v == "Проверил должность",
            "Должно быть ровно 'Проверил должность'",
        ),
        (
            13,
            4,
            "VAR",
            "В ячейку D13 впишите должность проверившего",
            lambda v: 2 <= len(str(v).strip()) <= 100,
            "Должность проверившего не заполнена",
        ),
        # Строка 15 (Заголовки колонок)
        (
            15,
            1,
            "STATIC",
            "В ячейку A15 впишите ровно «№ п.п.»",
            lambda v: v == "№ п.п.",
            "Должно быть '№ п.п.'",
        ),
        (
            15,
            2,
            "STATIC",
            "В ячейку B15 впишите ровно «Наименование работ, ресурсов, затрат по проекту»",
            lambda v: v == "Наименование работ, ресурсов, затрат по проекту",
            "Неверный заголовок столбца",
        ),
        (
            15,
            3,
            "STATIC",
            "В ячейку C15 впишите ровно «Ед. изм.»",
            lambda v: v == "Ед. изм.",
            "Должно быть 'Ед. изм.'",
        ),
        (
            15,
            4,
            "STATIC",
            "В ячейку D15 впишите ровно «Объем работ / Количество»",
            lambda v: v == "Объем работ / Количество",
            "Должно быть 'Объем работ / Количество'",
        ),
        (
            15,
            5,
            "STATIC",
            "В ячейку E15 впишите ровно «Формула расчета объемов работ и расхода материалов, потребности ресурсов»",
            lambda v: (
                v
                == "Формула расчета объемов работ и расхода материалов, потребности ресурсов"
            ),
            "Неверный заголовок столбца",
        ),
        (
            15,
            6,
            "STATIC",
            "В ячейку F15 впишите ровно «Ссылка на чертежи, спецификации в проектной документации»",
            lambda v: v == "Ссылка на чертежи, спецификации в проектной документации",
            "Неверный заголовок столбца",
        ),
        (
            15,
            7,
            "STATIC",
            "В ячейку G15 впишите ровно «Наименование файла»",
            lambda v: v == "Наименование файла",
            "Должно быть 'Наименование файла'",
        ),
        (
            15,
            8,
            "STATIC",
            "В ячейку H15 впишите ровно «Номера страниц (через пробел)» — обратите внимание: «Номера», не «Номер»",
            lambda v: v == "Номера страниц (через пробел)",
            "Должно быть 'Номера страниц (через пробел)'",
        ),
        (
            15,
            9,
            "STATIC",
            "В ячейку I15 впишите ровно «Дополнительная информация (комментарий).» — с точкой в конце",
            lambda v: v == "Дополнительная информация (комментарий).",
            "Должно быть 'Дополнительная информация (комментарий).'",
        ),
        (
            15,
            10,
            "STATIC",
            "В ячейку J15 впишите ровно «Идентификатор»",
            lambda v: v == "Идентификатор",
            "Должно быть 'Идентификатор'",
        ),
        (
            15,
            11,
            "STATIC",
            "В ячейку K15 впишите ровно «Тип позиции»",
            lambda v: v == "Тип позиции",
            "Должно быть 'Тип позиции'",
        ),
        # Строка 16 (Номера столбцов 1..9)
        (
            16,
            1,
            "STATIC",
            "В ячейку A16 впишите «1»",
            lambda v: str(v).strip() == "1",
            "Должно быть '1'",
        ),
        (
            16,
            2,
            "STATIC",
            "В ячейку B16 впишите «2»",
            lambda v: str(v).strip() == "2",
            "Должно быть '2'",
        ),
        (
            16,
            3,
            "STATIC",
            "В ячейку C16 впишите «3»",
            lambda v: str(v).strip() == "3",
            "Должно быть '3'",
        ),
        (
            16,
            4,
            "STATIC",
            "В ячейку D16 впишите «4»",
            lambda v: str(v).strip() == "4",
            "Должно быть '4'",
        ),
        (
            16,
            5,
            "STATIC",
            "В ячейку E16 впишите «5»",
            lambda v: str(v).strip() == "5",
            "Должно быть '5'",
        ),
        (
            16,
            6,
            "STATIC",
            "В ячейку F16 впишите «6»",
            lambda v: str(v).strip() == "6",
            "Должно быть '6'",
        ),
        (
            16,
            7,
            "STATIC",
            "В ячейку G16 впишите «6.1»",
            lambda v: str(v).strip() == "6.1",
            "Должно быть '6.1'",
        ),
        (
            16,
            8,
            "STATIC",
            "В ячейку H16 впишите «6.2»",
            lambda v: str(v).strip() == "6.2",
            "Должно быть '6.2'",
        ),
        (
            16,
            9,
            "STATIC",
            "В ячейку I16 впишите «7»",
            lambda v: str(v).strip() == "7",
            "Должно быть '7'",
        ),
        (
            16,
            10,
            "STATIC",
            "В ячейку J16 впишите «8»",
            lambda v: str(v).strip() == "8",
            "Должно быть '8'",
        ),
        (
            16,
            11,
            "STATIC",
            "В ячейку K16 впишите «9»",
            lambda v: str(v).strip() == "9",
            "Должно быть '9'",
        ),
    ]

    for r_idx, c_idx, c_type, exp_desc, validator, err_t in static_rules:
        coord = f"{get_column_letter(c_idx)}{r_idx}"
        val = get_val(r_idx, c_idx)
        if val == "":
            add_res(coord, c_type, exp_desc, val, "Ошибка", f"Не заполнено: {err_t}")
        elif validator(val):
            add_res(coord, c_type, exp_desc, val, "ОК")
        else:
            add_res(coord, c_type, exp_desc, val, "Ошибка", err_t)

    # D6: Номер ВОР
    val_d6 = get_val(6, 4)
    if val_d6 == "":
        add_res(
            "D6",
            "VAR",
            "В ячейку D6 впишите номер ведомости: буква «В», точка, цифры через точку — например В.79.16",
            val_d6,
            "Ошибка",
            "Номер ВОР не заполнен",
        )
    elif bool(re.match(r"^В\.\d+(\.\d+)*$", str(val_d6).strip())):
        add_res(
            "D6",
            "VAR",
            "В ячейку D6 впишите номер ведомости: буква «В», точка, цифры через точку — например В.79.16",
            val_d6,
            "ОК",
        )
    else:
        add_res(
            "D6",
            "VAR",
            "В ячейку D6 впишите номер ведомости: буква «В», точка, цифры через точку — например В.79.16",
            val_d6,
            "Внимание",
            f"Нетиповой формат номера ВОР: '{val_d6}'. Проверьте соответствие шифру проекта/имени файла.",
        )

    # D7: Основание
    val_d7 = get_val(7, 4)
    if val_d7 == "":
        add_res(
            "D7",
            "VAR",
            "В ячейку D7 впишите основание: слова «Раздел ПД №» и номер — например Раздел ПД № 3",
            val_d7,
            "Ошибка",
            "Основание не заполнено",
        )
    elif bool(re.match(r"^Раздел ПД № \d+(\.\d+)*$", str(val_d7).strip())):
        add_res(
            "D7",
            "VAR",
            "В ячейку D7 впишите основание: слова «Раздел ПД №» и номер — например Раздел ПД № 3",
            val_d7,
            "ОК",
        )
    else:
        add_res(
            "D7",
            "VAR",
            "В ячейку D7 впишите основание: слова «Раздел ПД №» и номер — например Раздел ПД № 3",
            val_d7,
            "Внимание",
            f"Нетиповой формат основания: '{val_d7}'. Проверьте соответствие шифру раздела ПД.",
        )

    # D8: Дата составления
    val_d8 = ws.cell(row=8, column=4).value
    if val_d8 is None or str(val_d8).strip() == "":
        add_res(
            "D8",
            "VAR",
            "В ячейку D8 впишите дату цифрами через точки — например 24.03.2025",
            "",
            "Ошибка",
            "Дата составления не заполнена",
        )
    elif isinstance(val_d8, (datetime.date, datetime.datetime)):
        if 2000 <= val_d8.year <= 2100:
            add_res(
                "D8",
                "VAR",
                "В ячейку D8 впишите дату цифрами через точки — например 24.03.2025",
                val_d8.strftime("%d.%m.%Y"),
                "ОК",
            )
        else:
            add_res(
                "D8",
                "VAR",
                "В ячейке D8 исправьте год на значение от 2000 до 2100",
                val_d8.strftime("%d.%m.%Y"),
                "Ошибка",
                "Год даты вне диапазона 2000–2100",
            )
    else:
        try:
            d_p = datetime.datetime.strptime(str(val_d8).strip(), "%d.%m.%Y")
            if 2000 <= d_p.year <= 2100:
                add_res(
                    "D8",
                    "VAR",
                    "В ячейку D8 впишите дату цифрами через точки — например 24.03.2025",
                    str(val_d8).strip(),
                    "ОК",
                )
            else:
                add_res(
                    "D8",
                    "VAR",
                    "В ячейке D8 исправьте год на значение от 2000 до 2100",
                    str(val_d8).strip(),
                    "Ошибка",
                    "Год даты вне диапазона 2000–2100",
                )
        except Exception:
            add_res(
                "D8",
                "VAR",
                "В ячейку D8 впишите дату цифрами через точки — например 24.03.2025",
                str(val_d8).strip(),
                "Ошибка",
                "Дата составления должна быть в формате ДД.ММ.ГГГГ, например 24.03.2025",
            )

    # =========================================================================
    # БЛОК 2. ЗАПРЕТ СЛОВА 'ЗАГОЛОВОК' В СТРОКЕ 18
    # =========================================================================
    for col_i in range(1, 12):
        if col_i in {1, 10}:
            continue
        v_18 = get_val(18, col_i)
        if "заголовок" in str(v_18).lower():
            add_res(
                f"{get_column_letter(col_i)}18",
                "HEADER_BAN",
                "Уберите слово «Заголовок» из строки 18 — там должны начинаться данные",
                v_18,
                "Ошибка",
                "Строка 18 не должна содержать текст 'Заголовок'",
            )

    # =========================================================================
    # БЛОК 3. ДИНАМИЧЕСКАЯ ПРОВЕРКА СТРОК (НАЧИНАЯ С 17 И ДО КОНЦА)
    # =========================================================================
    max_r = ws.max_row
    max_c = ws.max_column
    empty_streak = 0
    has_active_position = False

    for r in range(17, max_r + 1):
        row_vals = [get_val(r, c) for c in range(1, 12)]
        all_empty = all(v == "" for v in row_vals)

        # Пропускаем пустые строки (защита от сканирования миллионов строк)
        if all_empty:
            empty_streak += 1
            if empty_streak > 15:
                break
            continue
        empty_streak = 0

        # Распаковываем столбцы A..K
        val_a, val_b, val_c, val_d, val_e, val_f, val_g, val_h, val_i, val_j, val_k = (
            row_vals
        )

        # --- ТИП 1: СТРОКА РАЗДЕЛА ---
        # Получаем объект ячейки A и проверяем жирный шрифт
        cell_a = ws.cell(row=r, column=1)
        is_bold = bool(cell_a.font and cell_a.font.bold)
        val_a_clean = str(val_a).strip()

        # Признаки того, что строка является разделом:
        # 1) Начинается со слова "Раздел" (с двоеточием или без, в любом регистре)
        # 2) Либо ячейка жирная (bold), а остальные рабочие колонки (B, C, D, K) пусты
        is_section_candidate = (bool(
            re.match(r"^раздел", val_a_clean, re.IGNORECASE)
        ) and not (val_b and (val_c or val_d))) or (
            is_bold
            and val_b == ""
            and val_c == ""
            and val_d == ""
            and val_k == ""
            and len(val_a_clean) > 3
        )

        # --- ТИП 1: СТРОКА РАЗДЕЛА ---
        if is_section_candidate:
            has_active_position = False

            # 1. Проверка формата текста по регламенту: ^Раздел: \d+\. .+$
            format_ok = bool(re.match(r"^Раздел: \d+\. .+$", val_a_clean))

            if not format_ok:
                # Четкая подсказка, если забыли двоеточие или номер
                if re.match(r"^Раздел \d+", val_a_clean):
                    err_desc = 'Пропущено двоеточие. Должно быть строго: "Раздел: 1. Название раздела"'
                else:
                    err_desc = 'Заголовок раздела должен быть в формате "Раздел: 1. Название раздела"'
                add_res(
                    f"A{r}",
                    "SECTION",
                    f"Исправьте заголовок раздела в ячейке A{r}: слово «Раздел», двоеточие, номер с точкой, название — например «Раздел: 1. Земляные работы»",
                    val_a,
                    "Ошибка",
                    err_desc,
                )
            else:
                add_res(
                    f"A{r}",
                    "SECTION",
                    f"Заголовок раздела в ячейке A{r} оформлен верно",
                    val_a,
                    "ОК",
                )

            # 2. Проверка оформления: шрифт ОБЯЗАН быть жирным (bold)
            if not is_bold:
                add_res(
                    f"A{r}",
                    "SECTION_STYLE",
                    f"Выделите заголовок в ячейке A{r} полужирным (Ctrl+B в Excel)",
                    "Обычный шрифт",
                    "Внимание",
                    "Заголовок раздела должен быть выделен полужирным шрифтом",
                )

            # 3. Проверка, что колонки B–K пусты
            filled_bk = [
                get_column_letter(ci + 1)
                for ci, v in enumerate(row_vals[1:], start=1)
                if v != ""
            ]
            if filled_bk:
                add_res(
                    f"B{r}-K{r}",
                    "SECTION_EMPTY",
                    f"Очистите ячейки {', '.join(f'{col}{r}' for col in filled_bk)} — в строке раздела заполнена только колонка A",
                    f"Заполнены: {', '.join(filled_bk)}",
                    "Внимание",
                    "В строке раздела колонки B–K должны быть пустыми",
                )

        # --- ТИП 2: ОСНОВНАЯ СТРОКА ПОЗИЦИИ ---
        elif str(val_a).strip().isdigit() or (val_b and (val_c or val_d or val_k)):
            has_active_position = True

            # A and J are bookkeeping labels; they are neither mandatory
            # engineering facts nor quantities to compare against the PD.

            # B: наименование
            if val_b == "":
                add_res(
                    f"B{r}",
                    "ROW_POS",
                    f"В ячейку B{r} впишите название работы или ресурса",
                    val_b,
                    "Ошибка",
                    "Наименование работы/ресурса не заполнено",
                )

            # C: ед. изм.
            if val_c == "":
                add_res(
                    f"C{r}",
                    "ROW_POS",
                    f"В ячейку C{r} впишите единицу измерения — например м3, м2, шт",
                    val_c,
                    "Ошибка",
                    "Единица измерения не заполнена",
                )

            # --- D: ОБЪЕМ С УЧЕТОМ СЛЭША (/) КАК "ИЛИ" ---
            vol_raw = str(val_d).strip().replace(",", ".")
            vol_parts = []
            if "/" in vol_raw:
                # Слэш означает альтернативные единицы/значения (например 9/45 или 3/6)
                for part in vol_raw.split("/"):
                    try:
                        vol_parts.append(float(part.strip()))
                    except Exception:
                        pass
                if vol_parts:
                    add_res(f"D{r}", "ROW_POS", "Число > 0 (или через /)", val_d, "ОК")
                else:
                    add_res(
                        f"D{r}",
                        "ROW_POS",
                        f"В ячейку D{r} впишите объём числом больше нуля — например 231 или 9/45",
                        val_d,
                        "Ошибка",
                        "Некорректное значение объема",
                    )
            else:
                try:
                    vol_num = float(vol_raw)
                    if vol_num > 0:
                        vol_parts.append(vol_num)
                        add_res(f"D{r}", "ROW_POS", "Число > 0", val_d, "ОК")
                    else:
                        add_res(
                            f"D{r}",
                            "ROW_POS",
                            f"В ячейку D{r} впишите объём числом больше нуля",
                            val_d,
                            "Ошибка",
                            "Объем должен быть больше 0",
                        )
                except Exception:
                    add_res(
                        f"D{r}",
                        "ROW_POS",
                        f"В ячейку D{r} впишите объём числом больше нуля",
                        val_d,
                        "Ошибка",
                        "Объем должен быть числом больше 0",
                    )

            # --- E: ФОРМУЛА И СВЕРКА С ОБЪЕМОМ ---
            # Если формула не заполнена, пропускаем без ошибок
            if has_formula_text(val_e):
                calc_result, calc_err = safe_calc_formula(val_e)
                if calc_err:
                    add_res(
                        f"E{r}",
                        "ROW_POS",
                        f"В ячейке E{r} оставьте только цифры и знаки +-*/() — например 631,2*0,44",
                        val_e,
                        "Ошибка",
                        calc_err,
                    )
                else:
                    import math

                    def is_volume_match(calc_v, vol_v):
                        """
                        Сверяет математический расчет формулы и объем:
                        - Учитывает эквивалентность 198.36 и 198.360 (микропогрешность float).
                        - Учитывает стандартное округление и округление вверх (до 3, 2, 1 знака и целых).
                        """
                        diff = abs(calc_v - vol_v)
                        # Точное совпадение (включая 198.36 == 198.360)
                        if diff < 0.0001:
                            return True
                        # Округление до сотых (0.01) или тысячных (0.001)
                        if (
                            abs(round(calc_v, 2) - vol_v) < 0.001
                            or abs(round(calc_v, 3) - vol_v) < 0.0001
                        ):
                            return True
                        # Округление вверх до сотых: ceil(X * 100) / 100
                        if abs(math.ceil(calc_v * 100) / 100 - vol_v) < 0.001:
                            return True
                        # Округление до десятых (0.1)
                        if (
                            abs(round(calc_v, 1) - vol_v) < 0.01
                            or abs(math.ceil(calc_v * 10) / 10 - vol_v) < 0.01
                        ):
                            return True
                        # Округление до целого (78.9 -> 79, 281.85 -> 282)
                        if round(calc_v) == int(round(vol_v)) or math.ceil(
                            calc_v
                        ) == int(round(vol_v)):
                            return True
                        return False

                    match_vol = any(
                        is_volume_match(calc_result, vp) for vp in vol_parts
                    )

                    # Безопасное формирование отображения объема (без вызова .is_integer(), который ломал код)
                    if vol_parts:
                        vp_val = vol_parts[0]
                        val_d_display = (
                            str(int(round(vp_val)))
                            if float(vp_val).is_integer()
                            else str(round(vp_val, 3))
                        )
                    else:
                        val_d_display = str(val_d)

                    calc_display = str(round(calc_result, 3))

                    if match_vol:
                        add_res(
                            f"E{r}",
                            "ROW_POS",
                            f"Результат формулы = {val_d_display}",
                            f"{val_e} = {calc_display}",
                            "ОК",
                        )
                    else:
                        add_res(
                            f"E{r}",
                            "ROW_POS",
                            f"В ячейке E{r} исправьте формулу или в ячейке D{r} исправьте объём {val_d_display}: сейчас формула даёт {calc_display}, а должно получиться {val_d_display}",
                            f"{val_e} = {calc_display}",
                            "Ошибка",
                            f"Результат формулы ({calc_display}) не совпадает с объемом в графе D ({val_d_display})",
                        )

            # --- G: ПРОВЕРКА ПУТИ К ФАЙЛУ ---
            if val_g != "":
                clean_path = str(val_g).strip().strip("\"'«»")
                if not clean_path.lower().endswith(".pdf"):
                    add_res(
                        f"G{r}",
                        "ROW_POS",
                        f"В ячейке G{r} укажите имя PDF-файла с окончанием .pdf",
                        val_g,
                        "Внимание",
                        "Путь к файлу должен оканчиваться на .pdf",
                    )
                else:
                    add_res(f"G{r}", "ROW_POS", "Корректный путь к файлу", val_g, "ОК")

            # K: тип позиции (не пусто)
            if val_k == "":
                add_res(
                    f"K{r}",
                    "ROW_POS",
                    f"В ячейку K{r} впишите тип позиции — например Работа или Материал",
                    val_k,
                    "Ошибка",
                    "Тип позиции не заполнен (ожидается: Работа, Материал и т.д.)",
                )

        # --- ТИП 2b: СТРОКА ДОПОЛНИТЕЛЬНОЙ ПРИВЯЗКИ ДОКУМЕНТОВ К ПОЗИЦИИ (F, G, H) ---
        elif (
            val_a == ""
            and val_b == ""
            and val_c == ""
            and val_d == ""
            and val_k == ""
            and any(v != "" for v in [val_f, val_g, val_h])
        ):
            if not has_active_position:
                add_res(
                    f"A{r}-K{r}",
                    "SUB_LINK",
                    f"Строка {r} — это продолжение ссылок, но выше нет позиции. Переместите её сразу под нужную позицию или удалите",
                    "Строка привязки без позиции",
                    "Ошибка",
                    "Строка ссылок на документацию не привязана к позиции работы выше",
                )
            else:
                validate_fgh(val_f, val_g, val_h, r, add_res)

        elif all(value == "" for col, value in enumerate(row_vals) if col not in {0, 9}):
            # A/J-only labels are bookkeeping, not incomplete work rows.
            continue
        # --- ТИП 3: НЕКОРРЕКТНАЯ СТРОКА ---
        else:
            preview = " | ".join([str(v)[:20] for v in row_vals if v != ""])
            add_res(
                f"A{r}-K{r}",
                "ROW_INVALID",
                f"Приведите строку {r} к одному виду: раздел, позиция с наименованием и количеством или продолжение ссылок под позицией",
                preview,
                "Ошибка",
                "Не удалось отнести строку ни к разделу, ни к позиции работы, ни к привязке документов",
            )

    # =========================================================================
    # БЛОК 4. ЗАПРЕТ ДАННЫХ ПРАВЕЕ СТОЛБЦА K (НАЧИНАЯ СО СТОЛБЦА L)
    # =========================================================================
    stray: list[str] = []
    if max_c >= 12:
        for r_check in range(1, max_r + 1):
            for c_check in range(12, max_c + 1):
                val_l = ws.cell(row=r_check, column=c_check).value
                if val_l is not None and str(val_l).strip() != "":
                    stray.append(f"{get_column_letter(c_check)}{r_check}")
    if stray:
        rows = sorted({int(re.sub(r"\D", "", cell)) for cell in stray})
        cols = sorted({re.sub(r"\d", "", cell) for cell in stray})
        where = f"столбцы {cols[0]}–{cols[-1]}, строки {rows[0]}–{rows[-1]}"
        add_res(
            ", ".join(stray),
            "COL_L_LIMIT",
            "Уберите посторонние данные правее столбца K",
            f"{len(stray)} ячеек: {where}",
            "Ошибка",
            "Справа от таблицы (столбец L и дальше) не должно быть данных — "
            "обычно это забытые расчёты. Удалите их или перенесите на другой лист",
        )

    return results


def validate_fgh(val_f, val_g, val_h, r, add_res):
    """Валидация колонок ссылок на документацию, файлов и страниц."""
    # F: ссылка на документацию
    if val_f != "":
        if len(str(val_f).strip()) == 0:
            add_res(
                f"F{r}",
                "LINK_CHECK",
                f"В ячейку F{r} впишите ссылку на документацию или очистите её",
                val_f,
                "Ошибка",
                "Ссылка на документацию пуста",
            )

    # G: путь к файлу / H: страницы
    if val_g != "":
        if val_h == "":
            add_res(
                f"H{r}",
                "LINK_CHECK",
                f"В ячейку H{r} впишите номера страниц из файла — например 9 10",
                "<Пусто>",
                "Внимание",
                "Указан файл в столбце G, но не заполнены номера страниц в столбце H",
            )

    if val_h != "":
        h_str = str(val_h).strip()
        # Числа, разделенные строго одним пробелом
        if bool(re.match(r"^\d+( \d+)*$", h_str)):
            add_res(f"H{r}", "LINK_CHECK", "Числа через один пробел", h_str, "ОК")
        else:
            add_res(
                f"H{r}",
                "LINK_CHECK",
                f"В ячейке H{r} оставьте только номера страниц через один пробел — например 9 10",
                h_str,
                "Ошибка",
                "Номера страниц должны быть только числами, разделёнными одним пробелом (например: '9 10')",
            )


def get_highlighted_workbook(file_source, rule_results, sheet_name=None):
    """Return the workbook with findings painted and commented.

    Errors get a red fill, warnings a yellow one; every painted cell
    carries an Excel comment with «что не так / как исправить».
    Group coordinates (``L18, L22, …``) paint each cell; ranges
    (``B12-K12``) paint the row span. Never raises: on failure the
    original bytes come back unpainted.
    """
    from openpyxl.comments import Comment

    try:
        if isinstance(file_source, str):
            raw = open(file_source, "rb").read()
        elif hasattr(file_source, "getvalue"):
            raw = file_source.getvalue()
        else:
            file_source.seek(0)
            raw = file_source.read()
            file_source.seek(0)
        wb = openpyxl.load_workbook(io.BytesIO(raw))
        if sheet_name is None or str(sheet_name).strip() == "":
            ws = wb[wb.sheetnames[0]]
        else:
            ws = wb[sheet_name]
        red = openpyxl.styles.PatternFill(
            start_color="FCE8E9", end_color="FCE8E9", fill_type="solid"
        )
        yellow = openpyxl.styles.PatternFill(
            start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"
        )

        def cells_for(coord):
            found = []
            for chunk in str(coord or "").replace("…", "").split(","):
                chunk = chunk.strip()
                span = re.match(r"^([A-Z]+)(\d+)-([A-Z]+)(\d+)$", chunk)
                if span:
                    c_start, r_start, c_end, _ = span.groups()
                    idx1 = openpyxl.utils.column_index_from_string(c_start)
                    idx2 = openpyxl.utils.column_index_from_string(c_end)
                    for col_idx in range(idx1, idx2 + 1):
                        found.append(ws.cell(row=int(r_start), column=col_idx))
                elif re.match(r"^[A-Z]+\d+$", chunk):
                    try:
                        found.append(ws[chunk])
                    except Exception:
                        continue
            return found

        for item in rule_results or []:
            if item.get("status") not in {"Ошибка", "Внимание"}:
                continue
            fill = red if item.get("status") == "Ошибка" else yellow
            note = str(item.get("error") or "").strip()
            fix = str(item.get("expected") or "").strip()
            text = note + (f"\nКак исправить: {fix}" if fix else "")
            for cell in cells_for(item.get("coord", "")):
                cell.fill = fill
                if text:
                    try:
                        cell.comment = Comment(text, "Проверка ВОР")
                    except Exception:
                        pass
        out = io.BytesIO()
        wb.save(out)
        painted = out.getvalue()
        return painted if painted else raw
    except Exception:
        try:
            if isinstance(file_source, str):
                with open(file_source, "rb") as handle:
                    return handle.read()
            if hasattr(file_source, "getvalue"):
                return file_source.getvalue()
        except Exception:
            pass
        return b""


def generate_rules_word_report(filename, rule_results, sheet=""):
    """Word-отчёт для внешних проверяющих: те же колонки, что в интерфейсе.

    Группы идут в порядке важности, внутри — ячейки с текстами
    «что не так / как исправить». Без доступа к системе отчёт читается сам.
    """
    issues, errors = summarize_format_rows(rule_results)
    warnings = len(issues) - errors
    doc = docx.Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Inches(0.7)
        section.left_margin = section.right_margin = Inches(0.7)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("Проверка оформления ведомости объёмов работ")
    run.bold = True
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(30, 41, 59)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta = f"Файл: {filename}" + (f" · лист «{sheet}»" if sheet else "")
    run = sub.add_run(meta)
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(100, 116, 139)

    verdict = doc.add_paragraph()
    if not issues:
        run = verdict.add_run(
            "Ошибок оформления не обнаружено. Ведомость соответствует шаблону."
        )
        run.bold = True
    else:
        run = verdict.add_run(
            f"Итого: ошибок {errors}, замечаний {warnings}. "
            "Группы упорядочены по важности: сначала позиции и формулы, "
            "затем разделы, затем шапка."
        )
        run.bold = True

    groups: dict[str, list] = {}
    for item in issues:
        groups.setdefault(item.get("group", ""), []).append(item)
    for items in groups.values():
        head = doc.add_paragraph()
        run = head.add_run(f"{items[0].get('group_title', 'Замечания')} — {len(items)}")
        run.bold = True
        run.font.size = Pt(11)
        hint = items[0].get("group_hint", "")
        if hint:
            note = doc.add_paragraph()
            run = note.add_run(hint)
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(100, 116, 139)
        table = doc.add_table(rows=1, cols=3)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.style = "Table Grid"
        headers = ["Ячейка", "Что не так", "Как исправить"]
        widths = [Inches(1.0), Inches(2.7), Inches(3.2)]
        hdr = table.rows[0].cells
        for index, text in enumerate(headers):
            hdr[index].text = text
            hdr[index].paragraphs[0].runs[0].bold = True
            hdr[index].paragraphs[0].runs[0].font.size = Pt(9.5)
            shading = hdr[index]._element.get_or_add_tcPr()
            shading.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="E2E8F0"/>'))
            hdr[index].width = widths[index]
        for item in items:
            cells = table.add_row().cells
            cells[0].text = item.get("coord", "-")
            cells[1].text = item.get("error", "-")
            cells[2].text = item.get("expected", "-")
            fill = "FEE2E2" if item.get("status") == "Ошибка" else "FEF3C7"
            shading = cells[0]._element.get_or_add_tcPr()
            shading.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill}"/>'))
            for index, cell in enumerate(cells):
                cell.width = widths[index]
                for paragraph in cell.paragraphs:
                    for text_run in paragraph.runs:
                        text_run.font.size = Pt(9.0)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
