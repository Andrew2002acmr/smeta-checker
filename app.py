import io
import os
import glob
import json
import re
import time
import platform
import subprocess
import tempfile
import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from google import genai
from google.genai import types
import openai
from dotenv import load_dotenv
load_dotenv()
from vor_rules import validate_vor_formatting, get_highlighted_workbook, generate_rules_word_report

def get_setting(name: str, default: str = "") -> str:
    """Сначала читаем переменную окружения, затем Streamlit Secrets."""
    value = os.getenv(name)
    if value:
        return value

    try:
        value = st.secrets.get(name, default)
        return str(value) if value else default
    except (FileNotFoundError, KeyError):
        return default

YANDEX_FOLDER = get_setting("YANDEX_CLOUD_FOLDER")
YANDEX_API_KEY = get_setting("YANDEX_CLOUD_API_KEY")
YANDEX_MODEL_NAME = get_setting(
    "YANDEX_CLOUD_MODEL",
    "qwen3.6-35b-a3b/latest"
)
DEFAULT_GEMINI_KEY = get_setting("GEMINI_API_KEY")

st.set_page_config(page_title="Аудит ВОР и ПД", layout="wide")
st.title("🏗️ Комплексный аудит ведомостей объемов работ (ВОР)")

# ----------------- SESSION STATE -----------------
if "audit_results" not in st.session_state:
    st.session_state.audit_results = {}
if "pdf_pool" not in st.session_state:
    st.session_state.pdf_pool = {}          # { clean_name: fitz.Document }
if "pdf_local_paths" not in st.session_state:
    st.session_state.pdf_local_paths = {}   # { clean_name: full_path_str }
if "pdf_page_maps" not in st.session_state:
    st.session_state.pdf_page_maps = {}     # { clean_name: { sheet: phys_idx } }

# ----------------- БОКОВАЯ ПАНЕЛЬ -----------------
with st.sidebar:
    st.header("⚙️ Нейросеть")

    provider = st.selectbox(
        "Провайдер",
        options=["Google Gemini", "Yandex Cloud (Qwen)"],
        index=0
    )

    if provider == "Google Gemini":
        model_name = st.selectbox(
            "Модель",
            options=["gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
            index=0
        )
        api_key = DEFAULT_GEMINI_KEY
    else:
        model_name = st.selectbox(
            "Модель",
            options=[YANDEX_MODEL_NAME, "yandexgpt/latest"],
            index=0
        )
        api_key = YANDEX_API_KEY

    st.markdown("---")
    st.header("📂 Источник данных")
    source_mode = st.radio("Режим работы", ["📁 Локальная папка проекта", "📤 Ручная загрузка файлов"])

# ----------------- СЛУЖЕБНЫЕ УТИЛИТЫ -----------------

def open_pdf_in_system_viewer(file_source, filename, page_num=1):
    """
    Открывает PDF в Microsoft Edge на Windows на нужной странице (#page=N).
    Поддерживает: локальные пути, UploadedFile из Streamlit и fitz.Document.
    """
    try:
        temp_dir = os.path.join(tempfile.gettempdir(), "audit_pdf_cache")
        os.makedirs(temp_dir, exist_ok=True)
        abs_path = os.path.join(temp_dir, filename)

        # 1. Если это путь к файлу на диске
        if isinstance(file_source, str) and os.path.exists(file_source):
            abs_path = os.path.abspath(file_source)
        # 2. Если это объект из Streamlit file_uploader
        elif hasattr(file_source, 'getbuffer'):
            with open(abs_path, 'wb') as f:
                f.write(file_source.getbuffer())
        # 3. Если это объект fitz.Document
        elif hasattr(file_source, 'save'):
            file_source.save(abs_path)

        system = platform.system()
        if system == "Windows":
            # Edge гарантированно поддерживает синтаксис #page=N
            cmd = f'start msedge "{abs_path}#page={page_num}"'
            subprocess.Popen(cmd, shell=True)
            return True
        elif system == "Darwin":
            subprocess.Popen(['open', abs_path])
            return True
        else:
            subprocess.Popen(['xdg-open', abs_path])
            return True
    except Exception as e:
        st.error(f"Не удалось открыть PDF: {e}")
        return False


def prepare_df_for_display(df):
    """Предотвращает ошибки PyArrow (ArrowInvalid при '9 10')."""
    clean_df = df.copy()
    for col in clean_df.columns:
        clean_df[col] = clean_df[col].fillna('-').astype(str)
        clean_df[col] = clean_df[col].apply(lambda x: x[:-2] if x.endswith('.0') and x[:-2].isdigit() else x)
    return clean_df


def load_vor_with_metadata(file_source, filename):
    meta = {
        "object_name": "Не указано",
        "doc_number": filename,
        "basis_code": "Проектная документация"
    }

    # 1. Безопасное чтение файла в байты (защита от блокировок и смещения потока)
    if isinstance(file_source, str):
        with open(file_source, 'rb') as f:
            content = f.read()
    elif hasattr(file_source, 'getvalue'):
        content = file_source.getvalue()
    else:
        file_source.seek(0)
        content = file_source.read()
        file_source.seek(0)

    # 2. Извлечение строк для чтения шапки
    if filename.endswith(".csv"):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('cp1251', errors='ignore')
        lines = text.splitlines()
    else:
        # Передаем BytesIO, чтобы Excel-файл можно было перечитывать многократно
        df_raw = pd.read_excel(io.BytesIO(content), header=None)
        lines = ["\t".join([str(v) for v in row if pd.notna(v)]) for _, row in df_raw.iterrows()]

    # Чтение метаданных из шапки
    for line in lines[:20]:
        line_clean = line.replace('"', '').replace(';', '  ')
        if "наименование стройки" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split('  ') if p.strip()]
            if len(parts) > 1:
                meta["object_name"] = parts[-1]
        elif "ведомость объемов работ №" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split('  ') if p.strip()]
            if len(parts) > 1:
                meta["doc_number"] = parts[-1]
        elif "основание" in line_clean.lower():
            parts = [p.strip() for p in line_clean.split('  ') if p.strip()]
            if len(parts) > 1:
                meta["basis_code"] = parts[-1]

    header_idx = -1
    for idx, line in enumerate(lines):
        ll = line.lower()
        if '№ п.п.' in ll or 'наименование работ' in ll:
            header_idx = idx
            break

    if header_idx == -1:
        header_idx = 0

    # 3. Чтение итоговой таблицы
    if filename.endswith(".csv"):
        delimiter = ';' if ';' in lines[header_idx] else ','
        df = pd.read_csv(io.StringIO("\n".join(lines)), skiprows=header_idx, sep=delimiter)
    else:
        df = pd.read_excel(io.BytesIO(content), skiprows=header_idx)

    df.columns = [re.sub(r'\s+', ' ', str(c)).strip() for c in df.columns]

    def is_work_row(row):
        col_pp = str(row.iloc[0]).strip()
        col_name = str(row.iloc[1]).strip()
        col_type = str(row.get('Тип позиции', '')).strip().lower()

        # 1. Отсекаем техническую строку номеров колонок 1; 2; 3...
        if col_pp == '1' and col_name == '2':
            return False
        # 2. Основные позиции работ
        if col_type in ['работа', 'материал', 'оборудование']:
            return True
        if re.match(r'^\d+$', col_pp) and len(col_name) > 3 and not col_name.isdigit():
            return True
        # 3. НЕ ОТСЕКАЕМ строки-продолжения (где указан второй файл .pdf к этой же позиции)
        row_str = " ".join([str(val) for val in row.values if pd.notna(val)])
        if '.pdf' in row_str.lower():
            return True

        return False

    df_clean = df[df.apply(is_work_row, axis=1)].copy()
    return df_clean, meta


def is_pure_graphic_page(page):
    text = page.get_text().strip().lower()
    tabs = page.find_tables()
    if len(tabs.tables) > 0:
        return False
    graphic_markers = ['сейсмограмм', 'рисунок ', 'схема расположения', 'фотография', 'карта фактического']
    if (len(text) < 150 or any(m in text for m in graphic_markers)) and "таблица" not in text:
        return True
    return False


def build_pdf_page_map(doc):
    page_map = {}
    for idx, page in enumerate(doc):
        if page.rotation != 0:
            page.set_rotation(0)
        text = page.get_text()
        match = re.search(r'(\d+)\s+из\s+\d+', text)
        if match:
            sheet_num = match.group(1).strip()
            page_map[sheet_num] = idx
    return page_map


def get_optimized_page_indices(doc, page_map, page_str):
    """
    Ищет страницы СТРОГО относительно PDF-файла (без привязки к штампам).
    Если указано '9 10', берет страницы 9 и 10 из PDF.
    """
    if not page_str or pd.isna(page_str) or str(page_str).strip() == '':
        return [], "Страница не указана", []

    numbers = re.findall(r'\d+', str(page_str))
    if not numbers:
        return [], f"Не распознан номер страницы '{page_str}'", []

    pdf_pages = []
    for num in numbers:
        p_int = int(num)
        # 1-based номер страницы относительно PDF
        if 1 <= p_int <= len(doc):
            pdf_pages.append(p_int)

    if not pdf_pages:
        return [], f"Страницы {numbers} выходят за пределы PDF ({len(doc)} стр.)", []

    # 0-based индексы для PyMuPDF
    target_indices = [p - 1 for p in pdf_pages]
    desc = f"Стр. {', '.join(map(str, pdf_pages))}"
    return target_indices, desc, pdf_pages

    # Если указан только один лист и это Рисунок/Графика,
    # добавляем соседний Лист 8 в пул проверки как доп. контекст, НЕ удаляя исходный
    expanded_candidates = list(phys_indices)
    first_idx = phys_indices[0]
    if len(phys_indices) == 1 and is_pure_graphic_page(doc[first_idx]) and first_idx > 0:
        if (first_idx - 1) not in expanded_candidates:
            expanded_candidates.insert(0, first_idx - 1)

    desc = f"Листы {numbers} (физ. стр. {[p + 1 for p in expanded_candidates]})"
    # Возвращаем: страницы для анализа, описание, и список всех найденных страниц для UI
    return expanded_candidates, desc, [p + 1 for p in phys_indices]


def render_page(doc, page_idx, dpi=180):
    return doc[page_idx].get_pixmap(dpi=dpi).tobytes("png")


def parse_llm_json(raw_text):
    """
    Надежный парсер JSON: вырезает мыслительные теги <think>,
    чистит внешние тексты и спасает данные, даже если кавычки внутри сломали JSON.
    """
    if not raw_text or not raw_text.strip():
        return None

    # 1. Убираем теги размышлений модели (актуально для Qwen)
    cleaned = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL).strip()

    # 2. Ищем JSON от первой { до последней }
    match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
    if not match:
        return None
    json_str = match.group(1).strip()

    # 3. Пробуем стандартный json.loads
    try:
        return json.loads(json_str)
    except Exception:
        pass

    # 4. Если кавычки внутри сломали парсер — вытаскиваем поля регулярками (Fallback)
    st_m = re.search(r'"status"\s*:\s*"([^"]+)"', json_str)
    vol_m = re.search(r'"found_volume"\s*:\s*"([^"]+)"', json_str)
    page_m = re.search(r'"found_page"\s*:\s*"([^"]+)"', json_str)

    # Забираем суть замечания
    det_m = re.search(r'"discrepancy_details"\s*:\s*"(.*?)(?:"\s*\}|"$)', json_str, re.DOTALL)
    details = det_m.group(1).replace('\n', ' ') if det_m else cleaned[:250]

    if st_m:
        return {
            "status": "ОК" if "ок" in st_m.group(1).lower() else "Ошибка",
            "found_volume": vol_m.group(1) if vol_m else "-",
            "found_page": page_m.group(1) if page_m else "-",
            "discrepancy_details": details
        }

    if not raw_text or not raw_text.strip():
        return {
            "status": "Ошибка",
            "found_volume": "-",
            "found_page": "-",
            "discrepancy_details": "Модель вернула пустой ответ (проверьте лимиты контекста или токен в .env)"
        }

    return None

def audit_position_hybrid(provider, client, model_name, doc, target_indices, work_name, volume, unit, ref_text, page_ref, comment_text="", formula_text="", max_retries=3):
    """
    Универсальный аудитор: поддерживает Google Gemini (мультимодальный)
    и Yandex Cloud (Qwen) через responses.create API.
    """
    # Собираем текстовый контент со страниц
    text_context = ""
    gemini_contents = []

    for p_idx in target_indices:
        p_num = p_idx + 1
        p_text = doc[p_idx].get_text().strip()
        text_context += f"\n--- [СТРАНИЦА {p_num} ИЗ PDF] ---\n{p_text}\n"

        if len(p_text) > 80:
            gemini_contents.append(f"--- [ТЕКСТ СТРАНИЦЫ {p_num} ИЗ PDF] ---\n{p_text}\n")
        else:
            img_bytes = render_page(doc, p_idx)
            gemini_contents.append(types.Part.from_bytes(data=img_bytes, mime_type='image/png'))

    # Инструкции и правила аудита
    instructions = """Ты — строгий эксперт-аудитор проектно-сметной документации.
ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ПРОВЕРКИ:
1. ОБЪЕМ И СЛЭШ (/): Знак '/' в объеме (например, '9/45' или '3/6') означает АЛЬТЕРНАТИВНЫЕ единицы измерения ('или'). Если в ВОР заявлено хотя бы одно из этих чисел (например, 45 или 9) — объем считается ПОДТВЕРЖДЕННЫМ.
2. ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ (КОММЕНТАРИЙ): Обязательно учитывай комментарий из ВОР! Если там указан адрес организации, реквизиты СРО или коэффициенты (например, для районной надбавки), и этот адрес/коэффициент подтверждается в ПД (в титуле, лицензии, тексте) — считай позицию ПОДТВЕРЖДЕННОЙ!
3. СТРАНИЦА: данные должны находиться строго на указанной в ВОР странице.
4. ЕДИНИЦА ИЗМЕРЕНИЯ: должна быть логически совместима.
5. ДОПУСТИМОЕ ОКРУГЛЕНИЕ ОБЪЕМОВ:
В сметных расчетах разрешено стандартное математическое округление или округление в большую сторону (до целого, до 0.1 или до 0.01).
Например:
- В ПД найдено 78.9 (или 78.85), а в ВОР заявлено 79 — это СОВПАДЕНИЕ (ОК)!
- В ПД найдено 281.85, а в ВОР заявлено 282 — это СОВПАДЕНИЕ (ОК)!
- В ПД 631.2, а в ВОР 631 или 632 — это СОВПАДЕНИЕ (ОК)!
Не считай разницу из-за округления ошибкой!

Статус 'ОК' ставится, если объем (с учетом '/') и комментарий подтверждены, и страница совпадает.
Если есть несоответствие — статус строго 'Ошибка'.

Ответь СТРОГО в формате JSON:
{
  "status": "ОК" | "Ошибка",
  "found_volume": "найденный объем и единица или '-'",
  "found_page": "номер страницы PDF где реально найдено (число или '-')",
  "discrepancy_details": "краткое пояснение расхождений или подтверждение"
}"""

    user_input = f"""Проверяемая работа: "{work_name}"
Заявленный объем: {volume} {unit}
Формула расчета (если есть): "{formula_text}"
Дополнительная информация (комментарий): "{comment_text}"
Ссылка в ВОР: "{ref_text}" (указана стр. {page_ref})

ТЕКСТ ИЗ ДОКУМЕНТА PDF:
{text_context}"""

    for attempt in range(max_retries):
        raw_text = ""  # 1. ОБЯЗАТЕЛЬНО инициализируем заранее, чтобы не было ошибки UnboundLocalError
        try:
            if provider == "Yandex Cloud (Qwen)":
                # ВЫЗОВ YANDEX CLOUD
                ya_client = openai.OpenAI(
                    api_key=YANDEX_API_KEY,
                    base_url="https://ai.api.cloud.yandex.net/v1",
                    project=YANDEX_FOLDER
                )
                trimmed_input = user_input[:4000] if len(user_input) > 4000 else user_input

                response = ya_client.responses.create(
                    model=f"gpt://{YANDEX_FOLDER}/{model_name}",
                    temperature=0.1,
                    instructions=instructions,
                    input=trimmed_input,
                    max_output_tokens=3500
                )

                if getattr(response, "output_text", None):
                    raw_text = response.output_text.strip()

                if not raw_text and hasattr(response, "output") and response.output:
                    for item in response.output:
                        if hasattr(item, "content") and item.content:
                            if isinstance(item.content, list):
                                for part in item.content:
                                    if hasattr(part, "text") and part.text:
                                        raw_text += part.text
                                    elif isinstance(part, str):
                                        raw_text += part
                            elif isinstance(item.content, str):
                                raw_text += item.content
                        elif hasattr(item, "text") and item.text:
                            raw_text += item.text
                raw_text = raw_text.strip()

            else:
                # ВЫЗОВ GOOGLE GEMINI
                # Страховка клиента: если он не был передан, инициализируем из secrets / env
                gemini_client = client if client is not None else genai.Client(api_key=DEFAULT_GEMINI_KEY)

                gemini_contents_full = gemini_contents + [instructions + "\n\n" + user_input]
                resp = gemini_client.models.generate_content(
                    model=model_name,
                    contents=gemini_contents_full
                )
                # Читаем текст ответа Gemini
                raw_text = resp.text.strip() if getattr(resp, "text", None) else ""

            # 2. Парсим извлеченный JSON
            parsed_data = parse_llm_json(raw_text)
            if parsed_data:
                return parsed_data
            else:
                return {
                    "status": "Ошибка",
                    "found_volume": "-",
                    "found_page": "-",
                    "discrepancy_details": f"Не удалось извлечь JSON из ответа: {raw_text[:150]}..."
                }

        except Exception as e:
            err_msg = str(e)
            if any(code in err_msg for code in ["503", "429", "UNAVAILABLE"]):
                time.sleep((attempt + 1) * 3)
                if attempt == max_retries - 1:
                    return {"status": "Ошибка", "found_volume": "-", "found_page": "-",
                            "discrepancy_details": f"Лимит API ({model_name})"}
            else:
                return {"status": "Ошибка", "found_volume": "-", "found_page": "-",
                        "discrepancy_details": f"Сбой API: {err_msg}"}


def generate_word_report(df_final, meta):
    doc = docx.Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = s.left_margin = s.right_margin = Inches(0.7)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_t = title.add_run("ВЕДОМОСТЬ ЗАМЕЧАНИЙ ПО РЕЗУЛЬТАТАМ АУДИТА ВОР")
    r_t.bold = True
    r_t.font.size = Pt(14)
    r_t.font.color.rgb = RGBColor(30, 41, 59)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_s = sub.add_run(f"Объект: {meta['object_name']}\nДокумент: {meta['doc_number']} (Основание: {meta['basis_code']})")
    r_s.font.size = Pt(10)
    r_s.font.color.rgb = RGBColor(100, 116, 139)

    total = len(df_final)
    df_disc = df_final[df_final['Статус аудита'].isin(['Замечание', 'Не подтверждено', 'Ошибка'])]
    ok_items = df_final[df_final['Статус аудита'] == 'ОК']

    p_stat = doc.add_paragraph()
    p_stat.add_run("1. Краткие итоги проверки:\n").bold = True
    p_stat.add_run(f"• Всего проверено позиций: {total}\n")
    p_stat.add_run(f"• Принято без замечаний (ОК): {len(ok_items)}\n")
    p_stat.add_run(f"• Выявлено замечаний и расхождений: {len(df_disc)}\n")

    if not ok_items.empty:
        ok_nums = ", ".join([str(n) for n in ok_items.iloc[:, 0].tolist()])
        p_stat.add_run(f"• Позиции, подтвержденные без замечаний: № {ok_nums}\n")

    h2 = doc.add_paragraph()
    r_h2 = h2.add_run("2. Перечень выявленных несоответствий:")
    r_h2.bold = True
    r_h2.font.size = Pt(12)

    if df_disc.empty:
        doc.add_paragraph().add_run("Замечаний не выявлено. Все позиции подтверждены.")
    else:
        table = doc.add_table(rows=1, cols=6)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.style = 'Table Grid'
        headers = ["№", "Наименование работы", "ВОР", "Факт ПД", "Статус", "Суть замечания / Рекомендация"]
        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            hdr_cells[i].text = h
            hdr_cells[i].paragraphs[0].runs[0].bold = True
            hdr_cells[i].paragraphs[0].runs[0].font.size = Pt(9)
            tcPr = hdr_cells[i]._element.get_or_add_tcPr()
            tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="E2E8F0"/>'))

        for _, row in df_disc.iterrows():
            row_cells = table.add_row().cells
            row_cells[0].text = str(row.iloc[0])
            row_cells[1].text = str(row.get("Наименование работ, ресурсов, затрат по проекту", ""))
            row_cells[2].text = f"{row.get('Объем работ / Количество', '')} {row.get('Ед. изм.', '')}"
            row_cells[3].text = f"{row.get('Фактический объем', '-')}\n({row.get('Фактическое место в PDF', '-')})"
            st_text = str(row.get("Статус аудита", ""))
            row_cells[4].text = st_text
            fill_color = "FEF3C7" if st_text == "Замечание" else "FEE2E2"
            tcPr = row_cells[4]._element.get_or_add_tcPr()
            tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_color}"/>'))
            row_cells[5].text = str(row.get("Детали расхождений", ""))
            for cell in row_cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(8.5)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()

# ----------------- СКАНИРОВАНИЕ ДОКУМЕНТОВ -----------------

loaded_vors = {}
pdf_pool = {}
pdf_local_paths = {}

if source_mode == "📁 Локальная папка проекта":
    folder_path = st.sidebar.text_input("Путь к папке проекта на диске", value=r"C:\Projects\Bridge_Kemerovo")
    if os.path.exists(folder_path):
        # Находим все PDF (игнорируя скрытые файлы ~$*)
        found_pdfs = glob.glob(os.path.join(folder_path, "**", "*.pdf"), recursive=True)
        for p in found_pdfs:
            fname = os.path.basename(p).lower()
            if fname.startswith("~$"):
                continue
            try:
                pdf_pool[fname] = fitz.open(p)
                pdf_local_paths[fname] = p
            except Exception:
                pass

        # Находим все ВОР (игнорируя временные файлы ~$*)
        found_vors = glob.glob(os.path.join(folder_path, "**", "*.xlsx"), recursive=True) + \
                     glob.glob(os.path.join(folder_path, "**", "*.csv"), recursive=True)
        for p in found_vors:
            fname = os.path.basename(p)
            if fname.startswith("~$"):
                continue
            loaded_vors[fname] = p

        st.sidebar.success(f"Обнаружено: {len(pdf_pool)} PDF и {len(loaded_vors)} ВОР")
    else:
        st.sidebar.warning("Указанная папка не найдена.")

else:
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0

    # Растянутая кнопка на всю ширину боковой панели
    if st.sidebar.button("🗑️ Очистить все файлы", use_container_width=True):
        st.session_state.uploader_key += 1
        st.session_state.pdf_pool = {}
        st.session_state.audit_results = {}
        st.rerun()

    uploaded_files = st.sidebar.file_uploader(
        "Загрузите файлы проекта (PDF, XLSX, CSV)",
        type=["pdf", "xlsx", "csv"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_key}"
    )

    if uploaded_files:
        for uf in uploaded_files:
            fname = uf.name.lower()
            if fname.endswith(".pdf"):
                pdf_pool[fname] = fitz.open(stream=uf.read(), filetype="pdf")
                pdf_local_paths[fname] = uf
            elif fname.endswith(".xlsx") or fname.endswith(".csv"):
                loaded_vors[uf.name] = uf

        st.sidebar.success(f"Загружено: {len(pdf_pool)} PDF и {len(loaded_vors)} ВОР")

st.session_state.pdf_pool = pdf_pool
st.session_state.pdf_local_paths = pdf_local_paths

# ----------------- АУДИТ ВОР -----------------

# Создаем две вкладки на главной странице:
main_tab1, main_tab2 = st.tabs([
    "Проверка ПД и ВОР (Комплексный аудит)",
    "Проверка правил оформления ВОР"
])

# ----------------- ВКЛАДКА 1: ВЕСЬ СУЩЕСТВУЮЩИЙ КОД -----------------
with main_tab1:
    if loaded_vors:
        st.subheader("📋 Выбор ведомостей для проверки")
        target_vor_selection = st.multiselect(
            "Выберите ведомости для аудита:",
            options=list(loaded_vors.keys()),
            default=list(loaded_vors.keys())
        )

        # =========================================================================
        # ДИАГНОСТИКА: ПРОВЕРКА ПУТЕЙ НА ДИСКЕ И ИМЕН ФАЙЛОВ PDF
        # =========================================================================
        if target_vor_selection:
            st.markdown("### 🔍 Предварительная проверка путей и файлов PDF")

            preflight_rows = []
            has_critical_error = False

            for v_name in target_vor_selection:
                try:
                    df_prev, _ = load_vor_with_metadata(loaded_vors[v_name], v_name)
                except Exception:
                    continue

                f_cols = [c for c in df_prev.columns if 'наименование файла' in c.lower()]
                if not f_cols:
                    preflight_rows.append({
                        "Ведомость (ВОР)": v_name,
                        "Путь в ячейке ВОР": "Колонка не найдена",
                        "Прямой путь на диске": "—",
                        "Статус привязки": "🟡 По шифру тома",
                        "Фактический PDF": "Определится по разделу"
                    })
                    continue

                all_raw_entries = df_prev[f_cols[0]].dropna().tolist()
                individual_paths = []

                for entry in all_raw_entries:
                    text_val = str(entry).strip()
                    if not text_val or text_val.lower() == 'nan':
                        continue
                    # Разбиваем по переносам строк, точке с запятой или запятой
                    lines = re.split(r'[\n\r;]+', text_val)
                    for line in lines:
                        c_line = line.strip().strip('"\'«»')
                        if c_line and c_line not in individual_paths:
                            individual_paths.append(c_line)

                for clean_raw in individual_paths:
                    # дальше идет ваша существующая проверка физического пути и имени файла:
                    direct_path_exists = os.path.exists(clean_raw)
                    disk_status = "🟢 Файл на месте" if direct_path_exists else "⚠️ Путь не найден на ПК"

                    base_name = os.path.basename(clean_raw.replace('\\', '/')).strip().strip('"\'«»').lower()
                    matched_pdf = None
                    bind_status = "🔴 Не найден"

                    # 1. Проверка физического пути на диске (для локального режима)
                    direct_path_exists = os.path.exists(clean_raw)
                    disk_status = "🟢 Файл на месте" if direct_path_exists else "⚠️ Путь не найден на ПК"

                    # 2. Проверка имени файла и привязки к загруженному пулу
                    base_name = os.path.basename(clean_raw.replace('\\', '/')).strip().lower()
                    matched_pdf = None
                    bind_status = "🔴 Не найден"

                    if base_name in pdf_pool:
                        matched_pdf = base_name
                        bind_status = "🟢 Имя совпадает"
                    else:
                        # Поиск с очисткой дефисов и подчеркиваний
                        c_target = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', os.path.splitext(base_name)[0]).lower()
                        for p_name in pdf_pool.keys():
                            c_pool = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', os.path.splitext(p_name)[0]).lower()
                            if c_target == c_pool or (c_target in c_pool) or (c_pool in c_target):
                                matched_pdf = p_name
                                bind_status = "🟡 Опечатка в символах"
                                break

                    if not matched_pdf:
                        has_critical_error = True

                    preflight_rows.append({
                        "Ведомость (ВОР)": v_name,
                        "Путь в ячейке ВОР": clean_raw,
                        "Прямой путь на диске": disk_status,
                        "Статус привязки": bind_status,
                        "Фактический PDF": matched_pdf or "ОТСУТСТВУЕТ В ПАПКЕ"
                    })

            if preflight_rows:
                df_diag = pd.DataFrame(preflight_rows)


                def highlight_preflight(val):
                    if "🟢" in str(val):
                        return 'background-color: #d4edda; color: #155724;'
                    elif "🟡" in str(val) or "⚠️" in str(val):
                        return 'background-color: #fff3cd; color: #856404;'
                    elif "🔴" in str(val):
                        return 'background-color: #f8d7da; color: #721c24;'
                    return ''


                st.dataframe(
                    df_diag.style.map(highlight_preflight, subset=["Прямой путь на диске", "Статус привязки"]),
                    use_container_width=True
                )

                if has_critical_error:
                    st.error("❌ Обнаружены отсутствующие файлы PDF! Догрузите их перед запуском аудита.")
                else:
                    st.success("✅ Все файлы PDF успешно идентифицированы и готовы к проверке.")
        # =========================================================================

        start_btn = st.button("🚀 Запустить аудит", type="primary", use_container_width=True)

        if start_btn:
            if not api_key:
                st.error("Пожалуйста, введите Gemini API Key в боковой панели!")
            elif not pdf_pool:
                st.error("Не загружено ни одного файла PDF!")
            else:
                client = genai.Client(api_key=api_key) if provider == "Google Gemini" else None

                # Предварительно парсим все выбранные ВОР для точного расчета шкалы
                vors_parsed = {}
                total_items = 0
                for v_name in target_vor_selection:
                    df_w, meta = load_vor_with_metadata(loaded_vors[v_name], v_name)
                    vors_parsed[v_name] = (df_w, meta)
                    total_items += len(df_w)

                # Создаем постоянный прогресс-бар (он не исчезнет в конце)
                prog_placeholder = st.empty()
                progress_bar = prog_placeholder.progress(0, text="Инициализация проверки...")
                current_row_idx = 0

                for v_name, (df_works, meta) in vors_parsed.items():
                    page_col = [c for c in df_works.columns if 'номер страниц' in c.lower()][0]
                    name_col = [c for c in df_works.columns if 'наименование' in c.lower()][0]
                    vol_col = [c for c in df_works.columns if 'объем' in c.lower() or 'количество' in c.lower()][0]
                    unit_col = [c for c in df_works.columns if 'ед.' in c.lower()][0]
                    ref_col = [c for c in df_works.columns if 'ссылка' in c.lower()][0]
                    file_col = [c for c in df_works.columns if 'наименование файла' in c.lower()]
                    file_col_name = file_col[0] if file_col else None
                    # Находим колонку комментария и формулы
                    comm_cols = [c for c in df_works.columns if
                                 'дополнительная' in c.lower() or 'комментарий' in c.lower()]
                    comment_col_name = comm_cols[0] if comm_cols else None

                    form_cols = [c for c in df_works.columns if 'формула' in c.lower()]
                    formula_col_name = form_cols[0] if form_cols else None

                    results = []
                    row_page_tracking = []

                    for _, row in df_works.iterrows():
                        current_row_idx += 1
                        w_name = str(row[name_col])
                        w_vol = str(row[vol_col])
                        w_unit = str(row[unit_col])
                        w_ref = str(row[ref_col])
                        w_page = str(row[page_col]) if page_col else ""
                        w_comment = str(row.get(comment_col_name, "")).strip() if comment_col_name else ""
                        w_formula = str(row.get(formula_col_name, "")).strip() if formula_col_name else ""

                        # Обновляем progress bar на каждой строке
                        pct = current_row_idx / total_items
                        progress_bar.progress(
                            pct,
                            text=f"Проверка [{current_row_idx}/{total_items}]: {v_name} ➔ {w_name[:38]}..."
                        )

                        # 1. Защита от NameError (инициализируем заранее)
                        primary_phys_pages = [1]
                        target_pdf_doc = None
                        target_pdf_name = ""

                        # 2. Извлекаем имя PDF из строки ВОР (если заполнено)
                        if file_col_name:
                            raw_pdf_path = str(row.get(file_col_name, '')).strip().strip('"\'«»')
                            if raw_pdf_path and raw_pdf_path.lower() != 'nan':
                                target_pdf_name = os.path.basename(raw_pdf_path.replace('\\', '/')).strip().strip(
                                    '"\'«»').lower()

                        # 3. УМНЫЙ РОУТИНГ PDF СРЕДИ НЕСКОЛЬКИХ ФАЙЛОВ:
                        # Вариант А: Прямое точное совпадение имени
                        if target_pdf_name and target_pdf_name in pdf_pool:
                            target_pdf_doc = pdf_pool[target_pdf_name]

                        # Вариант Б: Нечеткое совпадение по имени (если имя файла чуть отличается)
                        elif target_pdf_name:
                            target_stem = os.path.splitext(target_pdf_name)[0]
                            for p_name, doc_obj in pdf_pool.items():
                                p_stem = os.path.splitext(p_name)[0]
                                if (target_stem in p_name) or (p_stem in target_stem):
                                    target_pdf_doc = doc_obj
                                    target_pdf_name = p_name
                                    break

                        # Вариант В: Если в ячейке не было пути к файлу, ищем по шифру ВОР (Основанию)
                        if target_pdf_doc is None and meta.get('basis_code'):
                            clean_basis = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', meta['basis_code']).lower()
                            for p_name, doc_obj in pdf_pool.items():
                                clean_p = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', p_name).lower()
                                if clean_basis and (clean_basis in clean_p or clean_p in clean_basis):
                                    target_pdf_doc = doc_obj
                                    target_pdf_name = p_name
                                    break

                        # Вариант Г: Если в папке всего 1 PDF — берем его как единственный вариант
                        if target_pdf_doc is None and len(pdf_pool) == 1:
                            target_pdf_name, target_pdf_doc = list(pdf_pool.items())[0]

                        # 4. Проверка и анализ
                        if target_pdf_doc is None:
                            audit = {
                                "status": "Ошибка",
                                "found_volume": "-",
                                "found_page": "-",
                                "discrepancy_details": f"Не удалось привязать PDF к строке. Искали: '{target_pdf_name}'"
                            }
                        else:
                            # Восстановлен поиск страниц:
                            if target_pdf_name not in st.session_state.pdf_page_maps:
                                st.session_state.pdf_page_maps[target_pdf_name] = build_pdf_page_map(target_pdf_doc)
                            p_map = st.session_state.pdf_page_maps[target_pdf_name]

                            target_indices, loc_desc, primary_phys_pages = get_optimized_page_indices(target_pdf_doc,
                                                                                                      p_map, w_page)

                            if target_indices:
                                audit = audit_position_hybrid(
                                    provider=provider,
                                    client=client,
                                    model_name=model_name,
                                    doc=target_pdf_doc,
                                    target_indices=target_indices,
                                    work_name=w_name,
                                    volume=w_vol,
                                    unit=w_unit,
                                    ref_text=w_ref,
                                    page_ref=w_page,
                                    comment_text=w_comment,
                                    formula_text=w_formula
                                )
                            else:
                                audit = {
                                    "status": "Ошибка",
                                    "found_volume": "-",
                                    "found_page": "-",
                                    "discrepancy_details": loc_desc
                                }

                        # 5. Сохранение результата (строго для КАЖДОЙ строки)
                        raw_found_p = str(audit.get("found_page", "-")).strip()
                        nums = re.findall(r'\d+', raw_found_p)
                        found_p_num = int(nums[0]) if nums and raw_found_p not in ["-", "None", "", "null"] else None
                        display_page = f"Стр. {found_p_num}" if found_p_num else "-"

                        final_status = audit.get("status", "Ошибка")
                        details_text = audit.get("discrepancy_details", "")

                        vor_pages_int = primary_phys_pages if primary_phys_pages else []
                        if found_p_num and vor_pages_int and (found_p_num not in vor_pages_int):
                            final_status = "Ошибка"
                            if "страниц" not in details_text.lower():
                                details_text = f"Не совпадает страница: в ВОР указана стр. {', '.join(map(str, vor_pages_int))}, фактически данные на стр. {found_p_num}. " + details_text

                        results.append({
                            "Статус аудита": final_status,
                            "Фактический объем": audit.get("found_volume", "-"),
                            "Фактическая страница в ПД (найдено моделью)": display_page,
                            "Детали расхождений": details_text
                        })

                        show_p = [found_p_num] if found_p_num else (vor_pages_int if vor_pages_int else [1])
                        row_page_tracking.append({
                            "pdf_name": target_pdf_name,
                            "pages_to_show": show_p,
                            "vor_pages": vor_pages_int,
                            "found_page": found_p_num,
                            "work_name": w_name
                        })

                        time.sleep(1.2)

                        # ПОСЛЕ ЗАВЕРШЕНИЯ ЦИКЛА СТРОК: СКЛЕЙКА ТАБЛИЦЫ
                    cols_to_drop = [c for c in df_works.columns if
                                    str(c).startswith('Unnamed') or str(c).strip() in ['1', '2', '3', '4', '5', '6',
                                                                                       '6.1', '6.2', '7', '8', '9']]
                    df_works_clean = df_works.drop(columns=cols_to_drop, errors='ignore').copy()

                    rename_map = {}
                    if vol_col in df_works_clean.columns:
                        rename_map[vol_col] = "[ВОР] Заявленный объем"
                    if unit_col in df_works_clean.columns:
                        rename_map[unit_col] = "[ВОР] Ед. изм."
                    if page_col in df_works_clean.columns:
                        rename_map[page_col] = "[ВОР] Ссылка на страницу"
                    df_works_clean.rename(columns=rename_map, inplace=True)

                    df_audit = pd.DataFrame([
                        {
                            "Статус аудита": r["Статус аудита"],
                            "[ПД] Найденный объем (нейросеть)": r["Фактический объем"],
                            "[ПД] Фактическая страница": r["Фактическая страница в ПД (найдено моделью)"],
                            "Детали расхождений": r["Детали расхождений"]
                        }
                        for r in results
                    ], index=df_works_clean.index)

                    df_final = pd.concat([df_works_clean, df_audit], axis=1)


                    excel_out = io.BytesIO()
                    with pd.ExcelWriter(excel_out, engine='openpyxl') as writer:
                        df_final.to_excel(writer, index=False, sheet_name='Аудит')

                    word_bytes = generate_word_report(df_final, meta)

                    st.session_state.audit_results[v_name] = {
                        "df": df_final,
                        "meta": meta,
                        "tracking": row_page_tracking,
                        "excel_bytes": excel_out.getvalue(),
                        "word_bytes": word_bytes
                    }

                # Фиксируем прогресс-бар на 100% (не удаляем его!)
                progress_bar.progress(1.0, text=f"✅ Аудит успешно завершен! Проверено позиций: {total_items}")
    pass

# ----------------- ВКЛАДКА 2: ПРОВЕРКА ОФОРМЛЕНИЯ -----------------
with main_tab2:
    st.subheader("📐 Валидация оформления шаблона ВОР")
    st.markdown("Проверка шапки, версий, реквизитов и названий столбцов на строгое соответствие регламенту.")

    # Инициализация состояния для вкладки 2
    if "rules_results" not in st.session_state:
        st.session_state.rules_results = None
    if "rules_file_name" not in st.session_state:
        st.session_state.rules_file_name = None

    check_file = st.file_uploader(
        "Загрузите файл ВОР для проверки правил оформления (.xlsx)",
        type=["xlsx"],
        key="rules_uploader"
    )

    # Если загрузили другой файл — сбрасываем старые результаты
    if check_file and check_file.name != st.session_state.rules_file_name:
        st.session_state.rules_results = None
        st.session_state.rules_file_name = check_file.name

    if check_file:
        if st.button("🚀 Проверить оформление", type="primary", key="btn_check_rules"):
            prog_bar = st.progress(0, text="Инициализация проверки правил...")

            # Запуск валидатора и сохранение в память сессии
            rule_results = validate_vor_formatting(check_file)
            total_rules = len(rule_results)

            for i, res in enumerate(rule_results):
                prog_bar.progress((i + 1) / total_rules, text=f"Проверка ячейки {res['coord']}...")
                time.sleep(0.02)

            prog_bar.progress(1.0, text="✅ Валидация оформления завершена!")
            st.session_state.rules_results = rule_results

    # Вывод результатов (вынесен ИЗ-ПОД кнопки, поэтому чекбокс больше ничего не сбрасывает)
    if st.session_state.rules_results is not None:
        rule_results = st.session_state.rules_results
        total_rules = len(rule_results)

        errors_count = sum(1 for r in rule_results if r["status"] == "Ошибка")
        warn_count = sum(1 for r in rule_results if r["status"] == "Внимание")
        ok_count = sum(1 for r in rule_results if r["status"] == "ОК")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Всего ячеек", total_rules)
        m2.metric("🟢 Соответствует (ОК)", ok_count)
        m3.metric("🟡 Требует внимания", warn_count)
        m4.metric("🔴 Ошибок оформления", errors_count)

        df_rules = pd.DataFrame([
            {
                "Адрес ячейки": r["coord"],
                "Тип проверки": r["type"],
                "Ожидаемое значение / Формат": r["expected"],
                "Фактическое значение": r["actual"],
                "Статус": r["status"],
                "Комментарий / Ошибка": r["error"]
            }
            for r in rule_results
        ])

        # Чекбокс теперь мгновенно фильтрует таблицу без вылета
        show_only_issues = st.checkbox("Показать только замечания и ошибки", value=False)
        if show_only_issues:
            df_to_show = df_rules[df_rules["Статус"] != "ОК"]
        else:
            df_to_show = df_rules


        def highlight_rule_status(val):
            if val == "ОК":
                return 'background-color: #d4edda; color: #155724;'
            elif val == "Внимание":
                return 'background-color: #fff3cd; color: #856404;'
            return 'background-color: #f8d7da; color: #721c24;'


        styled_rules = df_to_show.style.map(highlight_rule_status, subset=['Статус'])
        st.dataframe(styled_rules, use_container_width=True)

        # Генерация и скачивание размеченного желтым Excel
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            highlighted_excel = get_highlighted_workbook(check_file, rule_results)
            if highlighted_excel:
                st.download_button(
                    label="📥 Скачать ВОР с желтыми ячейками (Excel)",
                    data=highlighted_excel,
                    file_name=f"Ошибки_{check_file.name}",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
        with col_btn2:
            word_rules_doc = generate_rules_word_report(check_file.name, rule_results)
            st.download_button(
                label="📄 Скачать ведомость ошибок (Word DOCX)",
                data=word_rules_doc,
                file_name=f"Ведомость_ошибок_{check_file.name}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
                use_container_width=True
            )
# ----------------- ОТОБРАЖЕНИЕ РЕЗУЛЬТАТОВ -----------------

if st.session_state.audit_results:
    st.markdown("---")
    st.header("📊 Результаты проверки по ведомостям")

    def color_status(val):
        if val == "ОК":
            return 'background-color: #d4edda; color: #155724;'
        elif val == "Замечание":
            return 'background-color: #fff3cd; color: #856404;'
        else:
            return 'background-color: #f8d7da; color: #721c24;'

    vor_tab_names = list(st.session_state.audit_results.keys())
    tabs = st.tabs(vor_tab_names)

    for tab, v_name in zip(tabs, vor_tab_names):
        with tab:
            data = st.session_state.audit_results[v_name]
            df_res = data["df"]
            meta_res = data["meta"]
            tracking_info = data.get("tracking", [])

            # Метрики
            tot = len(df_res)
            ok_c = len(df_res[df_res['Статус аудита'] == 'ОК'])
            warn_c = len(df_res[df_res['Статус аудита'] == 'Замечание'])
            err_c = len(df_res[df_res['Статус аудита'].isin(['Не подтверждено', 'Ошибка'])])

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Всего позиций", tot)
            m2.metric("🟢 Без замечаний (ОК)", ok_c)
            m3.metric("🟡 С замечаниями", warn_c)
            m4.metric("🔴 Не подтверждено", err_c)

            st.write(f"**Объект:** {meta_res['object_name']} | **Основание:** {meta_res['basis_code']}")

            st.info("💡 **Кликните по любой строке в таблице ниже**, чтобы мгновенно просмотреть этот лист PDF или открыть его в ридере на нужной странице.")

            # Подготовка таблицы без Arrow-ошибок
            df_display = prepare_df_for_display(df_res)
            styler = df_display.style
            if hasattr(styler, 'map'):
                styled_df = styler.map(color_status, subset=['Статус аудита'])
            else:
                styled_df = styler.applymap(color_status, subset=['Статус аудита'])

            # Интерактивная таблица с выбором строки
            event = st.dataframe(
                styled_df,
                width="stretch",
                on_select="rerun",
                selection_mode="single-row",
                key=f"df_table_{v_name}"
            )

            # ----------------- ИНСПЕКТОР: РАЗГРАНИЧЕНИЕ СКАНИРОВАНИЯ -----------------
            if event and event.selection and event.selection.rows:
                selected_row_idx = event.selection.rows[0]
                if selected_row_idx < len(tracking_info):
                    t_info = tracking_info[selected_row_idx]
                    sel_pdf_name = t_info["pdf_name"]
                    vor_pages = t_info.get("vor_pages", [])
                    found_p = t_info.get("found_page", None)
                    sel_work_name = t_info["work_name"]

                    with st.container(border=True):
                        st.markdown(f"### 🔍 Инспектор: **{sel_work_name[:75]}**")
                        st.caption(f"Документ: `{sel_pdf_name}`")

                        doc_obj = st.session_state.pdf_pool.get(sel_pdf_name)

                        # Две четко разграниченные вкладки
                        lbl_vor = f"📌 Страница по ссылке из ВОР (Стр. {', '.join(map(str, vor_pages)) if vor_pages else '-'})"
                        lbl_found = f"🎯 Фактическая страница с данными ({f'Стр. {found_p}' if found_p else 'Не найдена'})"

                        tab_vor, tab_found = st.tabs([lbl_vor, lbl_found])

                        # Вкладка 1: Что находится на странице, которую указал сметчик
                        with tab_vor:
                            st.info(
                                "Лист, на который ссылается сметчик в ведомости (проверьте, почему там нет объемов):")
                            if doc_obj and vor_pages:
                                for p_num in vor_pages:
                                    if 0 <= p_num - 1 < len(doc_obj):
                                        st.image(
                                            render_page(doc_obj, p_num - 1, dpi=160),
                                            caption=f"{sel_pdf_name} — Страница {p_num} (указана в ВОР)",
                                            width=850
                                        )
                            else:
                                st.warning("Страница в ВОР не указана или выходит за пределы файла.")

                        # Вкладка 2: Где модель реально нашла эти данные
                        with tab_found:
                            if doc_obj and found_p:
                                st.success(f"Лист в ПД, где фактически обнаружены данные по работе (Стр. {found_p}):")
                                if 0 <= found_p - 1 < len(doc_obj):
                                    st.image(
                                        render_page(doc_obj, found_p - 1, dpi=160),
                                        caption=f"{sel_pdf_name} — Страница {found_p} (фактически найдено моделью)",
                                        width=850
                                    )
                            else:
                                st.error("Модель не смогла найти эту работу ни на одной странице документа.")

            # Скачивание
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                st.download_button(
                    label=f"📊 Скачать Excel ({v_name})",
                    data=data["excel_bytes"],
                    file_name=f"Аудит_{v_name}.xlsx" if not v_name.endswith('.xlsx') else f"Аудит_{v_name}",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_xl_{v_name}"
                )
            with col_d2:
                st.download_button(
                    label=f"📄 Скачать замечания Word ({v_name})",
                    data=data["word_bytes"],
                    file_name=f"Замечания_{v_name}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    type="primary",
                    key=f"dl_doc_{v_name}"
                )
else:
    st.info("Укажите папку проекта или загрузите файлы слева для начала аудита.")