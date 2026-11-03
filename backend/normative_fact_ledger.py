"""Shared immutable fact envelopes with explicit scope and literal provenance."""

from copy import deepcopy

from analysis_replay import digest
from normative_scope_graph import scope


def build(position, result, positions, relation):
    from normative_parameters import measure
    from normative_parameter_feedback import literal_parameter_issues
    from normative_parent_basis import _proposal_dependencies
    from workspace_models import NormativeParameterProposal

    owner = positions.get((relation.get("parent") or {}).get("index"), position)
    sources = {s["id"]: s for s in result["sources"]}
    facts = []
    for raw in result.get("parameter_proposals", []):
        proposal = NormativeParameterProposal.model_validate(raw)
        if proposal.parameter not in {"quantity", "thickness"}:
            continue
        source = sources.get(proposal.source_id)
        if source is None:
            continue
        measured = measure(proposal.value, proposal.unit)
        issues = literal_parameter_issues(proposal, source)
        quality = source.get("origin") != "uncertain_table" and (
            not source.get("origin", "").startswith("ocr")
            or source.get("numeric_verified")
        )
        literal = not issues and quality and proposal.scope_state == "verified"
        dependencies = _proposal_dependencies(proposal.model_dump(), sources)
        literal = literal and bool(dependencies) and source.get("kind") == "project"
        cards = [
            c
            for c in result.get("parameter_cards", [])
            if proposal.source_id in c.get("source_ids", [])
        ]
        from normative_parent_basis import _anchored_card
        from normative_workbook_rate import quantity_contract
        input_role=quantity_contract(owner) is not None and proposal.parameter=='quantity' and measured and measured[0]=='area' and any(
            c['id']=='input:area' for c in cards)
        card_id='input:area' if input_role else proposal.parameter

        bound = literal and any(
            c.get("state") == "confirmed"
            and _anchored_card(c, sources)
            and c.get("id") == card_id
            and measure(c.get("found_value"), c.get("found_unit", "")) == measured
            for c in cards
        )
        fact = dict(
            schema=1,
            scope_id=scope(owner),
            owner_position=owner.index if bound else None,
            subject_state="verified" if bound else "unresolved",
            proposed_owner_position=owner.index,
            parameter=proposal.parameter,
            quantity_role=('calculation_input' if input_role else 'output') if proposal.parameter=='quantity' else None,
            dimension=measured[0] if measured else None,
            value=proposal.value,
            unit=proposal.unit,
            source_id=proposal.source_id,
            version_id=source.get("version_id"),
            page=source.get("page"),
            quote=proposal.quote,
            scope_quote=proposal.scope_quote,
            operand_source_ids=list(proposal.operand_source_ids),
            operation=proposal.operation,
            literal_state="verified" if literal else "unresolved",
            comparison_states=[dict(id=c["id"], state=c["state"]) for c in cards],
            issues=issues,
        )
        fact["id"] = digest(fact)
        facts.append(fact)
    return dict(
        schema=1,
        position_index=position.index,
        scope_id=scope(position),
        facts=facts,
        source_hashes={
            sid: digest(s)
            for sid, s in sources.items()
            if any(
                f["source_id"] == sid or sid in f["operand_source_ids"] for f in facts
            )
        },
    )


def store(context, record, position, result, positions, relation):
    ledger = build(position, result, positions, relation)
    if getattr(context.repository, "supports_analysis_replay", False) is True:
        identity = dict(
            schema=1,
            stage="scoped_project_facts",
            project_id=str(record["project_id"]),
            vor_version_id=str(record["vor_version_id"]),
            pdf_versions=record["config"].get("pdf_versions", {}),
            ledger_sha256=digest(ledger),
        )
        context.repository.save_analysis_artifact(
            record["project_id"], identity, ledger
        )
        result.setdefault("coverage", {})["fact_ledger"] = dict(
            key=digest(identity), sha256=digest(ledger), count=len(ledger["facts"])
        )
    return ledger


def shared(context, record, positions, index, group):
    """Supply scoped evidence to the model, never another position's verdict."""
    from normative_parent_basis import _anchored_card, _proposal_dependencies
    from normative_parameters import build_cards, measure
    from normative_service import position_key

    sources, candidates = [], []
    pins = set(record["config"].get("pdf_versions", {}).values())
    for node in group["positions"]:
        donor = node["index"]
        if (
            donor == index
            or node["scope_id"] != group["scope_id"]
            or not group["scope_id"]
        ):
            continue
        rows = context.repository.normative_reviews(record["id"], donor)
        row = next((r for r in rows if r["kind"] == "check"), None)
        if row is None:
            continue
        payload = row["payload"]
        if payload.get("vor_version_id") != str(
            record["vor_version_id"]
        ) or payload.get("position_key") != position_key(positions[donor]):
            continue
        result = payload.get("position_check") or {}
        if result.get("execution_state") != "complete":
            continue
        available = {s["id"]: s for s in result.get("sources", [])}
        rebuilt = {
            c["id"]: c
            for c in build_cards(
                positions[donor], result, positions, payload.get("work_relations") or {}
            )
        }
        for card in result.get("parameter_cards", []):
            if (
                card["id"] not in {"quantity", "thickness"}
                or card["state"] != "confirmed"
            ):
                continue
            if not _anchored_card(card, available):
                continue
            current = rebuilt.get(card["id"]) or {}
            if (
                current.get("state") != "confirmed"
                or not _anchored_card(current, available)
                or set(current["source_ids"]) != set(card["source_ids"])
            ):
                continue
            for proposal in result.get("parameter_proposals", []):
                if proposal.get("source_id") not in card.get("source_ids", []):
                    continue
                if proposal.get("parameter") != card["id"] or measure(
                    proposal.get("value"), proposal.get("unit", "")
                ) != measure(current.get("found_value"), current.get("found_unit", "")):
                    continue
                ids = _proposal_dependencies(proposal, available)
                if not ids or any(
                    available[s].get("version_id") not in pins for s in ids
                ):
                    continue
                mapping = {
                    sid: "shared:" + str(donor) + ":" + digest(available[sid])[:24]
                    for sid in ids
                }
                for sid in ids:
                    source = dict(
                        deepcopy(available[sid]),
                        id=mapping[sid],
                        physical_source_id=available[sid].get(
                            "physical_source_id", sid
                        ),
                    )
                    if not any(s["id"] == source["id"] for s in sources):
                        sources.append(source)
                rewritten = dict(
                    deepcopy(proposal),
                    source_id=mapping[proposal["source_id"]],
                    scope_state="unresolved",
                    operand_source_ids=[
                        mapping[sid] for sid in proposal.get("operand_source_ids", [])
                    ],
                )
                candidates.append(
                    dict(
                        owner_position=donor,
                        scope_id=node["scope_id"],
                        work=node["work"],
                        source_review_id=str(row["id"]),
                        proposal=rewritten,
                        engineer_scope_for_current_position="unresolved",
                    )
                )
                if len(candidates) >= 4:
                    return dict(sources=sources, candidates=candidates, truncated=True)
    return dict(sources=sources, candidates=candidates, truncated=False)


def supply(tools, bundle):
    """Register independently readable saved operands, not donor verdicts.

    Sum dependencies have already been checked arithmetically and pinned by the
    caller. The ordinary sum tool still recomputes them when the model selects
    them. Importing a source never establishes its scope for the current work.
    """
    from normative_parameters import number
    from normative_agent import EvidenceRequest, cell_source_id
    import re

    if not bundle["sources"] and not bundle["candidates"]:
        return
    for source in bundle["sources"]:
        if not any(s["id"] == source["id"] for s in tools.sources):
            tools.sources.append(deepcopy(source))
    registry = {s["id"]: s for s in tools.sources}
    for candidate in bundle["candidates"]:
        proposal = candidate["proposal"]
        operands = []
        for sid in proposal.get("operand_source_ids", []):
            source = registry[sid]
            if source.get("version_id") != tools.version:
                continue
            # Reacquire the actual physical cell from the prepared snapshot.
            # A saved quote cannot manufacture a usable OCR cell/quality flag.
            physical = re.search(
                r"agent:p\d+:cell:[0-9a-f]{24}$", source.get("physical_source_id", sid)
            )
            if physical is None:
                continue
            physical_id = physical[0]
            if physical_id not in tools.cells:
                for table in tools._tables(source["page"]):
                    cell = next(
                        (
                            c
                            for c in table["cells"]
                            if cell_source_id(
                                tools.version,
                                source["page"],
                                table["table_id"],
                                c["cell_id"],
                            )
                            == physical_id
                        ),
                        None,
                    )
                    if cell is not None:
                        tools.execute(
                            EvidenceRequest(
                                tool="read_table",
                                page=source["page"],
                                table_id=table["table_id"],
                                row_ids=[cell["row"]],
                                column_ids=[cell["column"]],
                            )
                        )
                        break
            if physical_id in tools.cells:
                operands.append(physical_id)
        if proposal.get("operation") == "sum" and len(operands) == len(
            proposal["operand_source_ids"]
        ):
            computed = tools.execute(
                EvidenceRequest(
                    tool="sum_cells", operand_ids=operands, unit=proposal["unit"]
                )
            )
            if "error" not in computed and number(computed["value"]) == number(
                proposal["value"]
            ):
                proposal["source_id"] = computed["source_id"]
                proposal["operand_source_ids"] = operands
                equation = (
                    computed["equation"]
                    .rsplit("\n", 1)[-1]
                    .partition(" Ячейки:")[0]
                    .rstrip(".")
                )
                proposal["quote"] = equation[:900]
                proposal["scope_quote"] = equation[:900]
        tools.preferred.extend(
            [proposal["source_id"], *proposal.get("operand_source_ids", [])]
        )
