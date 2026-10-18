"""Headless audit orchestration: preflight, per-position workers, VOR runs.

No Streamlit dependency: progress is a ``progress(fraction, text)`` callback,
the LLM client factory and credentials are passed explicitly. Streamlit stays
a thin adapter that feeds inputs and renders ``run_audit`` results.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

import pandas as pd

from audit_core import (
    INCOMPLETE,
    TECHNICAL_ERROR,
    empty_result,
    parse_page_reference,
    result_level,
    technical_error,
)
from audit_llm import audit_position_hybrid
from audit_models import AuditResult
from audit_reports import build_user_report, generate_excel_report, generate_word_report
from audit_tracing import TracingRun
from pdf_binding import resolve_pdf_name, resolve_position_pdfs
from pdf_text import PDFTextSource
from vor_parse import load_vor_frame_positions, load_vor_positions


def get_optimized_page_indices(doc, page_str):
    # «9» в ВОР — девятая страница в PDF, независимо от колонтитула и штампа.
    return parse_page_reference(page_str, len(doc))


def preflight_bindings(loaded_vors, target_vor_selection, pdf_pool_keys):
    """Check PDF references per VOR; returns (rows, has_critical_error)."""
    preflight_rows = []
    has_critical_error = False

    for v_name in target_vor_selection:
        try:
            prev_positions, prev_meta = load_vor_positions(
                loaded_vors[v_name], v_name
            )
        except Exception:
            continue

        seen_bindings = set()
        for position in prev_positions:
            refs = position.explicit_refs
            binding_key = tuple(refs)
            if binding_key in seen_bindings:
                continue
            seen_bindings.add(binding_key)
            matched, _, _, error = resolve_position_pdfs(
                refs, pdf_pool_keys, prev_meta.get("basis_code", "")
            )
            if error:
                bind_status = "🔴 " + error
                has_critical_error = True
            elif not refs:
                bind_status = "🟡 По основанию / единственному PDF"
            else:
                statuses = [resolve_pdf_name(ref, pdf_pool_keys)[1] for ref in refs]
                bind_status = next((s for s in statuses if "🟡" in s), statuses[0])
            disk_status = (
                "🟢 Файл на месте"
                if refs and all(os.path.exists(ref) for ref in refs)
                else "⚠️ Путь не найден на ПК"
            )
            preflight_rows.append(
                {
                    "Ведомость (ВОР)": v_name,
                    "Путь в ячейке ВОР": "; ".join(refs) or "Не указан",
                    "Прямой путь на диске": disk_status,
                    "Статус привязки": bind_status,
                    "Фактический PDF": ", ".join(matched)
                    if not error
                    else "НЕ ПРИВЯЗАН",
                }
            )
    return preflight_rows, has_critical_error


def worker_check_row(task, audit_position):
    if task.get("prep_error"):
        audit = technical_error(task["prep_error"])
    elif task.get("bind_error"):
        audit = empty_result("Ошибка", task["bind_error"])
    elif task["text_index"] is None:
        wanted = "; ".join(task.get("explicit_refs") or []) or task["target_pdf_name"]
        audit = empty_result(
            "Ошибка", f"Не удалось привязать PDF к строке. Искали: '{wanted}'"
        )
    else:
        audit = audit_position(
            text_index=task["text_index"],
            target_indices=task["target_indices"],
            work_name=task["w_name"],
            volume=task["w_vol"],
            unit=task["w_unit"],
            ref_text=task["w_ref"],
            page_ref=task["w_page"],
            comment_text=task["w_comment"],
            formula_text=task["w_formula"],
        )
    validated = (
        audit
        if isinstance(audit, AuditResult)
        else AuditResult.from_legacy(dict(audit))
    )
    document_id = task.get("target_pdf_name") or ""
    if document_id:
        validated = validated.model_copy(
            update={
                "evidence": [
                    item.model_copy(update={"document_id": document_id})
                    for item in validated.evidence
                ]
            }
        )
    audit = validated.to_legacy()
    evidence = [dict(e) for e in (audit.get("evidence") or []) if isinstance(e, dict)]
    found_pages = sorted(
        {e["page"] for e in evidence if isinstance(e.get("page"), int)}
    )
    if not found_pages:
        for p in audit.get("found_pages") or []:
            if isinstance(p, int) and p not in found_pages:
                found_pages.append(p)
        found_pages = sorted(found_pages)
    raw_found_p = str(audit.get("found_page") or "-").strip()
    first_p = int(raw_found_p) if raw_found_p.isdigit() else None
    if first_p is None and found_pages:
        first_p = found_pages[0]
    elif first_p is not None and first_p not in found_pages:
        found_pages = sorted(set(found_pages + [first_p]))
    display_page = ("Стр. " + ", ".join(map(str, found_pages))) if found_pages else "-"
    details_text = str(audit.get("discrepancy_details") or "")
    final_status = audit.get("status", "Ошибка")
    # Core owns out-of-reference findings; the adapter
    # additionally preserves malformed/part-invalid input.
    vor_pages_int = task["primary_phys_pages"] if task["primary_phys_pages"] else []
    if (
        not task["target_indices"] or "недопустимые" in task["loc_desc"]
    ) and not task.get("bind_error"):
        if final_status not in {TECHNICAL_ERROR, INCOMPLETE}:
            final_status = "Ошибка"
        details_text = task["loc_desc"] + ". " + details_text

    def _yn(v):
        return "да" if v else "нет"

    work_found = bool(audit.get("work_found"))
    vol_ok = bool(audit.get("volume_confirmed"))
    unit_ok = bool(audit.get("unit_confirmed"))
    comm_ok = bool(audit.get("comment_confirmed"))
    confirm_text = (
        f"Работа: {_yn(work_found)}; Объём: {_yn(vol_ok)}; "
        f"Ед.: {_yn(unit_ok)}; Прим.: {_yn(comm_ok)}"
    )
    kind_ru = {"work": "работа", "volume": "объём", "unit": "ед.", "comment": "прим."}
    evidence_text = (
        "; ".join(
            f"[Стр. {e.get('page')}, {kind_ru.get(e.get('kind'), e.get('kind'))}] «{e.get('quote', '')}»"
            for e in evidence
        )
        or "-"
    )
    res_item = {
        "Статус аудита": final_status,
        "Фактический объем": audit.get("found_volume", "-"),
        "Фактическая страница в ПД (найдено моделью)": display_page,
        "Детали расхождений": details_text,
        "[ПД] Найденная единица": audit.get("found_unit", "-"),
        "[ПД] Подтверждения (работа/объём/ед./прим.)": confirm_text,
        "[ПД] Доказательства (цитаты со страницами)": evidence_text,
        "[ПД] Найденные страницы": ", ".join(map(str, found_pages))
        if found_pages
        else "-",
        "[ПД] Поиск завершён": "да" if audit.get("search_complete") else "нет",
        "[ПД] Запросов": audit.get("requests", 0),
        "[ПД] Контекстов": audit.get("contexts", 0),
        "work_found": work_found,
        "volume_confirmed": vol_ok,
        "unit_confirmed": unit_ok,
        "comment_confirmed": comm_ok,
        "found_pages": list(found_pages),
        "evidence": evidence,
        "search_complete": bool(audit.get("search_complete")),
        "requests": audit.get("requests", 0),
        "contexts": audit.get("contexts", 0),
    }
    show_p = (
        list(found_pages) if found_pages else (vor_pages_int if vor_pages_int else [1])
    )
    track_item = {
        "pdf_name": task["target_pdf_name"],
        "pages_to_show": show_p,
        "vor_pages": vor_pages_int,
        "found_page": first_p,
        "found_pages": list(found_pages),
        "evidence": evidence,
        "work_found": work_found,
        "volume_confirmed": vol_ok,
        "unit_confirmed": unit_ok,
        "comment_confirmed": comm_ok,
        "found_unit": audit.get("found_unit", "-"),
        "found_volume": audit.get("found_volume", "-"),
        "volume_calculation": audit.get("volume_calculation"),
        "search_complete": bool(audit.get("search_complete")),
        "requests": audit.get("requests", 0),
        "contexts": audit.get("contexts", 0),
        "work_name": task["w_name"],
    }
    return task["idx"], res_item, track_item


def traced_worker_check_row(task, audit_position):
    with task["position_trace"].stage("audit-position") as observation:
        result = worker_check_row(task, audit_position)
        final = result[1]["Статус аудита"]
        observation.update(
            output=result[1],
            status_message=(
                f"{final}; {result[1]['Фактическая страница в ПД (найдено моделью)']}"
            ),
            level=result_level(final),
        )
        return result


def _failure_items(exc, task):
    res_item = {
        "Статус аудита": TECHNICAL_ERROR,
        "Фактический объем": "-",
        "Фактическая страница в ПД (найдено моделью)": "-",
        "Детали расхождений": f"Сбой обработки позиции: {type(exc).__name__}: {str(exc)[:500]}",
        "[ПД] Найденная единица": "-",
        "[ПД] Подтверждения (работа/объём/ед./прим.)": "Работа: нет; Объём: нет; Ед.: нет; Прим.: нет",
        "[ПД] Доказательства (цитаты со страницами)": "-",
        "[ПД] Найденные страницы": "-",
        "[ПД] Поиск завершён": "нет",
        "[ПД] Запросов": 0,
        "[ПД] Контекстов": 0,
        "work_found": False,
        "volume_confirmed": False,
        "unit_confirmed": False,
        "comment_confirmed": False,
        "found_pages": [],
        "evidence": [],
        "search_complete": False,
        "requests": 0,
        "contexts": 0,
    }
    track_item = {
        "pdf_name": task["target_pdf_name"],
        "vor_pages": task["primary_phys_pages"],
        "found_page": None,
        "found_pages": [],
        "evidence": [],
        "work_found": False,
        "volume_confirmed": False,
        "unit_confirmed": False,
        "comment_confirmed": False,
        "found_unit": "-",
        "found_volume": "-",
        "search_complete": False,
        "requests": 0,
        "contexts": 0,
        "work_name": task["w_name"],
    }
    return res_item, track_item


def run_audit(
    loaded_vors,
    target_vor_selection,
    pdf_pool,
    *,
    api_key,
    folder,
    model_name,
    trace_run=None,
    max_workers=5,
    progress=None,
    client_factory=None,
    sleeper=None,
):
    """Audit selected VORs headless.

    Returns ``{"results": {v_name: {"df", "meta", "tracking", "excel_bytes",
    "word_bytes"}}, "issues": [...]}``. Owns PDF text sources (closed on exit);
    the caller owns ``pdf_pool`` documents and ``trace_run``.
    """
    audit_position = partial(
        audit_position_hybrid,
        api_key=api_key,
        folder=folder,
        model_name=model_name,
        client_factory=client_factory,
        sleeper=sleeper,
    )
    own_trace = trace_run is None
    trace_run = trace_run or TracingRun()
    notify = progress or (lambda fraction, text: None)
    results = {}
    issues = []
    pdf_text_sources = {}
    try:
        vors_parsed = {}
        total_items = 0
        for v_name in target_vor_selection:
            try:
                df_w, positions_w, meta = load_vor_frame_positions(
                    loaded_vors[v_name], v_name
                )
            except ValueError as exc:
                if "обязательные колонки" in str(exc):
                    issues.append(f"{v_name}: {exc}")
                    continue
                issues.append(
                    f"Не удалось прочитать {v_name}: {type(exc).__name__}: {str(exc)[:300]}"
                )
                continue
            except Exception as exc:
                issues.append(
                    f"Не удалось прочитать {v_name}: {type(exc).__name__}: {str(exc)[:300]}"
                )
                continue
            if not positions_w:
                issues.append(f"{v_name}: позиции для проверки не найдены.")
                continue
            vors_parsed[v_name] = (df_w, positions_w, meta)
            total_items += len(positions_w)

        notify(0, "Инициализация многопоточной проверки...")
        current_row_idx = 0

        for v_name, (df_works, positions, meta) in vors_parsed.items():
            vol_col = next(
                (
                    c
                    for c in df_works.columns
                    if "объем" in str(c).lower().replace("ё", "е")
                    or "количество" in str(c).lower().replace("ё", "е")
                ),
                None,
            )
            unit_col = next(
                (
                    c
                    for c in df_works.columns
                    if "ед." in str(c).lower()
                    or "единица измерения" in str(c).lower()
                    or "единицы измерения" in str(c).lower()
                ),
                None,
            )
            page_col = next(
                (
                    c
                    for c in df_works.columns
                    if any(
                        m in str(c).lower().replace("ё", "е")
                        for m in (
                            "номер страниц",
                            "номера страниц",
                            "ссылка на страницу",
                        )
                    )
                ),
                None,
            )
            file_col = [
                c for c in df_works.columns if "наименование файла" in str(c).lower()
            ]
            file_col_name = file_col[0] if file_col else None

            tasks = []
            for position in positions:
                r_idx = position.index
                w_name = position.work_name
                w_vol = position.volume
                w_unit = position.unit
                w_ref = position.reference
                w_page = position.page_ref
                w_comment = position.comment
                w_formula = position.formula
                position_trace = trace_run.position(
                    input={
                        "work": w_name,
                        "volume": w_vol,
                        "unit": w_unit,
                        "reference": w_ref,
                        "pages": w_page,
                        "comment": w_comment,
                        "formula": w_formula,
                    },
                    metadata={
                        "vor_file": v_name,
                        "position_index": r_idx,
                        "provider": "Yandex Cloud (Qwen)",
                        "model": model_name,
                    },
                )

                target_pdf_doc = None
                target_pdf_name = ""
                bind_error = None
                explicit_refs = list(position.explicit_refs)
                pdfs, _, _, bind_error = resolve_position_pdfs(
                    explicit_refs, pdf_pool.keys(), meta.get("basis_code", "")
                )
                if not bind_error and pdfs:
                    target_pdf_name = pdfs[0]
                    target_pdf_doc = pdf_pool[target_pdf_name]

                target_indices = []
                loc_desc = "Страница не указана"
                primary_phys_pages = []
                text_index = None
                prep_error = None
                with position_trace.stage(
                    "prepare-position", input={"pages": w_page}
                ) as preparation:
                    preparation.update(
                        metadata={
                            "pdf_name": target_pdf_name,
                            "pdf_bound": target_pdf_doc is not None,
                        }
                    )
                    try:
                        if target_pdf_doc is not None:
                            with position_trace.stage(
                                "parse-page-reference", input=w_page
                            ) as observation:
                                target_indices, loc_desc, primary_phys_pages = (
                                    get_optimized_page_indices(target_pdf_doc, w_page)
                                )
                                observation.update(
                                    output={
                                        "pages": primary_phys_pages,
                                        "description": loc_desc,
                                    },
                                    status_message=loc_desc[:200],
                                    level="DEFAULT" if target_indices else "WARNING",
                                )
                            if target_pdf_name not in pdf_text_sources:
                                notify(
                                    current_row_idx / max(total_items, 1),
                                    f"Подготовка Markdown и таблиц PDF: {target_pdf_name}",
                                )
                                with position_trace.stage(
                                    "index-native-pdf",
                                    input={
                                        "pdf_name": target_pdf_name,
                                        "extractor": "pymupdf4llm",
                                    },
                                ) as observation:
                                    source = PDFTextSource(target_pdf_doc)
                                    pdf_text_sources[target_pdf_name] = source
                                    observation.update(
                                        output={
                                            "pages": source.page_count,
                                            "page_chars": [
                                                len(t)
                                                for t in source.native_index.texts
                                            ],
                                            "pending_ocr": [
                                                i + 1 for i in source.pending_ocr
                                            ],
                                            "errors": source.native_index.errors,
                                        },
                                        status_message=(
                                            f"Страниц: {source.page_count}; "
                                            f"сканов: {len(source.pending_ocr)}; "
                                            f"сбоев чтения: {len(source.native_index.errors)}"
                                        ),
                                        level="ERROR"
                                        if source.native_index.errors
                                        else "DEFAULT",
                                    )
                            source = pdf_text_sources[target_pdf_name]
                            before_ocr, before_hits = (
                                source.ocr_count,
                                source.cache_hits,
                            )
                            with position_trace.stage(
                                "prepare-ocr-snapshot",
                                input={"pages": primary_phys_pages, "work": w_name},
                            ) as observation:
                                text_index = source.for_position(
                                    target_indices, w_name, w_vol, w_ref, w_comment
                                )
                                new_ocr = source.ocr_count - before_ocr
                                ocr_hits = source.cache_hits - before_hits
                                observation.update(
                                    output={
                                        "new_ocr_pages": new_ocr,
                                        "cache_hits": ocr_hits,
                                        "pending_ocr": [
                                            i + 1 for i in text_index.pending_ocr
                                        ],
                                        "errors": {
                                            i + 1: e
                                            for i, e in text_index.errors.items()
                                        },
                                    },
                                    status_message=(
                                        f"Новых OCR: {new_ocr}; из кеша: {ocr_hits}; "
                                        f"отложено: {len(text_index.pending_ocr)}; "
                                        f"ошибок: {len(text_index.errors)}"
                                    ),
                                    level="ERROR" if text_index.errors else "DEFAULT",
                                )
                    except Exception as exc:
                        prep_error = f"Подготовка позиции не выполнена: {type(exc).__name__}: {str(exc)[:300]}"
                        preparation.update(
                            level="ERROR",
                            status_message=prep_error[:200],
                            output={"prep_error": prep_error},
                        )
                    preparation.update(
                        output={
                            "pdf_bound": target_pdf_doc is not None,
                            "pages": primary_phys_pages,
                            "reference_description": loc_desc,
                            "prep_error": prep_error,
                        },
                        status_message=f"{target_pdf_name or 'PDF не привязан'}; {loc_desc[:200]}",
                        level="ERROR"
                        if prep_error
                        else ("DEFAULT" if target_pdf_doc is not None else "WARNING"),
                    )

                tasks.append(
                    {
                        "idx": r_idx,
                        "position_trace": position_trace,
                        "w_name": w_name,
                        "w_vol": w_vol,
                        "w_unit": w_unit,
                        "w_ref": w_ref,
                        "w_page": w_page,
                        "w_comment": w_comment,
                        "w_formula": w_formula,
                        "text_index": text_index,
                        "target_pdf_name": target_pdf_name,
                        "explicit_refs": list(explicit_refs),
                        "bind_error": bind_error,
                        "prep_error": prep_error,
                        "target_indices": target_indices,
                        "loc_desc": loc_desc,
                        "primary_phys_pages": primary_phys_pages,
                    }
                )

            results_list = [None] * len(tasks)
            row_page_tracking = [None] * len(tasks)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task = {
                    executor.submit(traced_worker_check_row, t, audit_position): t
                    for t in tasks
                }

                for future in as_completed(future_to_task):
                    try:
                        r_idx, res_item, track_item = future.result()
                    except Exception as exc:
                        task = future_to_task[future]
                        r_idx = task["idx"]
                        res_item, track_item = _failure_items(exc, task)
                    position_trace = future_to_task[future]["position_trace"]
                    position_trace.finish(res_item)
                    track_item["trace_id"] = position_trace.trace_id
                    track_item["trace_url"] = position_trace.url()
                    results_list[r_idx] = res_item
                    row_page_tracking[r_idx] = track_item

                    current_row_idx += 1
                    pct = current_row_idx / total_items
                    notify(
                        pct,
                        f"Многопоточная проверка [{current_row_idx}/{total_items}]: {v_name} (5 потоков)...",
                    )

            cols_to_drop = [
                c
                for c in df_works.columns
                if str(c).startswith("Unnamed")
                or str(c).strip()
                in ["1", "2", "3", "4", "5", "6", "6.1", "6.2", "7", "8", "9"]
            ]
            df_works_clean = df_works.drop(columns=cols_to_drop, errors="ignore").copy()

            if vol_col in df_works_clean.columns:

                def clean_vor_val(v):
                    s = str(v).strip()
                    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
                        return s[:-2]
                    return s

                df_works_clean[vol_col] = df_works_clean[vol_col].apply(clean_vor_val)

            rename_map = {}
            if vol_col in df_works_clean.columns:
                rename_map[vol_col] = "[ВОР] Заявленный объем"
            if unit_col in df_works_clean.columns:
                rename_map[unit_col] = "[ВОР] Ед. изм."
            if page_col in df_works_clean.columns:
                rename_map[page_col] = "[ВОР] Ссылка на страницу"
            df_works_clean.rename(columns=rename_map, inplace=True)
            # Повторный прогон файла с результатами: старые итоги
            # перезаписываются свежими, а не дублируются в concat.
            stale = [
                c
                for c in df_works_clean.columns
                if c in ("Статус аудита", "Детали расхождений")
                or str(c).startswith("[ПД]")
            ]
            df_works_clean.drop(columns=stale, inplace=True, errors="ignore")

            df_audit = pd.DataFrame(
                [
                    {
                        "Статус аудита": r["Статус аудита"],
                        "[ПД] Найденный объем (нейросеть)": r["Фактический объем"],
                        "[ПД] Фактическая страница": r[
                            "Фактическая страница в ПД (найдено моделью)"
                        ],
                        "Детали расхождений": r["Детали расхождений"],
                        "[ПД] Найденная единица": r.get("[ПД] Найденная единица", "-"),
                        "[ПД] Подтверждения (работа/объём/ед./прим.)": r.get(
                            "[ПД] Подтверждения (работа/объём/ед./прим.)", "-"
                        ),
                        "[ПД] Доказательства (цитаты со страницами)": r.get(
                            "[ПД] Доказательства (цитаты со страницами)", "-"
                        ),
                        "[ПД] Найденные страницы": r.get(
                            "[ПД] Найденные страницы", "-"
                        ),
                        "[ПД] Поиск завершён": r.get("[ПД] Поиск завершён", "нет"),
                        "[ПД] Запросов": r.get("[ПД] Запросов", 0),
                        "[ПД] Контекстов": r.get("[ПД] Контекстов", 0),
                    }
                    for r in results_list
                ],
                index=df_works_clean.index,
            )

            df_final = pd.concat([df_works_clean, df_audit], axis=1)

            report_tasks = [
                {**task, "file_refs": df_works.iloc[i].get(file_col_name, "")}
                for i, task in enumerate(tasks)
            ]
            user_report = build_user_report(df_final, row_page_tracking, report_tasks)
            excel_bytes = generate_excel_report(user_report)
            word_bytes = generate_word_report(user_report, meta)

            results[v_name] = {
                "df": df_final,
                "meta": meta,
                "tracking": row_page_tracking,
                "excel_bytes": excel_bytes,
                "word_bytes": word_bytes,
            }

        return {"results": results, "issues": issues}
    finally:
        for source in list(pdf_text_sources.values()):
            try:
                source.close()
            except Exception:
                pass
        if own_trace:
            trace_run.close()
