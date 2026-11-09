"""PDF-free graph evidence tools over explicitly pinned prepared snapshots.

No PDF handles, OCR, sibling harvesting, agent rounds, or verdict memory.
Every tool receives immutable snapshot refs with matching SHA metadata.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from normative_agent import (
    EvidenceRequest,
    EvidenceTools,
    compact,
    model_result,
    model_source_aliases,
    restore_source_ids,
    table_outline,
)


TOOL_STATE_VERSION = 1


def load_prepared_tools(*, blob_store: Any, snapshot_refs: list[dict[str, Any]],
                        version_id: str, filename: str = "Подготовленный ПД",
                        kb: Any = None, facts: dict[str, Any] | None = None,
                        prepared_index: Any = None) -> EvidenceTools:
    """Build tools from one pinned prepared corpus; missing data fails closed."""
    from audit_snapshot import load_snapshot

    if not snapshot_refs or not version_id:
        raise ValueError("Prepared snapshot refs and a pinned version are required")
    refs = [dict(ref) for ref in snapshot_refs]
    index = prepared_index if prepared_index is not None else load_snapshot(blob_store, refs[0])
    tools = EvidenceTools.from_prepared(index, str(version_id), filename=filename)
    tools.snapshot_refs = []
    tools.kb = kb
    tools.facts = dict(facts or {})
    return tools


def attach_supporting_tools(tools: EvidenceTools, *, blob_store: Any,
                            documents: dict[str, dict[str, Any]]) -> None:
    """Attach explicitly pinned supporting PDFs; primary refs stay immutable."""
    from audit_snapshot import load_snapshot
    from copy import copy

    if getattr(tools, "version", None) is None:
        raise ValueError("Primary prepared corpus is required before supporting PDFs")
    additional: dict[str, EvidenceTools] = {}
    for version_id, entry in (documents or {}).items():
        version_id = str(version_id)
        if version_id == tools.version:
            raise ValueError("Supporting PDF duplicates the primary document version")
        index = load_snapshot(blob_store, entry["snapshot"])
        if (
            index.pending_ocr
            or not index.metadata.get("preparation_complete")
            or index.metadata.get("document_version_id") != version_id
        ):
            raise ValueError("Supporting PDF must be fully prepared and pinned")
        child = copy(tools)
        child.index, child.filename, child.version = index, str(entry.get("filename") or "Подготовленный ПД"), version_id
        child.snapshot_refs = []
        child.additional_documents = {}  # type: ignore[attr-defined]
        child.is_supporting_document = True  # type: ignore[attr-defined]
        child.project_search = None
        child.project_search_results = {}
        child.table_search_done = False
        child.coverage = dict(
            state="prepared",
            pages=len(index.texts),
            pending_ocr_pages=[],
            error_pages=[int(page) + 1 for page in index.errors],
            document_version_id=version_id,
        )
        additional[version_id] = child
    tools.additional_documents = additional  # type: ignore[attr-defined]
    if additional:
        tools.coverage["supporting_documents"] = [
            dict(document_version_id=version_id, filename=child.filename, pages=len(child.index.texts))
            for version_id, child in additional.items()
        ]
        tools.coverage["primary_document_version_id"] = tools.version


def tool_bundle(tools: EvidenceTools) -> dict[str, Any]:
    """Serialize resumable tool state; page/table bytes stay in the blob store."""
    return {
        "version": TOOL_STATE_VERSION,
        "sources": deepcopy(tools.sources),
        "events": deepcopy(getattr(tools, "events", [])),
        "preferred": list(getattr(tools, "preferred", [])),
        "result_archive": deepcopy(getattr(tools, "result_archive", {})),
        "quantity_sums": deepcopy(getattr(tools, "quantity_sums", [])),
        "coverage": deepcopy(getattr(tools, "coverage", {})),
        "table_search_done": bool(getattr(tools, "table_search_done", False)),
        "project_search_results": deepcopy(getattr(tools, "project_search_results", {})),
        "filename": getattr(tools, "filename", "Подготовленный ПД"),
        "version_id": getattr(tools, "version", None),
        "supporting_versions": sorted(getattr(tools, "additional_documents", {}).keys()),
    }


def restore_tool_bundle(*, blob_store: Any, bundle: dict[str, Any],
                        snapshot_refs: list[dict[str, Any]],
                        supporting: dict[str, dict[str, Any]] | None = None,
                        kb: Any = None, facts: dict[str, Any] | None = None,
                        prepared_index: Any = None) -> EvidenceTools:
    """Restore tools from an explicit bundle and the same pinned corpora."""
    if bundle.get("version") != TOOL_STATE_VERSION:
        raise ValueError("Unsupported evidence tool bundle")
    tools = load_prepared_tools(
        blob_store=blob_store,
        snapshot_refs=snapshot_refs,
        version_id=str(bundle.get("version_id") or ""),
        filename=str(bundle.get("filename") or "Подготовленный ПД"),
        kb=kb,
        facts=facts,
        prepared_index=prepared_index,
    )
    attach_supporting_tools(tools, blob_store=blob_store, documents=supporting or {})
    expected = sorted((supporting or {}).keys())
    if sorted(getattr(tools, "additional_documents", {}).keys()) != expected:
        raise ValueError("Supporting prepared corpora do not match the saved bundle")
    if bundle.get("supporting_versions") != expected:
        raise ValueError("Supporting prepared corpora do not match the saved bundle")
    tools.sources = deepcopy(bundle.get("sources", []))
    tools.events = deepcopy(bundle.get("events", []))
    tools.preferred = list(bundle.get("preferred", []))
    tools.result_archive = deepcopy(bundle.get("result_archive", {}))
    tools.quantity_sums = deepcopy(bundle.get("quantity_sums", []))
    tools.coverage = deepcopy(bundle.get("coverage", {}))
    tools.table_search_done = bool(bundle.get("table_search_done", False))
    tools.project_search_results = deepcopy(bundle.get("project_search_results", {}))
    return tools


def _pickup_symmetric_columns(candidates: list[dict[str, Any]], tools: Any, unit: str) -> list[dict[str, Any]]:
    """Add headerless symmetric columns missed by lexical discovery.

    Wide schedules lose material headers over far-right columns, so those
    cells never match the query. For each total-row candidate, scan the
    same table row for numeric cells with the same resolved unit and a
    compatible measure tail; add them as direct candidates for merging.
    """
    from normative_parameters import number

    try:
        from pdf_cell_ocr import column_unit
    except Exception:
        return candidates
    index = getattr(tools, "index", None)
    tables_by_page = list(getattr(index, "tables", None) or [])
    if not tables_by_page:
        return candidates
    present = {(c.get("page"), c.get("table_id"), c.get("row"), col)
               for c in candidates for col in (c.get("columns") or [])}
    extra: list[dict[str, Any]] = []
    seeds = [c for c in candidates if (c.get("ranking_score") or 0) >= 5] or candidates[:1]
    for candidate in seeds:
        page = candidate.get("page")
        if not isinstance(page, int) or page < 1 or page > len(tables_by_page):
            continue
        seed_headers = {h for h in (str(x).casefold() for x in candidate.get("headers") or []) if h.strip()}
        seed_tail = tuple(
            h for h in (str(x).casefold() for x in candidate.get("headers") or []) if h.strip())[-1:]
        for table in tables_by_page[page - 1] or []:
            if not isinstance(table, dict) or table.get("table_id") != candidate.get("table_id"):
                continue
            row = candidate.get("row")
            for cell in table.get("cells", []):
                if int(cell.get("row", -1)) != row or (cell.get("role") or "") == "header":
                    continue
                if (page, table.get("table_id"), row, cell.get("column")) in present:
                    continue
                text = " ".join(str(cell.get("text") or "").split())
                if not text or number(text) is None:
                    continue
                try:
                    resolved = str(column_unit(table, cell) or "")
                except Exception:
                    continue
                if unit and resolved != unit:
                    continue
                by_id = {e.get("cell_id"): e for e in table.get("cells", [])}
                cell_headers = []
                for hid in cell.get("header_ids") or []:
                    header = by_id.get(hid)
                    if header:
                        header_text = " ".join(str(header.get("text") or "").split())
                        if header_text:
                            cell_headers.append(header_text)
                cell_tail = tuple(h.casefold() for h in cell_headers)[-1:]
                if seed_tail and cell_tail != seed_tail:
                    continue
                # Same measure is not enough: require material overlap with
                # the seed, unless the cell lost its material branch (then
                # the orphan merge re-checks thickness compatibility).
                # Measure/axis words match every column and must not count.
                _STOP = {"площадь,", "площадь", "объем,", "объем", "основание", "покрытие",
                         "слева", "справа", "ровик уширения / срезка обочины"}
                seed_material = {h for h in seed_headers - _STOP}
                cell_material = {h.casefold() for h in cell_headers} - _STOP
                if len(cell_headers) >= 3 and not (seed_material & cell_material):
                    continue
                path = cell_headers
                extra.append({
                    "page": page, "table_id": str(table.get("table_id") or ""),
                    "row": int(cell.get("row", -1)), "column": int(cell.get("column", -1)),
                    "columns": [int(cell.get("column", -1))],
                    "cell_ids": [str(cell.get("cell_id") or "")],
                    "operation": "direct", "values": [text], "calculated_sum": str(number(text)),
                    "headers": path, "unit": resolved,
                    "numeric_verified": bool(cell.get("numeric_verified")),
                    "unit_verified": bool(resolved),
                    "operand_source_ids": [],
                    "discovery_only": True, "scope_verified": False,
                })
                present.add((page, table.get("table_id"), row, cell.get("column")))
    if not extra:
        return candidates
    # Open the picked cells so they carry real source handles like others.
    try:
        from normative_agent import EvidenceRequest as _EvidenceRequest

        for item in extra:
            opened = tools.execute(_EvidenceRequest(
                tool="read_table", page=item["page"], table_id=item["table_id"],
                row_ids=[item["row"]], column_ids=item["columns"]))
            if isinstance(opened, dict):
                item["operand_source_ids"] = [
                    row.get("source_id") for row in opened.get("rows", [])
                    if row.get("source_id") and row.get("cell_id") in item["cell_ids"]]
    except Exception:
        pass
    return [*candidates, *[item for item in extra if item.get("operand_source_ids")]]


def _merge_symmetric_axes(candidates: list[dict[str, Any]], tools: Any, unit: str) -> list[dict[str, Any]]:
    """Merge direct candidates of one material/measure/row into a sum candidate.

    Wide schedules split left/right axes into separate header branches, so
    ``grouped_totals`` returns them as separate direct candidates. When two
    direct candidates share page, row, material headers, and unit, offer an
    additional sum candidate with a precomputed equation — the model then
    sees the total instead of guessing whether to add the parts.
    """
    from decimal import Decimal

    from normative_parameters import number


    def _group_key(item: dict[str, Any]) -> tuple:
        # Header branches differ by axis (left/right) and truncated upper
        # levels; the trailing material branch (material + thickness +
        # measure, in header order) is the stable identity for symmetric
        # totals of one row.
        headers = tuple(h for h in (str(x).casefold() for x in item.get("headers") or []) if h.strip())
        return (item.get("page"), item.get("row"), item.get("table_id"), headers[-3:])

    def _measure_tail(item: dict[str, Any]) -> tuple:
        # Fallback identity for columns whose material headers were lost in
        # extraction (only the measure line survives): page/row/table plus
        # the trailing measure header. Thickness is checked separately.
        headers = tuple(h for h in (str(x).casefold() for x in item.get("headers") or []) if h.strip())
        return (item.get("page"), item.get("row"), item.get("table_id"), headers[-1:])

    def _thickness(item: dict[str, Any]) -> str:
        import re as _re

        for header in item.get("headers") or []:
            match = _re.search(r"h\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|cm|м|m)\b", str(header), _re.I)
            if match:
                value = match.group(1).replace(",", ".")
                return value if match.group(2).lower() in {"см", "cm"} else str(float(value) * 100)
        return ""

    groups: dict[tuple, list[dict[str, Any]]] = {}
    orphans: list[dict[str, Any]] = []
    for item in candidates:
        if item.get("operation") != "direct" or not item.get("numeric_verified"):
            continue
        headers = [h for h in (str(x).casefold() for x in item.get("headers") or []) if h.strip()]
        if len(headers) < 3:
            orphans.append(item)
            continue
        groups.setdefault(_group_key(item), []).append(item)
    # Attach headerless columns (lost material branch) to the group with the
    # same measure tail and no conflicting thickness.
    for orphan in orphans:
        tail = _measure_tail(orphan)
        own_thickness = _thickness(orphan)
        for key, members in groups.items():
            if key[:3] != tail[:3] or not members:
                continue
            member_tail = _measure_tail(members[0])
            if member_tail[3] != tail[3]:
                continue
            member_thickness = _thickness(members[0])
            if own_thickness and member_thickness and own_thickness != member_thickness:
                continue
            # Same measure, same row/table, compatible thickness: one axis pair.
            if len(members) == 1 and orphan not in members:
                members.append(orphan)
            break
    merged = list(candidates)
    for _grouped_key, members in groups.items():
        if len(members) < 2:
            continue
        values = [number(str(v).replace(",", ".")) for m in members for v in (m.get("values") or [])]
        if any(v is None for v in values) or not values:
            continue
        total = sum((v for v in values if v is not None), Decimal(0))
        first = members[0]
        columns: list[int] = []
        cell_ids: list[str] = []
        operands: list[str] = []
        texts: list[str] = []
        for member in members:
            columns.extend(c for c in member.get("columns") or [] if c not in columns)
            cell_ids.extend(c for c in member.get("cell_ids") or [] if c not in cell_ids)
            operands.extend(s for s in member.get("operand_source_ids") or [] if s not in operands)
            texts.extend(v for v in member.get("values") or [])
        equation = " + ".join(texts) + f" = {total} {unit}."
        # A sum candidate needs a real calculated_from_cells source: call
        # sum_cells so the model can cite its source_id directly instead
        # of guessing. Without it the proposal fails operand validation.
        sum_source_id = ""
        try:
            from normative_agent import EvidenceRequest as _EvidenceRequest

            computed = tools.execute(_EvidenceRequest(
                tool="sum_cells", operand_ids=list(operands), unit=unit))
            if isinstance(computed, dict) and "error" not in computed:
                sum_source_id = str(computed.get("source_id") or "")
        except Exception:
            sum_source_id = ""
        if not sum_source_id:
            continue
        merged.append({
            **{k: v for k, v in first.items()
               if k not in {"columns", "cell_ids", "operand_source_ids", "values",
                            "calculated_sum", "equation", "operation", "source_id"}},
            "operation": "sum",
            "columns": columns,
            "cell_ids": cell_ids,
            "operand_source_ids": operands,
            "values": texts,
            "calculated_sum": str(total),
            "equation": equation,
            "source_id": sum_source_id,
            "unit": unit,
            "discovery_only": True,
            "scope_verified": False,
        })
    return merged


def prepared_quantity_candidates(tools: Any, position: Any, run_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Discovery-only numeric candidates via the shared grouped-totals builder.

    Thin graph wrapper over ``EvidenceTools.quantity_candidates``: same
    material/thickness/unit matching, same precomputed sums, no VOR amount
    in ranking. Candidates are not facts; the model must open them with
    ``read_table`` and validators re-check every binding.
    """
    method = getattr(tools, "quantity_candidates", None)
    if method is None:
        return []
    if isinstance(position, dict):
        work = str(position.get("work_name", ""))
        unit = str(position.get("unit", ""))
        index = int(position.get("index", -1))
        position_type = str(position.get("position_type", ""))
        source = position.get("position_source") or {}
    else:
        work = str(getattr(position, "work_name", ""))
        unit = str(getattr(position, "unit", ""))
        index = int(getattr(position, "index", -1))
        position_type = str(getattr(position, "position_type", ""))
        raw_source = getattr(position, "position_source", None)
        if raw_source is not None and hasattr(raw_source, "model_dump"):
            source = raw_source.model_dump(mode="json")
        elif isinstance(raw_source, dict):
            source = raw_source
        else:
            source = {}
    sections = source.get("sections") if isinstance(source, dict) else None
    section_texts = [
        str(section.get("text", "")) for section in sections or []
        if isinstance(section, dict) and section.get("text")
    ]
    # A section shared by dozens of positions (e.g. the whole road chapter)
    # is not a location signal; passing it promotes unrelated schedules
    # whose captions mention the same street word.
    section_context = ""
    if section_texts:
        try:
            inputs = list((run_config or {}).get("position_inputs") or [])
            joined = " | ".join(section_texts)
            shared = sum(
                1 for item in inputs
                if " | ".join(
                    str(section.get("text", ""))
                    for section in ((item.get("position_source") or {}).get("sections") or [])
                    if isinstance(section, dict) and section.get("text")
                ) == joined
            )
            if shared <= 12:
                section_context = " ".join(section_texts)
        except Exception:
            section_context = ""
    query_parts = [work]
    height_cm: str | None = None
    inputs: list[dict[str, Any]] = []
    try:
        from vor_material_links import material_links

        inputs = list((run_config or {}).get("position_inputs") or [])
        tasks = []
        for number, item in enumerate(inputs):
            entry = dict(item) if isinstance(item, dict) else {}
            entry.setdefault("index", number)
            tasks.append(entry)
        links = material_links([{"idx": int(entry.get("index", number)),
                                 "number": str(entry.get("number", "")),
                                 "w_name": str(entry.get("work_name", "")),
                                 "w_vol": str(entry.get("volume", "")),
                                 "w_unit": str(entry.get("unit", "")),
                                 "position_type": str(entry.get("position_type", "")),
                                 "calculation": entry.get("calculation") or {},
                                 "position_source": entry.get("position_source")}
                                for number, entry in enumerate(tasks)])
        parent = links.get(index) or {}
        parent_index = parent.get("index")
        if parent_index is not None and parent_index != index:
            owner = next((entry for entry in tasks if int(entry.get("index", -1)) == int(parent_index)), None)
            if owner is not None:
                query_parts.insert(0, str(owner.get("work_name", "")))
                unit = str(owner.get("unit", "")) or unit
    except Exception:
        pass
    if not isinstance(position, dict):
        try:
            from normative_workbook_rate import quantity_contract

            contract = quantity_contract(position)
            if contract is not None:
                # A derived row (emulsion tonnes from area x rate) has no
                # tonne column in PD: retrieve candidates in the input area
                # unit so the area base can be found and verified.
                unit = str((contract.get("project_input") or {}).get("unit", "")) or unit
        except Exception:
            pass
    if section_texts and inputs:
        try:
            joined = " | ".join(section_texts)
            siblings = [
                entry for entry in inputs
                if int(entry.get("index", -1)) != index
                and " | ".join(
                    str(section.get("text", ""))
                    for section in ((entry.get("position_source") or {}).get("sections") or [])
                    if isinstance(section, dict) and section.get("text")
                ) == joined
            ]
            # A derived row ("priming on the middle layer") names the layer
            # but not its material; nearby material rows of the same section
            # do. Following rows first (work before its material), then
            # preceding rows (derived rows after their layer). Retrieval-only
            # context, never a verdict.
            materials = [
                entry for entry in siblings
                if str(entry.get("position_type", "")).casefold() == "материал"
            ]
            following_materials = sorted(
                (entry for entry in materials if int(entry.get("index", -1)) > index),
                key=lambda entry: int(entry.get("index", 0)),
            )
            preceding_materials = sorted(
                (entry for entry in materials if int(entry.get("index", -1)) < index),
                key=lambda entry: int(entry.get("index", 0)), reverse=True,
            )
            # Nearest first regardless of direction: a derived row after its
            # layer must pick the adjacent material, not a distant same-name
            # row from another section block.
            nearby_materials = sorted(
                following_materials + preceding_materials,
                key=lambda entry: abs(int(entry.get("index", 0)) - index),
            )
            for sibling in nearby_materials:
                name = str(sibling.get("work_name", ""))
                lowered = name.casefold()
                # "Щебеночно-мастичный асфальтобетон" (ЩМА) is asphalt, not the
                # crushed-stone-sand aggregate (ЩПС); it must not steer retrieval.
                if "мастичн" in lowered or "асфальт" in lowered:
                    continue
                if "щпс" in lowered or "щебеночно" in lowered:
                    query_parts.append(name)
                    break
            # A work row ("middle layer") names no aggregate fraction while
            # the schedule splits it (0/31,5 vs 0/63). The material rows right
            # after the work name it; take the nearest following material.
            import re as _re

            if position_type.casefold() == "работа":
                for sibling in following_materials[:2]:
                    fraction = _re.search(r"\b0/\d+(?:[.,]\d+)?\b", str(sibling.get("work_name", "")))
                    if fraction:
                        query_parts.append(fraction.group(0))
                        break
        except Exception:
            pass
    # VOR work titles carry the operational layer thickness (12cm), while
    # quantity schedules carry the full construction thickness (24cm).
    # Filtering works by operational height drops the right schedule.
    if position_type.casefold() != "работа":
        try:
            import re

            match = re.search(r"h\s*=\s*(\d+(?:[.,]\d+)?)\s*(см|cm|м|m)\b", work, re.I)
            if match:
                value = match.group(1).replace(",", ".")
                height_cm = value if match.group(2).lower() in {"см", "cm"} else str(float(value) * 100)
        except Exception:
            height_cm = None
    query = " ".join(part for part in query_parts if part)[:600]
    try:
        result = method(query, unit=unit, height_cm=height_cm, section_context=section_context, limit=6)
    except Exception:
        return []
    picked = _pickup_symmetric_columns([dict(item) for item in result or []], tools, unit)
    found = _merge_symmetric_axes(picked, tools, unit)
    # Prefer pages already surfaced by lexical discovery, in search rank
    # order: a schedule found by the work title outranks a same-material
    # schedule elsewhere (e.g. street vs exits). Stable sort keeps
    # ranking_score order within ties.
    rank: dict[int, int] = {}
    for event in getattr(tools, "events", None) or []:
        if event.get("kind") != "project_retrieval_page_v1":
            continue
        for page in event.get("returned_pages") or []:
            if isinstance(page, int) and page not in rank:
                rank[page] = len(rank)

    def _rank(item: dict[str, Any]) -> int:
        page = item.get("page")
        return rank.get(page, 10_000) if isinstance(page, int) else 10_000

    found.sort(key=_rank)
    if unit.casefold() not in {"т", "t"}:
        return found
    # A mass row derives from an area schedule (priming/emulsion): no
    # schedule carries tonnes, so area candidates are the quantity basis.
    # The mass itself stays unresolved without density. Area basis first.
    try:
        fallback = method(query, unit="м2", height_cm=height_cm, section_context=section_context, limit=6)
    except Exception:
        return found
    area = _merge_symmetric_axes(
        _pickup_symmetric_columns([dict(item) for item in fallback or []], tools, "м2"), tools, "м2")
    seen = {(item.get("page"), item.get("row"), tuple(item.get("columns") or [])) for item in area}
    for item in found:
        key = (item.get("page"), item.get("row"), tuple(item.get("columns") or []))
        if key not in seen:
            seen.add(key)
            area.append(item)
    # Precomputed sums first: they carry the total the model needs, while
    # bare directs stay available below the cap.
    area.sort(key=lambda item: 0 if item.get("operation") == "sum" else 1)
    return area[:8]


__all__ = [
    "TOOL_STATE_VERSION",
    "EvidenceRequest",
    "EvidenceTools",
    "attach_supporting_tools",
    "compact",
    "load_prepared_tools",
    "model_result",
    "model_source_aliases",
    "prepared_quantity_candidates",
    "restore_source_ids",
    "restore_tool_bundle",
    "table_outline",
    "tool_bundle",
]
