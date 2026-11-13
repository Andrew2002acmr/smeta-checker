"""Fail closed when a selected norm's thickness needs an unproved adjustment.

No claim is promoted. Model proposals in the generic evidence trace are always
revalidated against current literal sources, selected measures and operation.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from normative_thickness_rules import (
    source_definition,
    base_thickness,
    adjustment_rule,
    literal_measure,
)

PROPOSAL = "norm_thickness_adjustment_proposal_v1"
TRACE = "mandatory_norm_thickness_adjustment_v1"
TOPIC = "Проверка поправки толщины выбранной нормы"


class AdjustmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_source_id: str = Field(min_length=1, max_length=200)
    adjustment_source_id: str = Field(min_length=1, max_length=200)
    signed_steps: int = Field(strict=True, ge=-1000, le=1000)
    base_quote: str = Field(min_length=1, max_length=900)
    adjustment_quote: str = Field(min_length=1, max_length=2000)


def proposal_events(proposals):
    return [dict(kind=PROPOSAL, **p.model_dump()) for p in proposals]


def _negative_thickness_fact(result, cards):
    """A literal numeric premise only; never infer its material family."""
    from normative_conditional_selection import _readable
    from normative_parameters import measure, quoted, unit_quoted, clean

    card = next(
        (c for c in cards if c["id"] == "thickness" and c["state"] == "confirmed"), None
    )
    proposals = [
        p
        for p in result.get("parameter_proposals", [])
        if p.get("parameter") == "thickness"
    ]
    if not card or len(proposals) != 1:
        return None
    proposal = proposals[0]
    source = next(
        (s for s in result["sources"] if s["id"] == proposal.get("source_id")), None
    )
    value = measure(card.get("found_value"), card.get("found_unit"))
    scope = proposal.get("scope_quote", "")
    verified_header = bool(source and source.get('origin')=='ocr_verified_header'
        and source.get('numeric_verified') is True
        and any(f.get('parameter')=='thickness' and measure(f.get('value'),f.get('unit',''))==value
            and f.get('quote') and quoted(f.get('value'),f['quote'],source.get('text',''))
            and unit_quoted(f.get('unit',''),f['quote']) for f in source.get('verified_parameters',[])))
    if (
        not value
        or value[0] != "length"
        or value[1] <= 0
        or not source
        or source.get("kind") != "project"
        or not (_readable(source) or verified_header)
        or not source.get("version_id")
        or not source.get("page")
        or source["id"] not in card.get("source_ids", [])
        or proposal.get("scope_state") != "verified"
        or proposal.get("operation", "direct") != "direct"
        or measure(proposal.get("value"), proposal.get("unit")) != value
        or not quoted(proposal.get("value"), proposal.get("quote", ""), scope)
        or not unit_quoted(proposal.get("unit", ""), proposal.get("quote", ""))
        or clean(scope) not in clean(source["text"])
    ):
        return None
    return dict(
        source_id=source["id"], value=value, quote=proposal["quote"], scope_quote=scope
    )


def findings(position, result, positions, relation, *, cards=None):
    from normative_conditional_selection import _project_thickness
    from normative_local_checks import layer_group
    from normative_parameters import (
        build_cards,
        measure,
        clean,
        construction_conflict,
        unit_scale_quoted,
    )
    from vor_material_links import _family
    from normative_lawn_soil_scope import lawn_soil_scope

    if (
        result.get("pipeline_version", 0) < 3
        or result.get("execution_state") == "technical_failure"
        or relation.get("parent")
        or getattr(position, "position_type", "").casefold() == "материал"
    ):
        return []
    if cards is None:
        cards = build_cards(
            position,
            {**result, "local_work_basis": None, "quantity_composition": None},
            positions,
            relation,
        )
    material_fact = _project_thickness(position, result, cards)
    fact = material_fact or _negative_thickness_fact(result, cards)
    if not fact or not any(
        c["id"] == "quantity" and c["state"] == "confirmed" for c in cards
    ):
        return []
    from normative_layer_assembly import single_base_price_application
    price_proof=single_base_price_application(position,result,positions,relation,cards=cards)
    targets = list(
        re.finditer(
            r"\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b", position.work_name, re.I
        )
    )
    if len(targets) != 1:
        return []
    target = measure(targets[0][1], targets[0][2].lower())
    if not target:
        return []
    group = layer_group(position, result.get("related_positions") or [],positions)
    if group or "двухслойн" in position.work_name.casefold():
        if not group or measure(str(group["height"]), "см") != fact["value"]:
            return []
    elif target != fact["value"]:
        return []
    sources = {s["id"]: s for s in result["sources"]}
    cited = {
        sid
        for i in result.get("items", [])
        if "normative:applicability" in i.get("claim_ids", [])
        and i.get("topic") != TOPIC
        for sid in i.get("normative_source_ids", [])
    }
    selected = {}
    for p in result.get("parameter_proposals", []):
        source = sources.get(p.get("source_id"))
        definition = source_definition(source) if source else None
        if (
            p.get("parameter") != "unit_scale"
            or not source
            or not definition
            or source["id"] not in cited
            or not unit_scale_quoted(p["value"], p["unit"], p["quote"], source["text"])
            or literal_measure(source, definition["code"])
            != measure(p["value"], p["unit"])
            or measure("1", p["unit"]) != measure("1", position.unit)
            or not any(
                c["id"] == "unit_scale:" + source["id"] and c["state"] == "confirmed"
                for c in cards
            )
        ):
            continue
        thickness = base_thickness(definition["text"])
        if not thickness:
            continue
        family_resolved = bool(
            material_fact
            and _family(position.work_name)
            and _family(definition["text"]) == _family(position.work_name)
            and not construction_conflict(definition["text"], position.work_name)
        )
        if not family_resolved:
            family_resolved = lawn_soil_scope(position, result, cards, fact, definition)
        if not family_resolved and _negative_thickness_fact(result, cards) is None:
            # An unresolved family cannot turn a weaker material-selection
            # premise into an asserted numeric normative condition. Keep the
            # literal/cards intact; require the same single located source as
            # the ordinary negative-premise route.
            continue
        selected[source["id"]] = (
            source,
            definition,
            thickness,
            p.get("scope_state") == "verified",
            family_resolved,
        )
    if not selected:
        return []
    # Equivalent excerpts of one base are not separate choices; different base
    # codes remain ambiguous, rather than assigning a layer to a guessed norm.
    unique = {
        (d["source_id"], d["family"], d["code"]) for _, d, _, _, _ in selected.values()
    }
    output = []
    for sid, (
        source,
        definition,
        thickness,
        scope_verified,
        family_resolved,
    ) in selected.items():
        # The complete literal proof validates both model-selected adjustments
        # against their own priced courses, preserving one physical PD base.
        if price_proof and any(c['base_source_id']==sid for c in price_proof['courses']):
            continue
        if thickness["value"] == target[1]:
            continue
        reason = (
            "unresolved_material_family"
            if not family_resolved
            else "ambiguous_selected_base"
            if len(unique) > 1
            else "unverified_selected_measure"
            if not scope_verified
            else "missing_or_invalid_adjustment"
        )
        candidates = []
        for other in sources.values():
            if not family_resolved:
                break
            other_def = source_definition(other)
            rule = adjustment_rule(other_def, definition) if other_def else None
            if (
                not rule
                or not other_def
                or literal_measure(other, other_def["code"])
                != literal_measure(source, definition["code"])
            ):
                continue
            steps = (target[1] - thickness["value"]) / rule["step"]
            if (
                steps != steps.to_integral_value()
                or abs(steps) > 1000
                or steps > 0
                and not rule["allow_positive"]
                or steps < 0
                and not rule["allow_negative"]
            ):
                continue
            candidates.append(
                dict(
                    source_id=other["id"],
                    code=other_def["code"],
                    base_quote=thickness["quote"],
                    adjustment_quote=rule["reference_quote"],
                    signed_steps=int(steps),
                    scope_verified=False,
                )
            )
        valid = []
        relevant = 0
        for event in result.get("evidence_trace", []):
            if event.get("kind") != PROPOSAL or event.get("base_source_id") != sid:
                continue
            relevant += 1
            try:
                proposal = AdjustmentProposal.model_validate(
                    {k: v for k, v in event.items() if k != "kind"}
                )
            except ValidationError:
                continue
            if (
                proposal.base_source_id != sid
                or proposal.adjustment_source_id not in cited
            ):
                continue
            other = sources.get(proposal.adjustment_source_id)
            if (
                not other
                or clean(proposal.base_quote) not in clean(source["text"])
                or clean(proposal.adjustment_quote) not in clean(other["text"])
            ):
                continue
            literal_base = base_thickness(proposal.base_quote)
            other_def = source_definition(other)
            rule = (
                adjustment_rule(
                    dict(other_def, text=proposal.adjustment_quote), definition
                )
                if other_def
                else None
            )
            if (
                not literal_base
                or literal_base["value"] != thickness["value"]
                or not rule
                or not any(
                    c["source_id"] == other["id"]
                    and c["signed_steps"] == proposal.signed_steps
                    for c in candidates
                )
            ):
                continue
            valid.append((other["id"], proposal.signed_steps))
        if (
            family_resolved
            and scope_verified
            and len(unique) == 1
            and len(set(valid)) == 1
            and len(valid) == relevant
        ):
            continue
        output.append(
            dict(
                kind=TRACE,
                reason=reason,
                base_source_id=sid,
                base_code=definition["code"],
                project_source_id=fact["source_id"],
                project_quote=fact["quote"],
                project_scope_quote=fact["scope_quote"],
                operation_quote=targets[0][0],
                operation_cm=str(target[1] * 100),
                base_cm=str(thickness["value"] * 100),
                base_quote=thickness["quote"],
                adjustment_candidates=candidates[:4],
            )
        )
    return output


def feedback(position, result, positions, relation):
    from normative_parameter_feedback import selected_measure_scope_feedback
    return [
        selected_measure_scope_feedback(f, result)
        if f['reason'] == 'unverified_selected_measure' else
        "Для выбранной нормы "
        + f["base_code"]
        + " базовая толщина "
        + f["base_cm"]
        + " см, "
        "проверенная толщина операции ВОР "
        + f["operation_cm"]
        + " см. Нужна явно выбранная поправка с буквальной ссылкой на эту базу, "
        "измерителем, шагом и направлением изменения. Укажи в adjustments base_source_id, adjustment_source_id, "
        "signed_steps, base_quote, adjustment_quote и включи обе нормы в нормативные ссылки применимости. "
        "Без доказанного расчёта поправки применимость остаётся needs_facts; это не доказанная ошибка ВОР. "
        + (
            "Материал/область выбранной нормы не установлены однозначно; одних шагов поправки недостаточно. "
            if f["reason"] == "unresolved_material_family"
            else ""
        )
        + "Доступные буквальные кандидаты: "
        + ", ".join(
            c["source_id"] + " (" + c["code"] + "), шаги " + str(c["signed_steps"])
            for c in f["adjustment_candidates"]
        )
        for f in findings(position, result, positions, relation)
    ][:4]


def apply_guard(position, result, positions, relation, *, cards=None):
    from normative_parameters import build_cards

    if cards is None:
        cards = build_cards(
            position,
            {**result, "local_work_basis": None, "quantity_composition": None},
            positions,
            relation,
        )
    found = findings(position, result, positions, relation, cards=cards)
    if not found:
        return False
    preserved = []
    for item in result.get("items", []):
        if "normative:applicability" in item.get("claim_ids", []):
            remaining = [c for c in item["claim_ids"] if c != "normative:applicability"]
            if remaining:
                # Do not erase an independently supported conflict or promote
                # an unknown claim. Ordinary source/numeric guards still apply.
                preserved.append(
                    {
                        **item,
                        "topic": item["topic"] + " · Остальные требования",
                        "claim_ids": remaining,
                    }
                )
                item["claim_ids"] = ["normative:applicability"]
            item["state"] = "needs_facts"
    result["items"] = [i for i in result.get("items", []) if i.get("topic") != TOPIC]
    result["items"].extend(preserved)
    for f in found:
        result["items"].append(
            dict(
                topic=TOPIC,
                state="needs_facts",
                required=True,
                claim_ids=["normative:applicability"],
                project_source_ids=[f["project_source_id"]],
                normative_source_ids=[f["base_source_id"]],
                explanation="Выбранная норма "
                + f["base_code"]
                + " задана для толщины "
                + f["base_cm"]
                + " см, "
                "а операция ВОР — "
                + f["operation_cm"]
                + " см. Применимость не подтверждена без проверенной поправки толщины "
                "и числа её шагов. "
                + (
                    "Материал/область выбранной нормы также не установлены; поправка сама по себе этого не доказывает. "
                    if f["reason"] == "unresolved_material_family"
                    else ""
                )
                + "Это вопрос нормативного основания, а не доказанная ошибка ВОР.",
            )
        )
    result["evidence_trace"] = [
        e for e in result.get("evidence_trace", []) if e.get("kind") != TRACE
    ] + found
    result["state"] = (
        "possible_mismatch"
        if any(i["state"] == "possible_mismatch" for i in result["items"])
        else "needs_facts"
    )
    return True
