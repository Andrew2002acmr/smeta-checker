"""Versioned navigation graph; no edge is an engineering quantity proof."""

from collections import defaultdict
import re

from analysis_replay import digest
from audit_models import position_source_dict

VERSION = 1


def scope(position):
    source = position_source_dict(position) or {}
    sections = source.get("sections") or []
    required = ("document_sha256", "sheet", "header_row")
    if not all(source.get(k) for k in required) or not sections:
        return None
    if not all(s.get("address") and s.get("text") for s in sections):
        return None
    return digest({k: source[k] for k in required} | {"sections": sections})


def signature(position):
    return digest(
        dict(
            work=re.sub(r"\s+", " ", position.work_name.casefold()).strip(),
            unit=position.unit,
            kind=position.position_type,
        )
    )


def build(positions):
    from vor_material_ownership import material_owners
    from vor_material_links import material_links
    from normative_layer_assembly import layer_assembly
    from normative_workbook_rate import related_area

    tasks = [p.legacy_task() for p in positions.values()]
    nodes, edges = {}, []
    patterns = defaultdict(list)
    for index, position in sorted(positions.items()):
        source = position_source_dict(position) or {}
        nodes[str(index)] = dict(
            index=index,
            scope_id=scope(position),
            row=source.get("row"),
            sections=source.get("sections", []),
            work=position.work_name,
            kind=position.position_type,
            unit=position.unit,
        )
        patterns[signature(position)].append(index)
    owners, links = material_owners(tasks), material_links(tasks)
    for index, owner in sorted(owners.items()):
        edges.append(
            dict(
                source=owner["index"],
                target=index,
                kind="calculation_candidate" if index in links else "structural_owner",
                state="requires_agent_binding",
                quantity_proven=False,
            )
        )
    pairs = set()
    for index, position in sorted(positions.items()):
        assembly = layer_assembly(position, positions)
        if assembly:
            pair = tuple(assembly["work_indices"])
            if pair not in pairs:
                pairs.add(pair)
                edges.append(
                    dict(
                        source=pair[0],
                        target=pair[1],
                        kind="technological_layers",
                        thickness_cm=str(assembly["height"]),
                        state="declared_workbook_relation",
                        quantity_proven=False,
                    )
                )
        for area in related_area(position, positions):
            edges.append(
                dict(
                    source=area["index"],
                    target=index,
                    kind="area_input_candidate",
                    state="requires_agent_binding",
                    quantity_proven=False,
                )
            )
    return dict(
        schema=VERSION,
        nodes=nodes,
        edges=edges,
        repeated_patterns=[group for group in patterns.values() if len(group) > 1],
    )


def for_agent(context, record, positions, index):
    """Read one group, while keeping cross-site analogies explicitly separate."""
    graph = build(positions)
    own_scope = graph["nodes"][str(index)]["scope_id"]
    selected = {index}
    # Two hops cover work/material siblings and layer-related works. Scope
    # identity is mandatory even for a structurally plausible neighbouring row.
    for _ in range(2):
        frontier = set(selected)
        for edge in graph["edges"]:
            if edge["source"] in frontier or edge["target"] in frontier:
                selected.update(
                    i
                    for i in (edge["source"], edge["target"])
                    if own_scope and graph["nodes"][str(i)]["scope_id"] == own_scope
                )
    ordered = sorted(selected)
    limited = set(ordered[:12]) | {index}
    result = dict(
        schema=VERSION,
        scope_id=own_scope,
        positions=[
            dict(
                graph["nodes"][str(i)],
                quantity=positions[i].volume,
                formula=positions[i].formula[:800],
                comment=positions[i].comment[:700],
            )
            for i in sorted(limited)
        ],
        edges=[e for e in graph["edges"] if {e["source"], e["target"]} <= limited],
        omitted_indices=sorted(selected - limited),
        same_work_other_scopes=[
            graph["nodes"][str(i)]
            for group in graph["repeated_patterns"]
            if index in group
            for i in group
            if i != index and graph["nodes"][str(i)]["scope_id"] != own_scope
        ][:12],
        policy="Workbook edges are navigation. Establish each operand and project scope from sources; never transfer geometry between sites.",
    )
    if getattr(context.repository, "supports_analysis_replay", False) is True:
        identity = dict(
            schema=VERSION,
            stage="work_scope_graph",
            project_id=str(record["project_id"]),
            vor_version_id=str(record["vor_version_id"]),
            graph_sha256=digest(graph),
        )
        context.repository.save_analysis_artifact(record["project_id"], identity, graph)
        result["artifact_key"] = digest(identity)
    return result
