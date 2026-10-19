import re
import datetime
import openpyxl
from openpyxl.utils import get_column_letter
import io
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

def safe_calc_formula(expr_str):
    """Безопасно вычисляет математическую формулу из строки."""
    clean = str(expr_str).replace('=', '').replace(',', '.').replace('^', '**').strip()
    # Разрешены только цифры, знаки +, -, *, /, скобки, точки и пробелы
    if not re.match(r'^[0-9+\-*/().\s]+$', clean):
        return None, "Формула содержит недопустимые символы или текст"
    try:
        val = eval(clean, {"__builtins__": None}, {})
        return round(float(val), 6), None  # Возвращает точное число
    except Exception as e:
        return None, f"Ошибка вычисления формулы: {e}"

def validate_vor_formatting(file_source, sheet_name=None):
    """
    Комплексная проверка ВОР:
    1. Шапка и служебные ячейки (строки 1–16).
    2. Запрет данных правее столбца K (столбец L и далее).
    3. Запрет слова 'Заголовок' в строке 18.
    4. Динамическая проверка разделов, позиций и строк привязки документов (со строки 17 до конца файла).
    """
    wb = openpyxl.load_workbook(file_source, data_only=True)
    ws = wb[sheet_name] if sheet_name is not None else wb.active
    results = []

    def get_val(r, c):
        v = ws.cell(row=r, column=c).value
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return v

    def add_res(coord, c_type, expected, actual, status, err_msg=""):
        results.append({
            "coord": coord,
            "type": c_type,
            "expected": expected,
            "actual": str(actual).strip() if actual != "" else "<Пусто>",
            "status": status,
            "error": err_msg
        })

    # =========================================================================
    # БЛОК 1. СТРОКИ 1–16 (ШАПКА И РЕКВИЗИТЫ)
    # =========================================================================
    static_rules = [
        # Строка 1
        (1, 1, "STATIC", "Документ", lambda v: v == "Документ", "Должно быть ровно 'Документ'"),
        (1, 4, "STATIC", "Ведомость объемов работ", lambda v: v == "Ведомость объемов работ", "Должно быть ровно 'Ведомость объемов работ'"),
        # Строка 2
        (2, 1, "STATIC", "Версия", lambda v: v == "Версия", "Должно быть ровно 'Версия'"),
        (2, 4, "VAR", "Формат: ЧЧ_ЦЦ (^\d+_\d{2}$)", lambda v: bool(re.match(r'^\d+_\d{2}$', str(v).strip())), "Версия должна быть в формате ЧЧ_ЦЦ, например 3_01"),
        # Строка 4
        (4, 1, "STATIC", "Наименование стройки", lambda v: v == "Наименование стройки", "Должно быть ровно 'Наименование стройки'"),
        (4, 4, "VAR", "Текст (от 5 до 255 символов)", lambda v: 5 <= len(str(v).strip()) <= 255, "Наименование стройки не заполнено или слишком короткое"),
        # Строка 5
        (5, 1, "STATIC", "Наименование объекта капитального строительства", lambda v: v == "Наименование объекта капитального строительства", "Должно быть ровно 'Наименование объекта капитального строительства'"),
        (5, 4, "VAR", "Текст (от 5 до 255 символов)", lambda v: 5 <= len(str(v).strip()) <= 255, "Наименование объекта не заполнено"),
        # Строка 6
        (6, 1, "STATIC", "Ведомость объемов работ №", lambda v: v == "Ведомость объемов работ №", "Должно быть ровно 'Ведомость объемов работ №'"),
        # Строка 7
        (7, 1, "STATIC", "Основание((наименование раздела (подраздела) ПД))", lambda v: v == "Основание((наименование раздела (подраздела) ПД))", "Должно быть ровно 'Основание((наименование раздела (подраздела) ПД))'"),
        # Строка 8
        (8, 1, "STATIC", "Дата составления", lambda v: v == "Дата составления", "Должно быть ровно 'Дата составления'"),
        # Строка 10
        (10, 1, "STATIC", "Составил ФИО", lambda v: v == "Составил ФИО", "Должно быть ровно 'Составил ФИО'"),
        (10, 4, "VAR", "Формат: Фамилия И.О. (^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$)", lambda v: bool(re.match(r'^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$', str(v).strip())), "ФИО должно быть в формате Фамилия И.О., например Иванова И.И."),
        # Строка 11
        (11, 1, "STATIC", "Составил должность", lambda v: v == "Составил должность", "Должно быть ровно 'Составил должность'"),
        (11, 4, "VAR", "Текст (от 3 до 100 символов)", lambda v: 3 <= len(str(v).strip()) <= 100, "Должность составителя не заполнена"),
        # Строка 12
        (12, 1, "STATIC", "Проверил ФИО", lambda v: v == "Проверил ФИО", "Должно быть ровно 'Проверил ФИО'"),
        (12, 4, "VAR", "Формат: Фамилия И.О. (^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$)", lambda v: bool(re.match(r'^[А-ЯЁ][а-яё-]+ [А-ЯЁ]\.[А-ЯЁ]\.$', str(v).strip())), "ФИО проверившего должно быть в формате Фамилия И.О., например Крекова Т.Л."),
        # Строка 13
        (13, 1, "STATIC", "Проверил должность", lambda v: v == "Проверил должность", "Должно быть ровно 'Проверил должность'"),
        (13, 4, "VAR", "Текст (от 2 до 100 символов)", lambda v: 2 <= len(str(v).strip()) <= 100, "Должность проверившего не заполнена"),
        # Строка 15 (Заголовки колонок)
        (15, 1, "STATIC", "№ п.п.", lambda v: v == "№ п.п.", "Должно быть '№ п.п.'"),
        (15, 2, "STATIC", "Наименование работ, ресурсов, затрат по проекту", lambda v: v == "Наименование работ, ресурсов, затрат по проекту", "Неверный заголовок столбца"),
        (15, 3, "STATIC", "Ед. изм.", lambda v: v == "Ед. изм.", "Должно быть 'Ед. изм.'"),
        (15, 4, "STATIC", "Объем работ / Количество", lambda v: v == "Объем работ / Количество", "Должно быть 'Объем работ / Количество'"),
        (15, 5, "STATIC", "Формула расчета объемов работ и расхода материалов, потребности ресурсов", lambda v: v == "Формула расчета объемов работ и расхода материалов, потребности ресурсов", "Неверный заголовок столбца"),
        (15, 6, "STATIC", "Ссылка на чертежи, спецификации в проектной документации", lambda v: v == "Ссылка на чертежи, спецификации в проектной документации", "Неверный заголовок столбца"),
        (15, 7, "STATIC", "Наименование файла", lambda v: v == "Наименование файла", "Должно быть 'Наименование файла'"),
        (15, 8, "STATIC", "Номера страниц (через пробел)", lambda v: v == "Номера страниц (через пробел)", "Должно быть 'Номера страниц (через пробел)'"),
        (15, 9, "STATIC", "Дополнительная информация (комментарий).", lambda v: v == "Дополнительная информация (комментарий).", "Должно быть 'Дополнительная информация (комментарий).'"),
        (15, 10, "STATIC", "Идентификатор", lambda v: v == "Идентификатор", "Должно быть 'Идентификатор'"),
        (15, 11, "STATIC", "Тип позиции", lambda v: v == "Тип позиции", "Должно быть 'Тип позиции'"),
        # Строка 16 (Номера столбцов 1..9)
        (16, 1, "STATIC", "1", lambda v: str(v).strip() == "1", "Должно быть '1'"),
        (16, 2, "STATIC", "2", lambda v: str(v).strip() == "2", "Должно быть '2'"),
        (16, 3, "STATIC", "3", lambda v: str(v).strip() == "3", "Должно быть '3'"),
        (16, 4, "STATIC", "4", lambda v: str(v).strip() == "4", "Должно быть '4'"),
        (16, 5, "STATIC", "5", lambda v: str(v).strip() == "5", "Должно быть '5'"),
        (16, 6, "STATIC", "6", lambda v: str(v).strip() == "6", "Должно быть '6'"),
        (16, 7, "STATIC", "6.1", lambda v: str(v).strip() == "6.1", "Должно быть '6.1'"),
        (16, 8, "STATIC", "6.2", lambda v: str(v).strip() == "6.2", "Должно быть '6.2'"),
        (16, 9, "STATIC", "7", lambda v: str(v).strip() == "7", "Должно быть '7'"),
        (16, 10, "STATIC", "8", lambda v: str(v).strip() == "8", "Должно быть '8'"),
        (16, 11, "STATIC", "9", lambda v: str(v).strip() == "9", "Должно быть '9'"),
    ]

    for r_idx, c_idx, c_type, exp_desc, validator, err_t in static_rules:
        coord = f"{get_column_letter(c_idx)}{r_idx}"
        val = get_val(r_idx, c_idx)
        if val == "":
            add_res(coord, c_type, exp_desc, val, "Ошибка", f"Ячейка пуста. {err_t}")
        elif validator(val):
            add_res(coord, c_type, exp_desc, val, "ОК")
        else:
            add_res(coord, c_type, exp_desc, val, "Ошибка", err_t)

    # D6: Номер ВОР
    val_d6 = get_val(6, 4)
    if val_d6 == "":
        add_res("D6", "VAR", "Формат: В.X.X (^В\.\d+(\.\d+)*$)", val_d6, "Ошибка", "Номер ВОР не заполнен")
    elif bool(re.match(r'^В\.\d+(\.\d+)*$', str(val_d6).strip())):
        add_res("D6", "VAR", "Формат: В.X.X (^В\.\d+(\.\d+)*$)", val_d6, "ОК")
    else:
        add_res("D6", "VAR", "Формат: В.X.X (^В\.\d+(\.\d+)*$)", val_d6, "Внимание", f"Нетиповой формат номера ВОР: '{val_d6}'. Проверьте соответствие шифру проекта/имени файла.")

    # D7: Основание
    val_d7 = get_val(7, 4)
    if val_d7 == "":
        add_res("D7", "VAR", "Формат: Раздел ПД № X (^Раздел ПД № \d+(\.\d+)*$)", val_d7, "Ошибка", "Основание не заполнено")
    elif bool(re.match(r'^Раздел ПД № \d+(\.\d+)*$', str(val_d7).strip())):
        add_res("D7", "VAR", "Формат: Раздел ПД № X (^Раздел ПД № \d+(\.\d+)*$)", val_d7, "ОК")
    else:
        add_res("D7", "VAR", "Формат: Раздел ПД № X (^Раздел ПД № \d+(\.\d+)*$)", val_d7, "Внимание", f"Нетиповой формат основания: '{val_d7}'. Проверьте соответствие шифру раздела ПД.")

    # D8: Дата составления
    val_d8 = ws.cell(row=8, column=4).value
    if val_d8 is None or str(val_d8).strip() == "":
        add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", "", "Ошибка", "Дата составления не заполнена")
    elif isinstance(val_d8, (datetime.date, datetime.datetime)):
        if 2000 <= val_d8.year <= 2100:
            add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", val_d8.strftime("%d.%m.%Y"), "ОК")
        else:
            add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", val_d8.strftime("%d.%m.%Y"), "Ошибка", "Год даты вне диапазона 2000–2100")
    else:
        try:
            d_p = datetime.datetime.strptime(str(val_d8).strip(), "%d.%m.%Y")
            if 2000 <= d_p.year <= 2100:
                add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", str(val_d8).strip(), "ОК")
            else:
                add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", str(val_d8).strip(), "Ошибка", "Год даты вне диапазона 2000–2100")
        except Exception:
            add_res("D8", "VAR", "Дата ДД.ММ.ГГГГ (2000–2100 гг.)", str(val_d8).strip(), "Ошибка", "Дата составления должна быть в формате ДД.ММ.ГГГГ, например 24.03.2025")

    # =========================================================================
    # БЛОК 2. ЗАПРЕТ СЛОВА 'ЗАГОЛОВОК' В СТРОКЕ 18
    # =========================================================================
    for col_i in range(1, 12):
        v_18 = get_val(18, col_i)
        if "заголовок" in str(v_18).lower():
            add_res(f"{get_column_letter(col_i)}18", "HEADER_BAN", "Не содержит 'Заголовок'", v_18, "Ошибка", "Строка 18 не должна содержать текст 'Заголовок'")

    # =========================================================================
    # БЛОК 3. ДИНАМИЧЕСКАЯ ПРОВЕРКА СТРОК (НАЧИНАЯ С 17 И ДО КОНЦА)
    # =========================================================================
    max_r = ws.max_row
    max_c = ws.max_column
    empty_streak = 0
    has_active_position = False

    # Предварительно собираем все идентификаторы из колонки J и считаем их частоту
    from collections import Counter
    all_j_ids = []
    for r_scan in range(18, max_r + 1):
        val_scan = str(ws.cell(row=r_scan, column=10).value or "").strip()
        val_a_scan = str(ws.cell(row=r_scan, column=1).value or "").strip()
        # Учитываем только строки позиций
        if val_a_scan.isdigit() and val_scan:
            all_j_ids.append(val_scan)
    id_frequencies = Counter(all_j_ids)

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
        val_a, val_b, val_c, val_d, val_e, val_f, val_g, val_h, val_i, val_j, val_k = row_vals

        # --- ТИП 1: СТРОКА РАЗДЕЛА ---
        # Получаем объект ячейки A и проверяем жирный шрифт
        cell_a = ws.cell(row=r, column=1)
        is_bold = bool(cell_a.font and cell_a.font.bold)
        val_a_clean = str(val_a).strip()

        # Признаки того, что строка является разделом:
        # 1) Начинается со слова "Раздел" (с двоеточием или без, в любом регистре)
        # 2) Либо ячейка жирная (bold), а остальные рабочие колонки (B, C, D, K) пусты
        is_section_candidate = bool(re.match(r'^раздел', val_a_clean, re.IGNORECASE)) or (
                is_bold and val_b == "" and val_c == "" and val_d == "" and val_k == "" and len(val_a_clean) > 3
        )

        # --- ТИП 1: СТРОКА РАЗДЕЛА ---
        if is_section_candidate:
            has_active_position = False

            # 1. Проверка формата текста по регламенту: ^Раздел: \d+\. .+$
            format_ok = bool(re.match(r'^Раздел: \d+\. .+$', val_a_clean))

            if not format_ok:
                # Четкая подсказка, если забыли двоеточие или номер
                if re.match(r'^Раздел \d+', val_a_clean):
                    err_desc = 'Пропущено двоеточие. Должно быть строго: "Раздел: 1. Название раздела"'
                else:
                    err_desc = 'Заголовок раздела должен быть в формате "Раздел: 1. Название раздела"'
                add_res(f"A{r}", "SECTION", 'Формат: "Раздел: X. Название"', val_a, "Ошибка", err_desc)
            else:
                add_res(f"A{r}", "SECTION", 'Формат: "Раздел: X. Название"', val_a, "ОК")

            # 2. Проверка оформления: шрифт ОБЯЗАН быть жирным (bold)
            if not is_bold:
                add_res(f"A{r}", "SECTION_STYLE", "Полужирный шрифт (Bold)", "Обычный шрифт", "Внимание",
                        "Заголовок раздела должен быть выделен полужирным шрифтом (Bold)")

            # 3. Проверка, что колонки B–K пусты
            filled_bk = [get_column_letter(ci + 1) for ci, v in enumerate(row_vals[1:], start=1) if v != ""]
            if filled_bk:
                add_res(f"B{r}-K{r}", "SECTION_EMPTY", "Колонки B–K пусты", f"Заполнены: {', '.join(filled_bk)}",
                        "Внимание", "В строке раздела колонки B–K должны быть пустыми")

        # --- ТИП 2: ОСНОВНАЯ СТРОКА ПОЗИЦИИ ---
        elif str(val_a).strip().isdigit():
            has_active_position = True

            # A: номер позиции
            add_res(f"A{r}", "ROW_POS", "Целое число (1, 2, 3...)", val_a, "ОК")

            # B: наименование
            if val_b == "":
                add_res(f"B{r}", "ROW_POS", "Наименование не пусто", val_b, "Ошибка", "Наименование работы/ресурса не заполнено")

            # C: ед. изм.
            if val_c == "":
                add_res(f"C{r}", "ROW_POS", "Ед. изм. не пусто", val_c, "Ошибка", "Единица измерения не заполнена")

            # --- D: ОБЪЕМ С УЧЕТОМ СЛЭША (/) КАК "ИЛИ" ---
            vol_raw = str(val_d).strip().replace(',', '.')
            vol_parts = []
            if "/" in vol_raw:
                # Слэш означает альтернативные единицы/значения (например 9/45 или 3/6)
                for part in vol_raw.split('/'):
                    try:
                        vol_parts.append(float(part.strip()))
                    except Exception:
                        pass
                if vol_parts:
                    add_res(f"D{r}", "ROW_POS", "Число > 0 (или через /)", val_d, "ОК")
                else:
                    add_res(f"D{r}", "ROW_POS", "Число > 0", val_d, "Ошибка", "Некорректное значение объема")
            else:
                try:
                    vol_num = float(vol_raw)
                    if vol_num > 0:
                        vol_parts.append(vol_num)
                        add_res(f"D{r}", "ROW_POS", "Число > 0", val_d, "ОК")
                    else:
                        add_res(f"D{r}", "ROW_POS", "Число > 0", val_d, "Ошибка", "Объем должен быть больше 0")
                except Exception:
                    add_res(f"D{r}", "ROW_POS", "Число > 0", val_d, "Ошибка", "Объем должен быть числом больше 0")

            # --- E: ФОРМУЛА И СВЕРКА С ОБЪЕМОМ ---
            # Если формула не заполнена, пропускаем без ошибок
            if val_e != "" and str(val_e).strip() != "":
                calc_result, calc_err = safe_calc_formula(val_e)
                if calc_err:
                    add_res(f"E{r}", "ROW_POS", "Математическая формула без текста", val_e, "Ошибка", calc_err)
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
                        if abs(round(calc_v, 2) - vol_v) < 0.001 or abs(round(calc_v, 3) - vol_v) < 0.0001:
                            return True
                        # Округление вверх до сотых: ceil(X * 100) / 100
                        if abs(math.ceil(calc_v * 100) / 100 - vol_v) < 0.001:
                            return True
                        # Округление до десятых (0.1)
                        if abs(round(calc_v, 1) - vol_v) < 0.01 or abs(
                                math.ceil(calc_v * 10) / 10 - vol_v) < 0.01:
                            return True
                        # Округление до целого (78.9 -> 79, 281.85 -> 282)
                        if round(calc_v) == int(round(vol_v)) or math.ceil(calc_v) == int(round(vol_v)):
                            return True
                        return False

                    match_vol = any(is_volume_match(calc_result, vp) for vp in vol_parts)

                    # Безопасное формирование отображения объема (без вызова .is_integer(), который ломал код)
                    if vol_parts:
                        vp_val = vol_parts[0]
                        val_d_display = str(int(round(vp_val))) if float(vp_val).is_integer() else str(
                            round(vp_val, 3))
                    else:
                        val_d_display = str(val_d)

                    calc_display = str(round(calc_result, 3))

                    if match_vol:
                        add_res(f"E{r}", "ROW_POS", f"Результат формулы = {val_d_display}",
                                f"{val_e} = {calc_display}", "ОК")
                    else:
                        add_res(
                            f"E{r}",
                            "ROW_POS",
                            f"Результат формулы равен объему {val_d_display}",
                            f"{val_e} = {calc_display}",
                            "Ошибка",
                            f"Результат формулы ({calc_display}) не совпадает с объемом в графе D ({val_d_display})"
                        )

            # --- G: ПРОВЕРКА ПУТИ К ФАЙЛУ ---
            if val_g != "":
                clean_path = str(val_g).strip().strip('"\'«»')
                if not clean_path.lower().endswith('.pdf'):
                    add_res(f"G{r}", "ROW_POS", "Имя файла PDF (*.pdf)", val_g, "Внимание",
                            "Путь к файлу должен оканчиваться на .pdf")
                else:
                    add_res(f"G{r}", "ROW_POS", "Корректный путь к файлу", val_g, "ОК")

            # --- J: ИДЕНТИФИКАТОР (НЕ ПУСТО И БЕЗ ПОВТОРОВ) ---
            id_clean = str(val_j).strip()
            if not id_clean:
                add_res(f"J{r}", "ROW_POS", "Идентификатор заполнен", "<Пусто>", "Ошибка",
                        "Идентификатор не заполнен (обязателен для каждой позиции)")
            elif id_frequencies.get(id_clean, 0) > 1:
                add_res(f"J{r}", "ROW_POS", "Уникальный идентификатор", id_clean, "Ошибка",
                        f"Дубликат идентификатора: '{id_clean}' встречается {id_frequencies[id_clean]} раз(а) в документе")
            else:
                add_res(f"J{r}", "ROW_POS", "Уникальный идентификатор", id_clean, "ОК")

            # K: тип позиции (не пусто)
            if val_k == "":
                add_res(f"K{r}", "ROW_POS", "Тип позиции не пусто", val_k, "Ошибка",
                        "Тип позиции не заполнен (ожидается: Работа, Материал и т.д.)")

        # --- ТИП 2b: СТРОКА ДОПОЛНИТЕЛЬНОЙ ПРИВЯЗКИ ДОКУМЕНТОВ К ПОЗИЦИИ (F, G, H) ---
        elif val_a == "" and val_b == "" and val_c == "" and val_d == "" and val_k == "" and any(v != "" for v in [val_f, val_g, val_h]):
            if not has_active_position:
                add_res(f"A{r}-K{r}", "SUB_LINK", "Привязка к позиции", "Строка привязки без позиции", "Ошибка", "Строка ссылок на документацию не привязана к позиции работы выше")
            else:
                validate_fgh(val_f, val_g, val_h, r, add_res)

        # --- ТИП 3: НЕКОРРЕКТНАЯ СТРОКА ---
        else:
            preview = " | ".join([str(v)[:20] for v in row_vals if v != ""])
            add_res(f"A{r}-K{r}", "ROW_INVALID", "Корректная строка раздела/позиции", preview, "Ошибка", "Не удалось отнести строку ни к разделу, ни к позиции работы, ни к привязке документов")

    # =========================================================================
    # БЛОК 4. ЗАПРЕТ ДАННЫХ ПРАВЕЕ СТОЛБЦА K (НАЧИНАЯ СО СТОЛБЦА L)
    # =========================================================================
    if max_c >= 12:
        for r_check in range(1, max_r + 1):
            for c_check in range(12, max_c + 1):
                val_l = ws.cell(row=r_check, column=c_check).value
                if val_l is not None and str(val_l).strip() != "":
                    add_res(
                        f"{get_column_letter(c_check)}{r_check}",
                        "COL_L_LIMIT",
                        "Пусто (столбец L и правее не используются)",
                        str(val_l)[:30],
                        "Ошибка",
                        "Начиная со столбца L не должно содержаться никаких данных"
                    )

    return results

def validate_fgh(val_f, val_g, val_h, r, add_res):
    """Валидация колонок ссылок на документацию, файлов и страниц."""
    # F: ссылка на документацию
    if val_f != "":
        if len(str(val_f).strip()) == 0:
            add_res(f"F{r}", "LINK_CHECK", "Непустой текст", val_f, "Ошибка", "Ссылка на документацию пуста")

    # G: путь к файлу / H: страницы
    if val_g != "":
        if val_h == "":
            add_res(f"H{r}", "LINK_CHECK", "Номера страниц заполнены", "<Пусто>", "Внимание", "Указан файл в столбце G, но не заполнены номера страниц в столбце H")

    if val_h != "":
        h_str = str(val_h).strip()
        # Числа, разделенные строго одним пробелом
        if bool(re.match(r'^\d+( \d+)*$', h_str)):
            add_res(f"H{r}", "LINK_CHECK", "Числа через один пробел", h_str, "ОК")
        else:
            add_res(f"H{r}", "LINK_CHECK", "Числа через один пробел", h_str, "Ошибка", "Номера страниц должны быть только числами, разделёнными одним пробелом (например: '9 10')")

def get_highlighted_workbook(file_source, rule_results):
    """
    Загружает исходный файл ВОР, красит ячейки с ошибками/замечаниями в желтый цвет
    и гарантированно возвращает байты Excel-файла.
    """
    try:
        # 1. Загрузка книги
        if isinstance(file_source, str):
            wb = openpyxl.load_workbook(file_source)
        elif hasattr(file_source, 'getvalue'):
            wb = openpyxl.load_workbook(io.BytesIO(file_source.getvalue()))
        else:
            file_source.seek(0)
            wb = openpyxl.load_workbook(io.BytesIO(file_source.read()))
            file_source.seek(0)

        ws = wb.active
        yellow_fill = openpyxl.styles.PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

        # 2. Покраска ячеек с ошибками/замечаниями
        for r in rule_results:
            if r.get("status") in ["Ошибка", "Внимание"]:
                coord = r.get("coord", "")
                if "-" in coord:
                    m = re.match(r'([A-Z]+)(\d+)-([A-Z]+)(\d+)', coord)
                    if m:
                        c_start, r_start, c_end, _ = m.groups()
                        idx1 = openpyxl.utils.column_index_from_string(c_start)
                        idx2 = openpyxl.utils.column_index_from_string(c_end)
                        for col_idx in range(idx1, idx2 + 1):
                            ws.cell(row=int(r_start), column=col_idx).fill = yellow_fill
                else:
                    try:
                        ws[coord].fill = yellow_fill
                    except Exception:
                        pass

        # 3. Сохранение в поток байтов (строго на внешнем уровне отступов)
        out = io.BytesIO()
        wb.save(out)
        result_bytes = out.getvalue()
        if result_bytes:
            return result_bytes
    except Exception:
        pass

    # Резервный возврат: если покраска не удалась, возвращаем исходный файл без изменений
    if hasattr(file_source, 'getvalue'):
        return file_source.getvalue()
    elif isinstance(file_source, str):
        with open(file_source, 'rb') as f:
            return f.read()
    return b""

def generate_rules_word_report(filename, rule_results):
    """
    Генерирует официальный Word-отчет ТОЛЬКО с ошибками и замечаниями.
    Таблица из 3 колонок: Адрес ячейки, Фактическое значение и широкий Комментарий.
    """
    doc = docx.Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = s.left_margin = s.right_margin = Inches(0.7)

    # Заголовок
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_t = title.add_run("ВЕДОМОСТЬ ОШИБОК И ЗАМЕЧАНИЙ ОФОРМЛЕНИЯ ВОР")
    r_t.bold = True
    r_t.font.size = Pt(14)
    r_t.font.color.rgb = RGBColor(30, 41, 59)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_s = sub.add_run(f"Файл: {filename}")
    r_s.font.size = Pt(10)
    r_s.font.color.rgb = RGBColor(100, 116, 139)

    # Фильтруем только ошибки и замечания (без строк ОК)
    issues = [r for r in rule_results if r.get("status") in ["Ошибка", "Внимание"]]

    p_stat = doc.add_paragraph()
    p_stat.add_run(f"Всего выявлено несоответствий: {len(issues)}\n").bold = True

    if not issues:
        p_ok = doc.add_paragraph()
        p_ok.add_run("Ошибок оформления не обнаружено. Шаблон полностью соответствует регламенту.")
    else:
        # Таблица из 3 колонок
        table = doc.add_table(rows=1, cols=3)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.style = 'Table Grid'

        headers = ["Адрес ячейки", "Фактическое значение", "Комментарий / Ошибка"]
        col_widths = [Inches(1.1), Inches(1.8), Inches(4.0)]

        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            hdr_cells[i].text = h
            hdr_cells[i].paragraphs[0].runs[0].bold = True
            hdr_cells[i].paragraphs[0].runs[0].font.size = Pt(9.5)
            tcPr = hdr_cells[i]._element.get_or_add_tcPr()
            tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="E2E8F0"/>'))
            hdr_cells[i].width = col_widths[i]

        for item in issues:
            row_cells = table.add_row().cells
            row_cells[0].text = item.get("coord", "-")
            row_cells[1].text = item.get("actual", "-")
            row_cells[2].text = item.get("error", "-")

            st_val = item.get("status", "")
            fill_hex = "FEE2E2" if st_val == "Ошибка" else "FEF3C7"
            tcPr = row_cells[0]._element.get_or_add_tcPr()
            tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>'))

            for i, cell in enumerate(row_cells):
                cell.width = col_widths[i]
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9.0)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
