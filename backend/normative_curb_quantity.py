"""Bind curb totals by explicit section and product mark, never expected amount.

Only a complete, independently readable physical column is accepted. Other
sections' candidates and the model's uncertainty are retained in the trace.
"""

from copy import deepcopy
import re
from analysis_replay import digest

from normative_parameters import clean, measure
from normative_quantity_anchors import _physical, _proposal

TRACE = "explicit_curb_column_quantity_v1"
TOPIC = "Количество бортового камня по собственному разделу ПД"


def marks(text):
    return {
        ".".join(m.groups())
        for m in re.finditer(
            r"\bбр\s*[-–]?\s*(\d{2,3})[.,](\d{2,3})[.,](\d{1,3})(?!\d)", clean(text)
        )
    }


def section(text, *, header=False):
    text = clean(text)
    kinds = set()
    if re.search(r"останов", text):
        kinds.add("stops")
    if re.search(r"тротуар|лестнич", text):
        kinds.add("sidewalk")
    if re.search(r"съезд", text):
        kinds.add("exits")
    if re.search(r"проезж\w*\s+част\w*.*улиц" if header else r"улиц", text):
        kinds.add("street")
    return next(iter(kinds)) if len(kinds) == 1 else None


def owner_scope(position, relation):
    if (
        relation.get("parent")
        or relation.get("children_truncated")
        or not re.match(
            r"^\s*установка\s+(?:бортов|газонн)\w*\s+камн", position.work_name, re.I
        )
        or measure("1", position.unit) != measure("1", "м")
    ):
        return None
    sections = (
        getattr(position.position_source, "sections", [])
        if position.position_source
        else []
    )
    scope = section(
        " ".join(s.get("text", "") if isinstance(s, dict) else s.text for s in sections)
    )
    # Exit tables require their own complete row-set proof, not a street total.
    if scope not in {"street", "sidewalk", "stops"}:
        return None
    source = position.position_source
    own_sections = [
        s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in sections
    ]
    materials = relation.get("scope_materials", [])
    for material in materials:
        owner = material.get("ownership") or {}
        provenance = owner.get("provenance") or {}
        if (
            owner.get("kind") != "vor_material_ownership"
            or owner.get("state") != "ownership_only"
            or owner.get("index") != position.index
            or provenance.get("work_index") != position.index
            or provenance.get("work_row") != source.row
            or provenance.get("sections") != own_sections
            or provenance.get("document_sha256") != source.document_sha256
            or provenance.get("sheet") != source.sheet
        ):
            return None
    codes = marks(
        position.work_name + " " + " ".join(c.get("work", "") for c in materials)
    )
    return dict(section=scope, mark=next(iter(codes))) if len(codes) == 1 else None


def apply(context, record, index, position, result, kb, relation):
    if not re.match(
        r"^\s*установка\s+(?:бортов|газонн)\w*\s+камн", position.work_name, re.I
    ):
        return False
    from workspace_service import inputs_for_run
    from vor_material_ownership import material_owners

    positions = {p.index: p for p in inputs_for_run(context, record)}
    owners = material_owners([p.legacy_task() for p in positions.values()])
    relation = {
        **relation,
        "scope_materials": [
            dict(index=i, work=positions[i].work_name, ownership=owner)
            for i, owner in owners.items()
            if owner["index"] == position.index
        ],
    }
    if owner_scope(position, relation) is None:
        return False
    from normative_agent import EvidenceTools

    return project(
        position, result, EvidenceTools(context, record, index, [], kb, {}), relation
    )


def project(position, result, tools, relation):
    scope = owner_scope(position, relation)
    if (
        not scope
        or not tools.version
        or result.get("execution_state") == "technical_failure"
        or any(
            c.get("id") == "quantity" and c.get("state") == "confirmed"
            for c in result.get("parameter_cards", [])
        )
    ):
        return False
    from normative_agent import EvidenceRequest

    # Inspect physical header identity before reading values. The VOR amount
    # is deliberately not an input to table/column selection.
    selections = []
    for at, tables in enumerate(tools.index.tables or []):
        for table in tools._tables(at + 1) if tables else []:
            headers = {
                c["cell_id"]: c for c in table["cells"] if c.get("role") == "header"
            }
            totals = [
                c["row"]
                for c in table["cells"]
                if c["column"] == 0
                and re.fullmatch(r"\s*Всего\s*[:.]?\s*", c["text"], re.I)
            ]
            if len(totals) != 1:
                continue
            for cell in table["cells"]:
                if (
                    cell["row"] != totals[0]
                    or cell.get("role") != "cell"
                    or cell.get("row_scope", {}).get("state")
                    not in {None, "total_without_section"}
                ):
                    continue
                chain = [
                    headers[key] for key in cell.get("header_ids", []) if key in headers
                ]
                own = " | ".join(
                    h["text"] for h in chain if h.get("colspan", 1) < table["col_count"]
                )
                if (
                    marks(own) == {scope["mark"]}
                    and section(own, header=True) == scope["section"]
                ):
                    selections.append((at + 1, table, cell))
    if len(selections) != 1:
        return False
    page, table, cell = selections[0]
    tools.execute(
        EvidenceRequest(
            tool="read_table",
            page=page,
            table_id=table["table_id"],
            row_ids=[cell["row"]],
            column_ids=[cell["column"]],
        )
    )
    lookup = {s["id"]: s for s in tools.sources}
    records = [
        r
        for sid in tools.cells
        if (r := _physical(sid, lookup, tools.cells)) is not None
    ]
    if len(records) != 1:
        return False
    physical = records[0]
    if physical["source"]["version_id"] != tools.version or measure(
        "1", physical["unit"]
    ) != measure("1", position.unit):
        return False
    proposal = _proposal(
        physical["source"],
        physical["value"],
        physical["unit"],
        physical["quote"],
        physical["scope_quote"],
    )
    if proposal is None:
        return False
    proposal["scope_state"] = "verified"
    from workspace_models import NormativeParameterProposal

    proposal = NormativeParameterProposal.model_validate(proposal).model_dump()
    preview = deepcopy(result)
    original = [
        p
        for p in preview.get("parameter_proposals", [])
        if p.get("parameter") == "quantity"
    ]
    original_items = []
    items = []
    for item in preview.get("items", []):
        if item.get("topic") == TOPIC:
            original_items.append(deepcopy(item))
            continue
        if item.get("state") == "needs_facts" and "quantity" in item.get(
            "claim_ids", []
        ):
            original_items.append(deepcopy(item))
            residual = [c for c in item["claim_ids"] if c != "quantity"]
            if residual:
                items.append({**item, "claim_ids": residual})
        else:
            items.append(item)
    preview["items"] = items
    sid = physical["source"]["id"]
    from normative_source_addresses import cell_source_handles
    from normative_source_identity import canonical_source_id
    handles = cell_source_handles(tools.version, page, table['table_id'], cell['cell_id'])
    old_sources = [s for s in preview["sources"] if canonical_source_id(s) in handles]
    # Reading a header again may enrich its scope; literal quantity and PDF
    # geometry must stay unchanged for the same canonical cell identity.
    for old in old_sources:
        if any(
            old.get(k) != physical["source"].get(k)
            for k in (
                "version_id",
                "page",
                "rects",
                "origin",
                "numeric_verified",
                "unit",
                "unit_verified",
            )
        ):
            return False
        if (
            old["text"].rsplit("\n", 1)[-1]
            != physical["source"]["text"].rsplit("\n", 1)[-1]
        ):
            return False
    preview["sources"] = [s for s in preview["sources"] if s["id"] != sid] + [
        deepcopy(physical["source"])
    ]
    preview["parameter_proposals"] = [
        p
        for p in preview.get("parameter_proposals", [])
        if p.get("parameter") != "quantity"
    ] + [proposal]
    from normative_parameters import build_cards

    card = next(
        c
        for c in build_cards(position, preview, {position.index: position}, {})
        if c["id"] == "quantity"
    )
    if card["state"] not in {"confirmed", "possible_mismatch"}:
        return False
    new_item = dict(
        topic=TOPIC,
        state=card["state"],
        required=True,
        claim_ids=["quantity"],
        project_source_ids=[sid],
        normative_source_ids=[],
        explanation="ПД: "
        + proposal["value"]
        + " "
        + proposal["unit"]
        + "; ВОР: "
        + position.volume
        + " "
        + position.unit
        + ". "
        + card["reason"]
        + " Источник выбран по разделу и марке камня, без сложения других участков.",
    )
    if (
        original == [proposal]
        and physical["source"] in old_sources
        and len([i for i in result["items"] if i.get("topic") == TOPIC]) == 1
        and new_item in result["items"]
    ):
        return False
    preview["items"].append(new_item)
    preview.setdefault("evidence_trace", []).append(
        dict(
            kind=TRACE,
            scope=scope,
            physical=physical["provenance"],
            original_proposals=original,
            original_items=original_items,
            original_sources=old_sources,
            proposal=proposal,
            physical_trace=deepcopy(tools.events),
            scope_materials=deepcopy(relation.get("scope_materials", [])),
            cell_evidence=deepcopy(tools.cells[sid]),
            cell_sha256=digest(tools.cells[sid]),
            source_sha256=digest(physical["source"]),
        )
    )
    result.update(preview)
    return True


def verified_mismatch(position, result, *, item=None):
    """Recheck a server-generated PD conflict without requiring a work norm."""
    if position is None:
        return False
    events = [e for e in result.get("evidence_trace", []) if e.get("kind") == TRACE]
    if len(events) != 1:
        return False
    event = events[0]
    scope = owner_scope(
        position, dict(scope_materials=event.get("scope_materials", []))
    )
    entry = event.get("cell_evidence")
    proposal = event.get("proposal")
    if (
        not scope
        or scope != event.get("scope")
        or not entry
        or not proposal
        or digest(entry) != event.get("cell_sha256")
        or [
            p
            for p in result.get("parameter_proposals", [])
            if p.get("parameter") == "quantity"
        ]
        != [proposal]
    ):
        return False
    sid = proposal["source_id"]
    if item is not None and (item.get('topic')!=TOPIC or item.get('claim_ids')!=['quantity']
            or item.get('project_source_ids')!=[sid] or item.get('normative_source_ids')):
        return False
    sources = [s for s in result.get("sources", []) if s["id"] == sid]
    if len(sources) != 1 or digest(sources[0]) != event.get("source_sha256"):
        return False
    physical = _physical(sid, {sid: sources[0]}, {sid: entry})
    if (
        physical is None
        or physical["provenance"] != event.get("physical")
        or marks(physical["header"]) != {scope["mark"]}
        or section(physical["header"], header=True) != scope["section"]
        or not re.fullmatch(
            r"\s*Всего\s*:[ \t]*(?:\|[ \t]*)?\n[ \t]*" + re.escape(physical["cell"]["text"]) + r"\s*",
            sources[0]["text"][len(physical["header"]) :],
        )
    ):
        return False
    from normative_parameters import build_cards

    cards = build_cards(position, result, {position.index: position}, {})
    return any(
        c["id"] == "quantity" and c["state"] == "possible_mismatch" for c in cards
    )
