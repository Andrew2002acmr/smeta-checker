"""Headless audit orchestration: preflight, per-position workers, VOR runs.

No UI dependency: progress is a ``progress(fraction, text)`` callback,
the LLM client factory and credentials are passed explicitly.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

from audit_core import (
    TECHNICAL_ERROR,
    build_claim_specs,
    empty_result,
    parse_page_reference,
    result_level,
    technical_error,
)
from audit_llm import audit_position_hybrid
from audit_models import AuditResult, expected_scope_hint, position_source_dict, claim_status
from audit_snapshot import decode_snapshot, encode_snapshot
from audit_tracing import TracingRun
from pdf_binding import (
    clean_pdf_name,
    index_designations,
    resolve_pdf_name,
    resolve_position_pdfs,
    verify_designations,
)
_SHEET_STAMP_RE = None
_SHEET_MAP_CACHE: dict[int, dict[int, int]] = {}


def _volume_sheet_map(doc) -> dict[int, int]:
    """Map printed volume sheet numbers to physical PDF pages (1-based).

    VOR page references cite the bound volume ("9" = sheet 9 of the report),
    while the PDF file starts with covers/registers, so physical page 9 is
    a different sheet. Each body page carries a header stamp ``N из M``;
    the most frequent total M identifies the volume, and the first
    occurrence of each sheet builds the map. Returns {} when no usable
    stamps exist, in which case callers keep the legacy 1:1 numbering.
    """
    global _SHEET_STAMP_RE
    import re
    from collections import Counter

    if _SHEET_STAMP_RE is None:
        _SHEET_STAMP_RE = re.compile(r"(?m)^\s*(\d+)\s+из\s+(\d+)\s*$")
    key = id(doc)
    if key in _SHEET_MAP_CACHE:
        return _SHEET_MAP_CACHE[key]
    per_page: dict[int, tuple[int, int]] = {}
    totals: Counter[int] = Counter()
    for number in range(1, len(doc) + 1):
        try:
            head = doc[number - 1].get_text()[:600]
        except Exception:
            continue
        match = _SHEET_STAMP_RE.findall(head)
        if not match:
            continue
        sheet, total = int(match[0][0]), int(match[0][1])
        totals[total] += 1
        per_page[number] = (sheet, total)
    sheet_map: dict[int, int] = {}
    if totals:
        main_total = totals.most_common(1)[0][0]
        # A real volume stamp repeats on most pages; a couple of stray
        # "N из M" lines inside body text must not rewire pagination.
        if totals[main_total] >= min(5, max(1, len(doc) // 3)):
            for phys, (sheet, total) in sorted(per_page.items()):
                if total == main_total and sheet not in sheet_map:
                    sheet_map[sheet] = phys
    if len(_SHEET_MAP_CACHE) > 64:
        _SHEET_MAP_CACHE.clear()
    _SHEET_MAP_CACHE[key] = sheet_map
    return sheet_map


def get_optimized_page_indices(doc, page_str):
    """Resolve a VOR page reference to physical PDF indices (0-based).

    Prefers volume sheet numbers from the ``N из M`` header stamps when the
    document carries them; otherwise falls back to legacy file-order pages.
    """
    indices, description, pages = parse_page_reference(page_str, len(doc))
    sheet_map = _volume_sheet_map(doc)
    if not sheet_map:
        return indices, description, pages
    mapped: list[int] = []
    unmapped: list[int] = []
    for number in pages:
        phys = sheet_map.get(number)
        if phys is None:
            unmapped.append(number)
        elif phys - 1 not in mapped:
            mapped.append(phys - 1)
    if not mapped:
        return indices, description, pages
    note = f"Стр. {', '.join(map(str, pages))} тома → стр. PDF {', '.join(map(str, sorted(p + 1 for p in mapped)))}"
    if unmapped:
        note += f"; без штампа тома: {', '.join(map(str, unmapped))} (взяты как есть)"
        for number in unmapped:
            if 1 <= number <= len(doc) and number - 1 not in mapped:
                mapped.append(number - 1)
    return mapped, note, pages


def preflight_bindings(loaded_vors, target_vor_selection, pdf_pool_keys, bindings=None):
    """Check PDF references per VOR; returns (rows, has_critical_error)."""
    from vor_parse import load_vor_positions

    preflight_rows = []
    has_critical_error = False

    for v_name in target_vor_selection:
        try:
            prev_positions, prev_meta = load_vor_positions(loaded_vors[v_name], v_name)
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
                refs, pdf_pool_keys, prev_meta.get("basis_code", ""), bindings
            )
            if error:
                bind_status = "🔴 " + error
                has_critical_error = True
            elif any(clean_pdf_name(ref) in (bindings or {}) for ref in refs):
                bind_status = "🟢 Явная привязка пользователя"
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


def independent_position_checks(task):
    """Workbook/schedule facts that never depend on the auditor outcome."""
    workbook_notes: list = []
    if task.get("calculation"):
        from vor_consistency import check_display_consistency
        workbook_notes = check_display_consistency(
            task.get("calculation"), task.get("w_formula", ""),
            task.get("formula_refs"))
        index = task.get("text_index")
        pages = task.get("primary_phys_pages") or []
        if index is not None and pages:
            from vor_consistency import check_length_operands
            texts = getattr(index, "texts", []) or []
            related = {p: texts[p - 1] for p in pages
                       if 0 < p <= len(texts)}
            workbook_notes += check_length_operands(
                task.get("calculation"), related)
    workbook_notes += [dict(h) for h in (task.get("scheme_notes") or [])]
    quantity_kind = "direct"
    if task.get("text_index") is not None:
        from quantity_graph import classify_quantity
        quantity_kind, _ = classify_quantity(
            task.get("w_name", ""), task.get("w_comment", ""), task.get("w_unit", ""),
            task.get("w_formula", ""), task.get("calculation", {}))
    return workbook_notes, quantity_kind


def _independent_checks(task, notes, consumption=None):
    checks = []
    index = task.get("text_index")
    reference = str(task.get("w_page") or "").strip()
    if index is not None and reference.casefold() not in {"", "-", "nan", "none", "null"}:
        indices, description, _ = parse_page_reference(reference, len(index.texts))
        if not indices or "недопустимые" in description:
            checks.append({
                "claim_id": "reference:pages", "kind": "basis", "required": True,
                "state": "contradiction", "reasons": ["invalid_pdf_page_reference"],
                "facts": [],
            })
    for index, note in enumerate(notes):
        kind = note.get("kind", "unknown")
        checks.append({
            "claim_id": f"workbook:{note.get('display_cell', note.get('cell', index))}:{kind}",
            "kind": "workbook", "required": True,
            "state": ("review_required" if kind in {
                "scheme-divisor-mismatch", "length-operand-conflicts"} else "contradiction"),
            "reasons": [kind], "facts": [],
        })
    calculation = task.get("calculation") or {}
    from vor_formula import has_formula_text
    if calculation and (calculation.get("status") in {"calculated", "error"}
                        or has_formula_text(calculation.get("expression"))):
        state = calculation.get("status")
        checks.append({
            "claim_id": "workbook:formula", "kind": "workbook", "required": True,
            "state": ("contradiction" if state == "error" else
                      "confirmed" if state == "calculated" else "review_required"),
            "reasons": ["workbook_arithmetic_" + str(state)], "facts": [],
        })
    if consumption:
        checks.append({
            "claim_id": "quantity:consumption", "kind": "quantity", "required": True,
            "state": ("confirmed" if consumption.get("matches") is True else
                      "contradiction" if consumption.get("matches") is False else "review_required"),
            "reasons": list(consumption.get("uncertainties") or ["schedule_reconciliation"]),
            "facts": [],
        })
    return checks


def _position_view(task):
    """Minimal position view for claim specs from a worker task dict."""
    from types import SimpleNamespace
    return SimpleNamespace(
        work_name=task.get("w_name", ""),
        volume=task.get("w_vol", ""),
        unit=task.get("w_unit", ""),
        comment=task.get("w_comment", ""),
        formula=task.get("w_formula", ""),
        position_type=task.get("position_type", ""),
        calculation=task.get("calculation", {}),
        position_source=task.get("position_source"),
    )


def worker_check_row(task, audit_position):
    workbook_notes, quantity_kind = independent_position_checks(task)
    consumption = None
    if task.get("prep_error"):
        audit = technical_error(task["prep_error"])
    elif task.get("bind_error"):
        audit = empty_result("Ошибка", task["bind_error"])
    elif task.get("text_index") is None:
        wanted = "; ".join(task.get("explicit_refs") or []) or task["target_pdf_name"]
        audit = empty_result(
            "Ошибка", f"Не удалось привязать PDF к строке. Искали: '{wanted}'"
        )
    else:
        declared = list(task.get("designation_refs") or [])
        designation_index = index_designations(
            (number, text)
            for number, text in enumerate(task["text_index"].texts, start=1)
        )
        found_designations, missing_designations = verify_designations(
            declared, designation_index
        )
        task["designation_found"] = found_designations
        task["designation_index"] = designation_index
        if missing_designations:
            files = task.get("target_pdf_name") or "загруженной ПД"
            missing = ", ".join(f"'{item}'" for item in missing_designations)
            audit = empty_result(
                "Ошибка",
                f"ВОР ссылается на {missing}, "
                f"в {files} обозначения отсутствуют",
            )
            audit["checks"] = [
                {
                    "claim_id": "basis:designation",
                    "kind": "basis",
                    "state": "contradiction",
                    "reasons": ["designation_missing_in_pd"],
                    "facts": [],
                    "required": True,
                }
            ]
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
                position_type=task.get("position_type", ""),
                calculation=task.get("calculation", {}),
                expected_scope=expected_scope_hint(task.get("position_source")),
                claim_specs=task.get("claim_specs") or build_claim_specs(
                    _position_view(task)),
            )
    validated = (
        audit
        if isinstance(audit, AuditResult)
        else AuditResult.from_legacy(dict(audit))
    )
    document_id = task.get("target_pdf_name") or ""
    update: dict = {}
    if document_id:
        update["evidence"] = [
            item.model_copy(update={"document_id": document_id})
            for item in validated.evidence
        ]
    if validated.position_source is None and task.get("position_source") is not None:
        from audit_models import PositionSource
        source = task.get("position_source")
        update["position_source"] = (
            PositionSource.model_validate(source)
            if isinstance(source, dict) else source)
    if update:
        validated = validated.model_copy(update=update)
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
    if task.get("text_index") is not None:
        from quantity_graph import reconcile_position_schedule
        consumption = reconcile_position_schedule(
            task,
            {"evidence": [{"page": e.get("page"), "kind": e.get("kind")}
                          for e in evidence]},
        )
        if consumption is not None and consumption.get("matches") is False:
            from audit_measurements import format_ru
            chain = consumption.get("stock_chain") or {}
            if chain:
                details_text += (
                    f" Расхождение с ПД: потребность в материале с запасом — "
                    f"в ВОР {chain['material_volume']} {task.get('w_unit')} "
                    f"(работа {chain['work_volume']} × {chain['factor']}, позиция №{chain['material_number']}), "
                    f"в спецификации — {consumption['schedule_volume']} {task.get('w_unit')} "
                    f"(стр. {consumption['schedule_page']}, {consumption['schedule_item']}). "
                    f"Разница — {consumption['delta']} {task.get('w_unit')} "
                    f"({consumption['delta_percent']}%). "
                    f"В ВОР принят расход {consumption['rate']} {consumption['unit']}, "
                    f"из количества в ПД следует {consumption['derived_rate']} {consumption['unit']}."
                )
            else:
                details_text += (
                    f" Расхождение с ПД: в ВОР {format_ru(task.get('w_vol'))} {task.get('w_unit')}, "
                    f"в спецификации — {consumption['schedule_volume']} {task.get('w_unit')} "
                    f"(стр. {consumption['schedule_page']}, {consumption['schedule_item']}). "
                    f"Разница — {consumption['delta']} {task.get('w_unit')} "
                    f"({consumption['delta_percent']}%). "
                    f"В ВОР принят расход {consumption['rate']} {consumption['unit']}, "
                    f"из количества в ПД следует {consumption['derived_rate']} {consumption['unit']}."
                )
        elif (consumption is not None and consumption.get("matches") is True
                and (consumption.get("stock_chain") or {})):
            chain = consumption["stock_chain"]
            details_text += (
                f" Потребность в материале сходится: работа {chain['work_volume']} "
                f"× {chain['factor']} = {chain['material_volume']} {task.get('w_unit')} "
                f"(позиция №{chain['material_number']}), в спецификации — "
                f"{consumption['schedule_volume']} {task.get('w_unit')} "
                f"(стр. {consumption['schedule_page']}). "
                f"Обоснованность нормы {consumption['rate']} {consumption['unit']} "
                f"в ПД не подтверждена — отдельный вопрос."
            )
    # Core owns out-of-reference findings; the adapter
    # additionally preserves malformed/part-invalid input.
    for note in workbook_notes:
        kind = note.get("kind", "")
        if kind in {"scheme-divisor-mismatch", "length-operand-conflicts"}:
            details_text += f" Вопрос к исходным данным ВОР: {note['message']}."
        else:
            details_text += f" Внутренняя несогласованность ВОР: {note['message']}."
    checks = list(audit.get("checks") or [])
    checks.extend(_independent_checks(task, workbook_notes, consumption))
    if checks:
        errors = list(audit.get("execution_errors") or [])
        if audit.get("status") == TECHNICAL_ERROR:
            errors.append({"kind": "provider", "message": details_text or "Execution failed"})
        pdf_checks = [check for check in audit.get("checks") or []
                      if check.get("kind") != "workbook"]
        final_status = claim_status(checks, errors, pdf_evaluated=bool(pdf_checks))
        audit["checks"] = checks
        audit["execution_errors"] = errors
    vor_pages_int = task["primary_phys_pages"] if task["primary_phys_pages"] else []
    if any(check["claim_id"] == "reference:pages" for check in checks):
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
    kind_ru = {"work": "работа", "volume": "объём", "unit": "ед.", "comment": "прим.", "technology": "технология"}
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
        "budget_tokens": audit.get("budget_tokens"),
        "usage": dict(audit.get("usage") or {}),
        "technology": audit.get("technology", {}),
        "quantity_basis": audit.get("quantity_basis", "unknown"),
        "consumption": consumption if consumption is not None else audit.get("consumption"),
        "workbook_calculation": audit.get("workbook_calculation", {}),
        "workbook_notes": [dict(n) for n in workbook_notes],
        "position_source": task.get("position_source"),
        "checks": [dict(check) for check in audit.get("checks", [])],
        "execution_errors": [dict(item) for item in audit.get("execution_errors", [])],
        "evaluation_version": audit.get("evaluation_version", ""),
        "corpus_id": task.get("corpus_id", ""),
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
        "technology": audit.get("technology", {}),
        "quantity_basis": audit.get("quantity_basis", "unknown"),
        "consumption": consumption if consumption is not None else audit.get("consumption"),
        "quantity_kind": quantity_kind,
        "workbook_calculation": audit.get("workbook_calculation", {}),
        "workbook_notes": [dict(n) for n in workbook_notes],
        "position_source": task.get("position_source"),
        "position_type": task.get("position_type", ""),
        "search_complete": bool(audit.get("search_complete")),
        "requests": audit.get("requests", 0),
        "contexts": audit.get("contexts", 0),
        "budget_tokens": audit.get("budget_tokens"),
        "usage": dict(audit.get("usage") or {}),
        "work_name": task["w_name"],
        "checks": [dict(check) for check in audit.get("checks", [])],
        "execution_errors": [dict(item) for item in audit.get("execution_errors", [])],
        "evaluation_version": audit.get("evaluation_version", ""),
        "corpus_id": task.get("corpus_id", ""),
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
    """Use the same independent checks and precedence after an auditor crash."""
    failed = {**task, "prep_error":
              f"Сбой обработки позиции: {type(exc).__name__}: {str(exc)[:500]}"}
    _, result, tracking = worker_check_row(failed, None)
    return result, tracking


def build_run_artifacts(df_works, meta, tasks, tracking, results):
    """Build df/word from finished position data; shared by run_audit and tasks.

    Column detection, stale-result cleanup, renames and the report-task
    projection are byte-identical to the historical inline block: Celery
    finalize must emit the same exports as the in-process path.
    """
    import pandas as pd
    from audit_reports import build_user_report, generate_word_report

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
    file_col = [c for c in df_works.columns if "наименование файла" in str(c).lower()]
    file_col_name = file_col[0] if file_col else None
    cols_to_drop = [
        c
        for c in df_works.columns
        if str(c).startswith("Unnamed")
        or str(c).strip() in ["1", "2", "3", "4", "5", "6", "6.1", "6.2", "7", "8", "9"]
    ]
    df_works_clean = df_works.drop(columns=cols_to_drop, errors="ignore").copy()

    if vol_col in df_works_clean.columns:

        def clean_vor_val(v):
            from audit_measurements import format_ru

            s = str(v).strip()
            if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
                return s[:-2]
            return format_ru(s)

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
        if c in ("Статус аудита", "Детали расхождений") or str(c).startswith("[ПД]")
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
                "[ПД] Найденные страницы": r.get("[ПД] Найденные страницы", "-"),
                "[ПД] Поиск завершён": r.get("[ПД] Поиск завершён", "нет"),
                "[ПД] Запросов": r.get("[ПД] Запросов", 0),
                "[ПД] Контекстов": r.get("[ПД] Контекстов", 0),
            }
            for r in results
        ],
        index=df_works_clean.index,
    )

    df_final = pd.concat([df_works_clean, df_audit], axis=1)

    report_tasks = [
        {**task, "file_refs": df_works.iloc[i].get(file_col_name, "")}
        for i, task in enumerate(tasks)
    ]
    user_report = build_user_report(df_final, tracking, report_tasks)
    return {
        "df": df_final,
        "user_report": user_report,
        "word_bytes": generate_word_report(user_report, meta),
    }


def run_audit(
    loaded_vors,
    target_vor_selection,
    pdf_pool,
    *,
    api_key,
    folder,
    model_name,
    trace_run=None,
    max_workers=8,
    progress=None,
    client_factory=None,
    sleeper=None,
    store=None,
    project_name="default",
    pdf_sources=None,
    run_id=None,
    pdf_versions=None,
    allowed_llm_hosts=("ai.api.cloud.yandex.net",),
    llm_base_url="https://ai.api.cloud.yandex.net/v1",
    llm_provider="Yandex Cloud (Qwen)",
):
    """Audit selected VORs headless.

    Returns ``{"results": {v_name: {"df", "meta", "tracking",
    "word_bytes"}}, "issues": [...]}``. Owns PDF text sources (closed on exit);
    the caller owns ``pdf_pool`` documents and ``trace_run``.

    ``store`` (``storage_db.Repository``) persists each VOR as a new run with
    position results and the Word artifact; reruns append new runs.
    ``run_id`` reuses one API-created ``runs`` row instead: the payload lands
    in that row via ``save_vor_run_payload`` (same shape as above) and the row
    is finished ``done``. ``pdf_versions`` maps pool keys to stored version
    rows so API payloads keep ``pdf_version_id``/``pdf_sha256`` without
    re-adding versions. ``pdf_sources`` optionally maps pool keys to
    original PDF bytes/paths so stored hashes are stable (``fitz``
    re-serialization is not deterministic). ``llm_base_url`` selects the
    transport (Yandex Responses vs chat gateways); ``llm_provider`` labels
    traces and stored run configs.
    """
    from pdf_text import PDFTextSource
    from vor_parse import load_vor_frame_positions

    audit_position = partial(
        audit_position_hybrid,
        api_key=api_key,
        folder=folder,
        model_name=model_name,
        client_factory=client_factory,
        sleeper=sleeper,
        allowed_hosts=tuple(allowed_llm_hosts or ()),
        base_url=llm_base_url,
    )
    own_trace = trace_run is None
    trace_run = trace_run or TracingRun()
    notify = progress or (lambda fraction, text: None)
    results = {}
    issues = []
    pdf_text_sources = {}
    prepared_documents = {}
    executor = ThreadPoolExecutor(max_workers=max_workers)
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

        # Complete ALL documents before submitting even the first position.
        for name, document in pdf_pool.items():
            notify(0, f"Полная подготовка ПД, OCR и таблиц: {name}")
            source = PDFTextSource(document, full_document=True)
            pdf_text_sources[name] = source
            prepared_documents[name] = source.prepare_full()
        notify(0, "Инициализация многопоточной проверки...")
        current_row_idx = 0

        for v_name, (df_works, positions, meta) in vors_parsed.items():
            from vor_consistency import check_scheme_divisors
            scheme_notes: dict = {}
            for hit in check_scheme_divisors(positions):
                scheme_notes.setdefault(hit.get("position", ""), []).append(hit)
            tasks = []
            results_list = [None] * len(positions)
            row_page_tracking = [None] * len(positions)
            future_to_task = {}
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
                        "position_type": position.position_type,
                        "calculation": position.calculation,
                    },
                    metadata={
                        "vor_file": v_name,
                        "position_index": r_idx,
                        "provider": llm_provider,
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
                corpus_id = ""
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
                            source = pdf_text_sources[target_pdf_name]
                            before_ocr, before_hits, before_tables = (
                                source.ocr_count,
                                source.cache_hits,
                                source.table_cache_hits,
                            )
                            with position_trace.stage(
                                "prepare-ocr-snapshot",
                                input={"pages": primary_phys_pages, "work": w_name},
                            ) as observation:
                                prepared = prepared_documents[target_pdf_name]
                                # Frozen boundary: workers receive only the
                                # restored snapshot, same contract as Celery.
                                text_index = decode_snapshot(encode_snapshot(prepared))
                                corpus_id = prepared.to_snapshot().get("corpus_id", "")
                                new_ocr = source.ocr_count - before_ocr
                                ocr_hits = source.cache_hits - before_hits
                                table_hits = source.table_cache_hits - before_tables
                                observation.update(
                                    output={
                                        "corpus_id": corpus_id,
                                        "new_ocr_pages": new_ocr,
                                        "cache_hits": ocr_hits,
                                        "table_cache_hits": table_hits,
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
                                        f"таблиц из кеша: {table_hits}; "
                                        f"отложено: {len(text_index.pending_ocr)}; "
                                        f"ошибок: {len(text_index.errors)}"
                                    ),
                                    level="ERROR" if text_index.errors else "DEFAULT",
                                )
                    except Exception as exc:
                        text_index = None
                        corpus_id = ""
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
                            "corpus_id": corpus_id,
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
                        "position_type": position.position_type,
                        "calculation": position.calculation,
                        "formula_refs": dict(position.formula_refs or {}),
                        "number": position.number,
                        "file_refs": position.file_ref,
                        "position_source": position_source_dict(position),
                        "claim_specs": build_claim_specs(position),
                        "text_index": text_index,
                        "corpus_id": corpus_id,
                        "target_pdf_name": target_pdf_name,
                        "explicit_refs": list(explicit_refs),
                        "bind_error": bind_error,
                        "prep_error": prep_error,
                        "target_indices": target_indices,
                        "loc_desc": loc_desc,
                        "primary_phys_pages": primary_phys_pages,
                        "vor_positions": list(positions),
                        "scheme_notes": [dict(h) for h in scheme_notes.get(position.number, [])],
                    }
                )
                task = tasks[-1]
                future_to_task[executor.submit(traced_worker_check_row, task, audit_position)] = task

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
                    f"Многопоточная проверка [{current_row_idx}/{total_items}]: {v_name} ({max_workers} потоков)...",
                )

            artifacts = build_run_artifacts(
                df_works, meta, tasks, row_page_tracking, results_list
            )
            df_final = artifacts["df"]
            word_bytes = artifacts["word_bytes"]

            stored_id = None
            if store is not None and run_id is not None:
                store.save_vor_run_payload(
                    run_id,
                    tasks=tasks,
                    tracking=row_page_tracking,
                    results=results_list,
                    word_bytes=bytes(word_bytes),
                    pdf_versions=dict(pdf_versions or {}),
                )
                store.finish_run(run_id, "done")
                stored_id = run_id
            elif store is not None:
                stored_id = store.persist_vor_run(
                    vor_name=v_name,
                    vor_source=loaded_vors[v_name],
                    pdf_pool=pdf_pool,
                    tasks=tasks,
                    tracking=row_page_tracking,
                    results=results_list,
                    word_bytes=word_bytes,
                    model_name=model_name,
                    project_name=project_name,
                    pdf_sources=pdf_sources,
                    provider=llm_provider,
                )

            results[v_name] = {
                "df": df_final,
                "meta": meta,
                "tracking": row_page_tracking,
                "word_bytes": word_bytes,
            }
            if stored_id is not None:
                results[v_name]["run_id"] = stored_id

        return {"results": results, "issues": issues}
    finally:
        executor.shutdown(wait=True)
        for source in list(pdf_text_sources.values()):
            try:
                source.close()
            except Exception:
                pass
        if own_trace:
            trace_run.close()
