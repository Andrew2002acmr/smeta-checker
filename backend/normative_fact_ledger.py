"""Shared immutable fact envelopes with explicit scope and literal provenance."""

from copy import deepcopy
from typing import Any

from analysis_replay import digest
from normative_scope_graph import scope


def build(position, result, positions, relation) -> dict[str, Any]:
    """Keep project observations independent of their comparison with the VOR.

    A saved green card is not proof of subject ownership. Rebuild the ordinary
    cards, including their source/scope guards, and require an anchored matching
    observation. A proved project quantity remains proved when that comparison
    reports a mismatch. Missing scope never invalidates a literal reading.
    """
    from normative_parameters import build_cards, measure
    from normative_parameter_feedback import literal_parameter_issues
    from normative_parent_basis import _anchored_card, _proposal_dependencies
    from normative_workbook_rate import quantity_contract
    from normative_source_identity import canonical_source_id
    from workspace_models import NormativeParameterProposal

    owner = positions.get((relation.get("parent") or {}).get("index"), position)
    sources = {s["id"]: s for s in result["sources"]}
    from normative_fact_contract import restore_numeric_items
    literal_result = deepcopy(result)
    restore_numeric_items(literal_result)
    rebuilt = {c["id"]: c for c in build_cards(position, literal_result, positions, relation)}
    facts, calculations = [], []
    for raw in result.get("parameter_proposals", []):
        proposal = NormativeParameterProposal.model_validate(raw)
        if proposal.parameter not in {"quantity", "thickness"}:
            continue
        source = sources.get(proposal.source_id)
        dependencies = []
        if source is not None:
            try:
                dependencies = _proposal_dependencies(proposal.model_dump(), sources)
                if any(canonical_source_id(sources[sid]) is None for sid in dependencies):
                    dependencies = []
            except (KeyError, TypeError, ValueError):
                # A missing operand is an open calculation, never a literal sum.
                pass
        calculation_state = (
            "verified" if dependencies else "unresolved"
        ) if proposal.operation == "sum" else "not_applicable"
        if proposal.operation == "sum":
            calculations.append(dict(
                id=digest(dict(parameter=proposal.parameter, source_id=proposal.source_id,
                    operand_source_ids=list(proposal.operand_source_ids),
                    value=proposal.value, unit=proposal.unit, operation=proposal.operation)),
                parameter=proposal.parameter, source_id=proposal.source_id,
                operand_source_ids=list(proposal.operand_source_ids),
                calculation_state=calculation_state,
                reason=("Сумма повторно проверена по исходным величинам и единицам."
                    if dependencies else "Не все исходные величины, единицы или ссылки подтверждены."),
            ))
        if source is None:
            continue
        measured = measure(proposal.value, proposal.unit)
        # Scope quotation is a separate assertion. Its absence, dispute or
        # malformed citation must not erase the actual number and unit.
        literal_proposal = proposal.model_copy(update={"scope_state": "unresolved", "scope_quote": ""})
        issues = literal_parameter_issues(literal_proposal, source)
        binding_issues = literal_parameter_issues(proposal, source)
        quality = source.get("origin") != "uncertain_table" and (
            not source.get("origin", "").startswith("ocr")
            or source.get("numeric_verified")
        )
        literal = not issues and quality and bool(measured) and bool(dependencies) and source.get("kind") == "project"
        cards = [c for c in result.get("parameter_cards", [])
            if proposal.source_id in c.get("source_ids", [])]
        input_role = bool(quantity_contract(owner) is not None
            and proposal.parameter == "quantity" and measured and measured[0] == "area"
            and any(c["id"] == "input:area" for c in cards))
        card_id = "input:area" if input_role else proposal.parameter
        current = rebuilt.get(card_id) or {}
        bound = bool(literal and not binding_issues and proposal.scope_state == "verified"
            and current.get("state") in {"confirmed", "possible_mismatch"}
            and _anchored_card(current, sources)
            and measure(current.get("found_value"), current.get("found_unit", "")) == measured
            and any(c.get("state") in {"confirmed", "possible_mismatch"}
                and _anchored_card(c, sources) and c.get("id") == card_id
                and measure(c.get("found_value"), c.get("found_unit", "")) == measured
                for c in cards))
        comparison_states = [dict(id=c["id"], state=c["state"]) for c in cards]
        comparison_state = (current.get("state", "needs_facts") if bound else "needs_facts")
        fact = dict(
            schema=2, scope_id=scope(owner), owner_position=owner.index if bound else None,
            subject_state="verified" if bound else "unresolved",
            subject_binding_state="verified" if bound else "unresolved",
            proposed_owner_position=owner.index, parameter=proposal.parameter,
            quantity_role=("calculation_input" if input_role else "output") if proposal.parameter == "quantity" else None,
            dimension=measured[0] if measured else None, value=proposal.value, unit=proposal.unit,
            source_id=proposal.source_id, version_id=source.get("version_id"), page=source.get("page"),
            quote=proposal.quote, scope_quote=proposal.scope_quote,
            operand_source_ids=list(proposal.operand_source_ids), operation=proposal.operation,
            literal_state="verified" if literal else "unresolved",
            calculation_state=calculation_state, comparison_state=comparison_state,
            comparison_states=comparison_states, issues=issues, binding_issues=binding_issues,
        )
        # The project observation's identity excludes the declared VOR quantity
        # and all mutable validation/comparison states. It survives a mismatch.
        fact["id"] = digest(dict(schema=2, parameter=proposal.parameter,
            source_id=canonical_source_id(source) or proposal.source_id,
            version_id=source.get("version_id"), value=proposal.value, unit=proposal.unit,
            operation=proposal.operation,
            operand_source_ids=[canonical_source_id(sources.get(sid)) or sid
                for sid in proposal.operand_source_ids]))
        facts.append(fact)
    return dict(
        schema=2, position_index=position.index, scope_id=scope(position), facts=facts,
        calculations=calculations,
        source_hashes={sid: digest(s) for sid, s in sources.items()
            if any(f["source_id"] == sid or sid in f["operand_source_ids"] for f in facts)
            or any(c["source_id"] == sid or sid in c["operand_source_ids"] for c in calculations)},
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
    # Explicit prior runs may supply only rechecked project literals. Their
    # normative edition and verdicts never become evidence for this run.
    records = [record]
    prior = record['config'].get('normative_fact_source_run_ids', [])
    if not isinstance(prior, list) or len(prior) > 3:
        raise ValueError('Prior fact runs must be an explicit bounded list')
    from uuid import UUID
    if prior and (not isinstance(record['config'].get('position_inputs'),list)
            or not record['config']['position_inputs']):
        raise ValueError('Prior fact runs require the complete position input bank')
    for run_id in dict.fromkeys(prior):
        if str(run_id) == str(record['id']):
            continue
        previous = context.repository.load_run(UUID(str(run_id)))
        if (str(previous['project_id']) != str(record['project_id'])
                or str(previous['vor_version_id']) != str(record['vor_version_id'])
                or previous['config'].get('pdf_versions') != record['config'].get('pdf_versions')
                or not isinstance(previous['config'].get('position_inputs'),list)
                or not previous['config']['position_inputs']
                or digest(previous['config']['position_inputs']) != digest(record['config']['position_inputs'])):
            raise ValueError('Prior fact run project/VOR/PDF inputs differ')
        records.append(previous)
    requests = ([(node,record) for node in group['positions'] if node['index']!=index]
        + [(node,source_record) for source_record in records[1:]
            for node in group['positions'] if node['index']==index]
        + [(node,source_record) for source_record in records[1:]
            for node in group['positions'] if node['index']!=index])
    for node, source_record in requests:
        donor = node["index"]
        if (
            node["scope_id"] != group["scope_id"]
            or not group["scope_id"]
        ):
            continue
        rows = context.repository.normative_reviews(source_record["id"], donor)
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
        from normative_fact_contract import restore_numeric_items
        literal_result = deepcopy(result)
        restore_numeric_items(literal_result)
        rebuilt = {
            c["id"]: c
            for c in build_cards(
                positions[donor], literal_result, positions, payload.get("work_relations") or {}
            )
        }
        for card in result.get("parameter_cards", []):
            if (
                card["id"] not in {"quantity", "thickness"}
                or card["state"] not in {"confirmed", "possible_mismatch"}
            ):
                continue
            if not _anchored_card(card, available):
                continue
            current = rebuilt.get(card["id"]) or {}
            if (
                current.get("state") not in {"confirmed", "possible_mismatch"}
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
                from normative_source_identity import shared_source

                aliases = {sid: shared_source(available[sid], donor) for sid in ids}
                if any(s is None for s in aliases.values()):
                    continue
                mapping = {sid: source["id"] for sid, source in aliases.items() if source is not None}
                for source in aliases.values():
                    if source is not None and not any(s["id"] == source["id"] for s in sources):
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
                        source_run_id=str(source_record['id']),
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
    from normative_source_identity import canonical_source_id

    if not bundle["sources"] and not bundle["candidates"]:
        return
    for source in bundle["sources"]:
        if canonical_source_id(source) is not None and not any(s["id"] == source["id"] for s in tools.sources):
            tools.sources.append(deepcopy(source))
    registry = {s["id"]: s for s in tools.sources}
    for candidate in bundle["candidates"]:
        proposal = candidate["proposal"]
        if (proposal["source_id"] not in registry
                or any(sid not in registry for sid in proposal.get("operand_source_ids", []))):
            continue
        operands = []
        for sid in proposal.get("operand_source_ids", []):
            source = registry[sid]
            if source.get("version_id") != tools.version:
                continue
            # Reacquire the actual physical cell from the prepared snapshot.
            # A saved quote cannot manufacture a usable OCR cell/quality flag.
            physical = re.search(
                r"agent:p\d+:cell:[0-9a-f]{24}$", canonical_source_id(source) or ""
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
