import glob
import os
import time
import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from vor_rules import validate_vor_formatting, get_highlighted_workbook, generate_rules_word_report
from audit_core import INCOMPLETE, TECHNICAL_ERROR
from pdf_text import bounded_page_dpi
from audit_tracing import TracingRun
from audit_settings import get_setting as _resolve_setting
from audit_models import AuditSettings
from audit_service import preflight_bindings, run_audit


def get_setting(name, default=""):
    """Настройки: .env этого проекта, окружение процесса, Streamlit Secrets."""
    return _resolve_setting(name, default, secrets=st.secrets)


def load_settings():
    """Typed settings snapshot; resolution stays .env → env → Secrets."""
    return AuditSettings.load(secrets=st.secrets)


st.set_page_config(page_title="Аудит ВОР и ПД", layout="wide")
st.title("Комплексный аудит ведомостей объемов работ (ВОР)")

# ----------------- SESSION STATE -----------------
if "audit_results" not in st.session_state:
    st.session_state.audit_results = {}
if "audit_run_issues" not in st.session_state:
    st.session_state.audit_run_issues = []
if "pdf_pool" not in st.session_state:
    st.session_state.pdf_pool = {}          # { clean_name: fitz.Document }
if "pdf_local_paths" not in st.session_state:
    st.session_state.pdf_local_paths = {}   # { clean_name: full_path_str }
    
# Флаги выполнения фоновых процессов
if "is_auditing" not in st.session_state:
    st.session_state.is_auditing = False
if "is_checking_rules" not in st.session_state:
    st.session_state.is_checking_rules = False

# =========================================================================
# МОДАЛЬНОЕ ОКНО: РУКОВОДСТВО ПОЛЬЗОВАТЕЛЯ
# =========================================================================
@st.dialog("📖 Краткое руководство пользователя", width="large")
def show_user_guide():
    st.markdown("""
    ### 1. Вкладка «Проверка ПД и ВОР (Комплексный аудит)»
    * **Загрузка файлов:** Загрузите файлы ПД (PDF) и ВОР (XLSX/CSV) в боковом меню или выберите локальную папку (локальная пока не работает, поэтому только вручную, где можно выбрать сразу все нужные файлы из папки).
    * **Проверка:** До старта система проверяет пути и имена файлов, чтобы убедиться что указанные в ВОР  (`🟢` — найден, `🟡` — опечатка в дефисе/подчеркивании, `🔴` — файл отсутствует).
    * **Запуск аудита:** Нажмите кнопку *«Запустить аудит»*. Нейросеть проверяет соответствие объема, единиц измерения и указанной страницы.
    * **Инспектор страниц:** Кликните по любой строке в таблице — ниже откроются две вкладки:
      * *Страница из ВОР* — лист, куда сослался сметчик (чтобы увидеть, почему там нет данных).
      * *Фактические страницы* — все листы с проверенными доказательствами работы, объема, единицы и примечания.
    * **Скачивание:** Кнопки *Excel* и *Word* — краткие отчёты с проблемными позициями в начале, источниками и рекомендуемыми действиями. Ссылки на PDF работают, если положить отчёт рядом с исходным файлом; переход на страницу зависит от просмотрщика.

    ---

    ### 2. Вкладка «Проверка правил оформления ВОР»
    * **Проверка регламента:** Контроль строк 1–16 (шапка, дата 2000–2100 гг., версия `3_01`, ФИО) и строк 17+ (разделы `Bold`, позиции, запрет столбцов L+).
    * **Формулы (графа E):** Математический пересчет и сверка с объемом D. Разрешено округление вверх/математическое (до сотых, десятых и целых).
    * **Идентификаторы (графа J):** Запрещены пустые ячейки и дубликаты во всем документе.
    * **Скачивание:** Оригинальный Excel с желтой заливкой ячеек с ошибками или Word-ведомость только замечаний.

    ---

    ### 3. Важные особенности
    * **Знак `/` в объемах:** Считывается как альтернатива («или»). Если в ПД `9/45`, а в ВОР заявлено `45` — объем подтверждается.
    * **Результаты:** `Ошибка` — найденная работа с расхождением данных или ссылки. `Неполная проверка` — ограниченный поиск не дал достаточных доказательств; отсутствие работы во всем PDF не установлено.
    * **Номера страниц:** «9» означает девятую страницу PDF, независимо от колонтитула. Сначала проверяется связанный контекст ссылки ВОР, затем локально отобранные фрагменты всего текстового индекса. Поиск останавливается на найденной работе, даже при несовпадении объема или примечания.
    * **Лимиты:** Не больше 6 API-попыток на позицию, включая повторы; до 4 страниц и 10 000 символов в контексте. OCR — до 4 новых связанных сканов на позицию с кешем.
    * **Технический сбой:** API, чтение PDF, OCR или некорректный ответ модели могут оставить проверку незавершенной. Причина указана в деталях; найденные доказательства сохраняются.
    """)
    # Стало (окно закроется без принудительной перезагрузки страницы):
    st.button("Понятно, закрыть", use_container_width=True)

# ----------------- БОКОВАЯ ПАНЕЛЬ -----------------
with st.sidebar:
    # КНОПКА ВЫЗОВА ИНСТРУКЦИИ:
    is_busy = st.session_state.is_auditing or st.session_state.is_checking_rules
    if st.button("📖 Инструкция пользователя", use_container_width=True, disabled=is_busy):
        show_user_guide()

    st.markdown("---")
    st.header("📂 Источник данных")
    source_mode = st.radio("Режим работы", ["📁 Локальная папка проекта", "📤 Ручная загрузка файлов"])

# ----------------- СЛУЖЕБНЫЕ УТИЛИТЫ -----------------



def prepare_df_for_display(df):
    """Предотвращает ошибки PyArrow (ArrowInvalid при '9 10')."""
    clean_df = df.copy()
    for col in clean_df.columns:
        clean_df[col] = clean_df[col].fillna('-').astype(str)
        clean_df[col] = clean_df[col].apply(lambda x: x[:-2] if x.endswith('.0') and x[:-2].isdigit() else x)
    return clean_df




_SETTINGS = load_settings()
YANDEX_FOLDER = _SETTINGS.yandex_cloud_folder
YANDEX_API_KEY = _SETTINGS.yandex_cloud_api_key
YANDEX_MODEL_NAME = _SETTINGS.yandex_cloud_model


def render_page(doc, page_idx, dpi=250):
    """Рендерит таблицы и чертежи без неограниченного выделения памяти."""
    page = doc[page_idx]
    return page.get_pixmap(dpi=bounded_page_dpi(page, dpi)).tobytes("png")





# ----------------- СКАНИРОВАНИЕ ДОКУМЕНТОВ -----------------

loaded_vors = {}
# Предыдущий запуск Streamlit мог оставить документы открытыми на диске.
# API-потоки используют только текстовые индексы, поэтому PDF можно закрыть здесь.
for previous_doc in st.session_state.pdf_pool.values():
    if not previous_doc.is_closed:
        previous_doc.close()
pdf_pool = {}
pdf_local_paths = {}

if source_mode == "📁 Локальная папка проекта":
    folder_path = st.sidebar.text_input("Путь к папке проекта на диске", value=r"C:\Projects\Project")
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
        for stale_doc in st.session_state.pdf_pool.values():
            try:
                if not stale_doc.is_closed:
                    stale_doc.close()
            except Exception:
                pass
        st.session_state.pdf_pool = {}
        st.session_state.audit_results = {}
        st.session_state.audit_run_issues = []
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
    "Проверка ПД и ВОР",
    "Проверка правил оформления ВОР"
])

# ----------------- ВКЛАДКА 1: АУДИТ ПД И ВОР -----------------
with main_tab1:
    if not loaded_vors:
        st.info(
            "📂 Для начала работы загрузите файлы проекта (PDF и XLSX) в боковой панели слева или выберите локальную папку проекта.")
    else:
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

            preflight_rows, has_critical_error = preflight_bindings(
                loaded_vors, target_vor_selection, pdf_pool.keys())

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
        # ЗАПУСК АУДИТА С МГНОВЕННОЙ БЛОКИРОВКОЙ КНОПКИ
        # =========================================================================
        if st.session_state.get("is_auditing", False):
            st.button("⏳ Выполняется аудит (подождите)...", disabled=True, use_container_width=True)

            trace_run = None
            try:
                if not YANDEX_API_KEY:
                    st.error("Пожалуйста, укажите API Key в .env!")
                elif not pdf_pool:
                    st.error("Не загружено ни одного файла PDF!")
                else:
                    st.session_state.audit_run_issues = []
                    _run_settings = load_settings()
                    trace_run = TracingRun(
                        _run_settings.langfuse_public_key,
                        _run_settings.langfuse_secret_key,
                        _run_settings.langfuse_base_url,
                        _run_settings.langfuse_environment,
                    )
                    if trace_run.error:
                        st.warning(trace_run.error)
                    for v_name in target_vor_selection:
                        st.session_state.audit_results.pop(v_name, None)

                    prog_placeholder = st.empty()
                    progress_bar = prog_placeholder.progress(0, text="Инициализация многопоточной проверки...")

                    outcome = run_audit(
                        loaded_vors, target_vor_selection, pdf_pool,
                        api_key=YANDEX_API_KEY, folder=YANDEX_FOLDER, model_name=YANDEX_MODEL_NAME,
                        trace_run=trace_run,
                        progress=lambda fraction, text: progress_bar.progress(fraction, text=text))
                    st.session_state.audit_run_issues = outcome["issues"]
                    st.session_state.audit_results.update(outcome["results"])

                    progress_bar.progress(1.0, text="Обработка завершена. Результаты и причины сбоев указаны в отчете.")
                    time.sleep(0.8)
                    prog_placeholder.empty()


            finally:
                if trace_run is not None:
                    trace_run.close()
                st.session_state.is_auditing = False

            st.rerun()

        else:
            if st.button("🚀 Запустить аудит", type="primary", use_container_width=True):
                st.session_state.is_auditing = True
                st.rerun()

    # =========================================================================
    # РЕЗУЛЬТАТЫ ПРОВЕРКИ (ОТОБРАЖАЮТСЯ, ЕСЛИ АУДИТ УЖЕ ПРОВЕДЕН)
    # =========================================================================
    for issue in st.session_state.audit_run_issues:
        if issue.endswith(": позиции для проверки не найдены."):
            st.info(issue)
        else:
            st.error(issue)

    if st.session_state.audit_results:
        st.markdown("---")
        st.header("📊 Результаты проверки по ведомостям")


        def color_status(val):
            if val == "ОК":
                return 'background-color: #d4edda; color: #155724;'
            elif val == INCOMPLETE:
                return 'background-color: #e2e3f1; color: #383d6b;'
            elif val == TECHNICAL_ERROR:
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

                tot = len(df_res)
                ok_c = len(df_res[df_res['Статус аудита'] == 'ОК'])
                err_c = len(df_res[df_res['Статус аудита'] == 'Ошибка'])
                technical_c = len(df_res[df_res['Статус аудита'] == TECHNICAL_ERROR])
                incomplete_c = len(df_res[df_res['Статус аудита'] == INCOMPLETE])

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Всего позиций", tot)
                m2.metric("🟢 ОК", ok_c)
                m3.metric("🔴 Ошибка", err_c)
                m4.metric("🟡 Неполная проверка", incomplete_c)
                m5.metric("⚠️ Технический сбой", technical_c)

                if technical_c:
                    st.warning(f"Технических сбоев: {technical_c}. Эти позиции требуют повторной проверки; причина указана в деталях.")
                if incomplete_c:
                    st.info(f"Неполных проверок: {incomplete_c}. Отсутствие работы не установлено; сохраненные доказательства доступны для ручной или повторной проверки.")
                st.write(f"**Объект:** {meta_res['object_name']} | **Основание:** {meta_res['basis_code']}")
                st.info("💡 **Кликните по любой строке в таблице ниже**, чтобы мгновенно просмотреть этот лист PDF.")

                df_display = prepare_df_for_display(df_res)
                styler = df_display.style
                if hasattr(styler, 'map'):
                    styled_df = styler.map(color_status, subset=['Статус аудита'])
                else:
                    styled_df = styler.applymap(color_status, subset=['Статус аудита'])

                event = st.dataframe(
                    styled_df,
                    width="stretch",
                    on_select="rerun",
                    selection_mode="single-row",
                    key=f"df_table_{v_name}"
                )

                if event and event.selection and event.selection.rows:
                    selected_row_idx = event.selection.rows[0]
                    if selected_row_idx < len(tracking_info):
                        t_info = tracking_info[selected_row_idx]
                        sel_pdf_name = t_info["pdf_name"]
                        vor_pages = t_info.get("vor_pages", [])
                        found_p = t_info.get("found_page", None)
                        found_pages = list(t_info.get("found_pages") or ([found_p] if found_p else []))
                        evidence = list(t_info.get("evidence") or [])
                        sel_work_name = t_info["work_name"]

                        with st.container(border=True):
                            st.markdown(f"### 🔍 Инспектор: **{sel_work_name[:75]}**")
                            st.caption(f"Документ: `{sel_pdf_name}`")
                            conf = (f"Работа: {'да' if t_info.get('work_found') else 'нет'}; "
                                    f"Объём: {'да' if t_info.get('volume_confirmed') else 'нет'}; "
                                    f"Ед.: {'да' if t_info.get('unit_confirmed') else 'нет'}; "
                                    f"Прим.: {'да' if t_info.get('comment_confirmed') else 'нет'}; "
                                    f"ед. ПД: {t_info.get('found_unit', '-')}; "
                                    f"поиск: {'завершён' if t_info.get('search_complete') else 'неполный'}, "
                                    f"запросов: {t_info.get('requests', 0)}, "
                                    f"контекстов: {t_info.get('contexts', 0)}")
                            st.caption(conf)
                            if t_info.get("trace_url"):
                                st.link_button("Открыть trace позиции в Langfuse", t_info["trace_url"])
                            elif t_info.get("trace_id"):
                                st.caption(f"Langfuse trace: {t_info['trace_id']}")
                            kind_ru = {"work": "работа", "volume": "объём", "unit": "ед.", "comment": "прим."}
                            for item in evidence:
                                quote = str(item.get("quote", ""))
                                st.markdown(f"«{quote}» — стр. {item.get('page')} ({kind_ru.get(item.get('kind'), item.get('kind'))})")

                            doc_obj = st.session_state.pdf_pool.get(sel_pdf_name)

                            lbl_vor = f"📌 Страница по ссылке из ВОР (Стр. {', '.join(map(str, vor_pages)) if vor_pages else '-'})"
                            found_label = ", ".join(map(str, found_pages)) if found_pages else "Не найдена"
                            lbl_found = f"🎯 Фактическая страница с данными (Стр. {found_label})"

                            tab_vor, tab_found = st.tabs([lbl_vor, lbl_found])

                            with tab_vor:
                                st.info(
                                    "Лист, на который ссылается сметчик в ведомости (проверьте, почему там нет объемов):")
                                if doc_obj and vor_pages:
                                    for p_num in vor_pages:
                                        if 0 <= p_num - 1 < len(doc_obj):
                                            st.image(
                                                render_page(doc_obj, p_num - 1, dpi=250),
                                                caption=f"{sel_pdf_name} — Страница {p_num} (указана в ВОР)",
                                                width=850
                                            )
                                else:
                                    st.warning("Страница в ВОР не указана или выходит за пределы файла.")

                            with tab_found:
                                if doc_obj and found_pages:
                                    st.success(f"Листы в ПД, где фактически обнаружены данные (Стр. {', '.join(map(str, found_pages))}):")
                                    for p_num in found_pages:
                                        if 0 <= p_num - 1 < len(doc_obj):
                                            st.image(
                                                render_page(doc_obj, p_num - 1, dpi=250),
                                                caption=f"{sel_pdf_name} — Страница {p_num} (найдено моделью)",
                                                width=850
                                            )
                                else:
                                    st.info("Фактическая страница не подтверждена. Причина и область поиска указаны в деталях проверки.")

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

# ----------------- ВКЛАДКА 2: ПРОВЕРКА ОФОРМЛЕНИЯ -----------------
with main_tab2:
    st.subheader("Валидация оформления шаблона ВОР")
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

    if st.session_state.get("is_checking_rules", False):
        st.button("⏳ Выполняется проверка правил...", disabled=True, use_container_width=True)

        try:
            prog_placeholder = st.empty()
            prog_bar = prog_placeholder.progress(0, text="Инициализация проверки правил...")

            rule_results = validate_vor_formatting(check_file)
            total_rules = len(rule_results)

            for i, res in enumerate(rule_results):
                prog_bar.progress((i + 1) / total_rules, text=f"Проверка ячейки {res['coord']}...")
                time.sleep(0.01)

            prog_bar.progress(1.0, text="✅ Валидация оформления завершена!")
            time.sleep(0.8)
            prog_placeholder.empty()

            st.session_state.rules_results = rule_results
        finally:
            st.session_state.is_checking_rules = False

        st.rerun()

    else:
        if st.button("🚀 Проверить оформление", type="primary", key="btn_check_rules", use_container_width=True):
            st.session_state.is_checking_rules = True
            st.rerun()

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
