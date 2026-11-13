"""A selected base at 4 cm cannot be green for 5 cm without a proved step."""

from copy import deepcopy

import pytest

from audit_models import VorPosition
from normative_parameters import enrich
from normative_thickness_adjustment import (
    AdjustmentProposal,
    PROPOSAL,
    TRACE,
    findings,
    feedback,
)
from normative_parameter_anchors import thickness_adjustments
from normative_preflight import (
    adjustment_references,
    same_table_adjustments,
    preflight_candidates,
)
from normative_thickness_rules import base_thickness
from test_normative_preflight import Corpus, norm
from test_normative_conditional_selection import actual_lower_layer_case


def asphalt_case():
    position = VorPosition(
        index=74,
        work_name="Устройство верхнего слоя усиления из асфальтобетонной смеси асфальтоукладчиком 3 типоразмера, h = 5 см",
        volume="106",
        unit="м2",
        position_type="Работа",
    )
    scope = "Верхний слой покрытия (слой усиления) — ЩМА-16 Щебёночно-мастичный асфальтобетон по ГОСТ Р 58406.1-2020 на ПБВ 60 по ГОСТ Р 52056-2003, h=5 см"
    sources = [
        dict(
            id="pd",
            kind="project",
            origin="native",
            version_id="pd1",
            page=50,
            text=scope,
        ),
        dict(
            id="cell",
            kind="project",
            origin="native",
            version_id="pd1",
            unit="м2",
            unit_verified=True,
            numeric_verified=True,
            text="Покрытие | ЩМА-16 на ПБВ 60 | h=0,05м | Площадь, м2\nВсего: 106,00",
        ),
        dict(
            id="base",
            kind="normative",
            origin="extracted_native_unreviewed",
            source_id="pdf1",
            label="ГЭСН 27 · 27-06-031-01",
            text="Таблица ГЭСН 27-06-031 Устройство покрытия из горячих асфальтобетонных смесей\nИзмеритель: 1000 м2\n"
            "27-06-031-01 третьего типоразмера, ширина укладки до 6 м, толщина слоя 4 см\nСостав работ:\nУкладка и укатка.",
        ),
        dict(
            id="step",
            kind="normative",
            origin="extracted_native_unreviewed",
            source_id="pdf1",
            label="ГЭСН 27 · 27-06-032-01",
            text="Таблица ГЭСН 27-06-032 Нормы для корректировки таблицы 27-06-031\nИзмеритель: 1000 м2\n"
            "При изменении толщины покрытия на 0,5 см добавлять или исключать:\n27-06-032-01 к норме 27-06-031-01",
        ),
    ]
    result = dict(
        pipeline_version=4,
        execution_state="complete",
        state="confirmed",
        sources=sources,
        summary="Норма подтверждена.",
        items=[
            dict(
                topic="Нормативная применимость",
                state="confirmed",
                required=True,
                claim_ids=["normative:applicability"],
                project_source_ids=["pd"],
                normative_source_ids=["base"],
                explanation="Базовая толщина 4 см, проектная 5 см; поправок нет — требуется инженерная проверка.",
            )
        ],
        parameter_proposals=[
            dict(
                parameter="quantity",
                source_id="cell",
                value="106",
                unit="м2",
                quote="Площадь, м2\nВсего: 106,00",
                scope_quote=sources[1]["text"],
                scope_state="verified",
            ),
            dict(
                parameter="thickness",
                source_id="pd",
                value="5",
                unit="см",
                quote="h=5 см",
                scope_quote=scope,
                scope_state="verified",
            ),
            dict(
                parameter="unit_scale",
                source_id="base",
                value="1000",
                unit="м2",
                quote="Измеритель: 1000 м2",
                scope_quote="Измеритель: 1000 м2",
                scope_state="verified",
            ),
        ],
        related_positions=[],
        evidence_trace=[],
    )
    return position, result


def selected_adjustment(result, steps=2):
    source = next(s for s in result["sources"] if s["id"] == "step")
    base = next(s for s in result["sources"] if s["id"] == "base")
    result["items"][0]["normative_source_ids"].append("step")
    proposal = AdjustmentProposal(
        base_source_id="base",
        adjustment_source_id="step",
        signed_steps=steps,
        base_quote=base_thickness(base["text"])["quote"],
        adjustment_quote=source["text"],
    )
    result["evidence_trace"] = [dict(kind=PROPOSAL, **proposal.model_dump())]


def check(position, result):
    return findings(position, result, {position.index: position}, {})


def test_false_green_74_is_demoted_without_accusing_vor_or_changing_geometry():
    position, result = asphalt_case()
    original = deepcopy(result)
    (issue,) = check(position, result)
    assert issue["operation_cm"] == "5.00" and issue["base_cm"] == "4.00"
    assert issue["adjustment_candidates"][0]["signed_steps"] == 2
    assert feedback(position, result, {74: position}, {})
    checked = enrich(position, result, {74: position}, {})
    assert checked["state"] == "needs_facts"
    assert all(i["state"] == "needs_facts" for i in checked["items"])
    assert any(e["kind"] == TRACE for e in checked["evidence_trace"])
    assert all(c["state"] == "confirmed" for c in checked["parameter_cards"])
    assert result == original


def test_verified_physical_header_geometry_still_requires_norm_thickness_adjustment():
    position,result=asphalt_case()
    pd=next(s for s in result['sources'] if s['id']=='pd')
    pd.update(origin='ocr_verified_header',numeric_verified=True,
        verified_parameters=[dict(parameter='thickness',value='5',unit='см')])
    assert check(position,result)
    selected_adjustment(result)
    assert not check(position,result)


def test_preceding_material_paragraph_does_not_contaminate_selected_literal_layer():
    position,result=asphalt_case()
    position.work_name=position.work_name.replace('верхнего слоя усиления','слоя покрытия')
    pd=next(s for s in result['sources'] if s['id']=='pd')
    pd['text']='Нижний слой основания — ЩПС, h=20 см.\n'+pd['text']
    result['parameter_proposals'][1]['scope_quote']=pd['text']
    selected_adjustment(result)
    assert not check(position,result)


def test_literal_cross_table_step_and_signed_arithmetic_pass_without_promoting_any_claim():
    position, result = asphalt_case()
    selected_adjustment(result)
    result["items"][0]["state"] = "needs_facts"
    result["state"] = "needs_facts"
    assert check(position, result) == []
    assert (
        enrich(position, result, {74: position}, {})["items"][0]["state"]
        == "needs_facts"
    )


@pytest.mark.parametrize(
    "change",
    [
        "steps",
        "source",
        "quote",
        "base_quote",
        "not_cited",
        "family",
        "pdf",
        "version",
        "unit",
        "direction",
        "not_referenced",
        "extra_field",
        "saved_proof",
        "unverified_measure",
        "contradictory_proposal",
    ],
)
def test_unproved_adjustment_never_closes_the_gap(change):
    position, result = asphalt_case()
    selected_adjustment(result)
    proposal = result["evidence_trace"][0]
    step = result["sources"][3]
    if change == "steps":
        proposal["signed_steps"] = -2
    elif change == "source":
        proposal["adjustment_source_id"] = "invented"
    elif change == "quote":
        proposal["adjustment_quote"] = "На 0,5 см к норме 27-06-031-01"
    elif change == "base_quote":
        proposal["base_quote"] = "толщина слоя 5 см"
    elif change == "not_cited":
        result["items"][0]["normative_source_ids"] = ["base"]
    elif change == "family":
        step["label"] = step["label"].replace("ГЭСН", "ФЕР")
    elif change == "pdf":
        step["source_id"] = "other"
    elif change == "version":
        step["version_id"] = "other"
    elif change == "unit":
        step["text"] = step["text"].replace("1000 м2", "1000 м3")
        proposal["adjustment_quote"] = step["text"]
    elif change == "direction":
        step["text"] = step["text"].replace("добавлять или ", "")
        proposal["adjustment_quote"] = step["text"]
    elif change == "not_referenced":
        step["text"] = step["text"].replace(
            "к норме 27-06-031-01", "к норме 27-06-031-02"
        )
        proposal["adjustment_quote"] = step["text"]
    elif change == "extra_field":
        proposal["validated"] = True
    elif change == "saved_proof":
        proposal["kind"] = TRACE
    elif change == "unverified_measure":
        result["parameter_proposals"][2]["scope_state"] = "unresolved"
    elif change == "contradictory_proposal":
        result["evidence_trace"].append(dict(proposal, signed_steps=3))
    assert check(position, result)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_scope",
        "ambiguous_base_thickness",
        "unreadable",
        "wrong_quantity",
    ],
)
def test_unknown_premise_does_not_infer_a_thickness_obligation(change):
    position, result = asphalt_case()
    if change == "unknown_scope":
        result["parameter_proposals"][1]["scope_state"] = "unresolved"
    elif change == "ambiguous_base_thickness":
        result["sources"][2]["text"] = "толщина 9 см\n" + result["sources"][2]["text"]
    elif change == "unreadable":
        result["sources"][2]["origin"] = "ocr_unverified"
    elif change == "wrong_quantity":
        result["parameter_proposals"][0]["value"] = "105"
    assert check(position, result) == []


def test_equal_thickness_requires_no_adjustment_and_is_not_promoted():
    position, result = asphalt_case()
    result["sources"][2]["text"] = result["sources"][2]["text"].replace(
        "слоя 4 см", "слоя 5 см"
    )
    assert check(position, result) == []


def test_ambiguous_actual_bases_cannot_be_resolved_by_one_valid_adjustment():
    position, result = asphalt_case()
    selected_adjustment(result)
    other = deepcopy(result["sources"][2])
    other["id"] = "other_base"
    other["label"] = other["label"].replace("031-01", "031-02")
    other["text"] = other["text"].replace("031-01", "031-02")
    result["sources"].append(other)
    result["parameter_proposals"].append(
        dict(result["parameter_proposals"][2], source_id="other_base")
    )
    result["items"][0]["normative_source_ids"].append("other_base")
    assert all(
        f["reason"] == "ambiguous_selected_base" for f in check(position, result)
    )


def test_a_fractional_step_is_not_rounded_into_an_allowed_adjustment():
    position, result = asphalt_case()
    position.work_name = position.work_name.replace("5 см", "5,2 см")
    result["sources"][0]["text"] = result["sources"][0]["text"].replace(
        "5 см", "5,2 см"
    )
    p = result["parameter_proposals"][1]
    p.update(value="5.2", quote="h=5,2 см", scope_quote=result["sources"][0]["text"])
    (issue,) = check(position, result)
    assert issue["adjustment_candidates"] == []


def test_lower_layer_uses_operation_12_not_project_24_and_valid_minus_three_passes():
    position, result = actual_lower_layer_case()
    base = next(s for s in result["sources"] if s["id"] == "selected")
    base["id"] = "base"
    base["label"] = base["label"].replace("-02", "-06")
    base["text"] = base["text"].replace("-02", "-06").replace("12 см", "15 см")
    result["parameter_proposals"][-1]["source_id"] = "base"
    result["items"][0]["normative_source_ids"] = ["base", "clause"]
    result["sources"].append(
        dict(
            id="step",
            kind="normative",
            origin="native",
            source_id="norm-pdf",
            label="ГЭСН 27 · 27-04-003-08",
            text="Измеритель: 1000 м2\n27-04-003-08 На каждый 1 см изменения толщины слоя добавлять или исключать к нормам с 27-04-003-05 по 27-04-003-07",
        )
    )
    (issue,) = check(position, result)
    assert (
        issue["operation_cm"] == "12.00"
        and issue["adjustment_candidates"][0]["signed_steps"] == -3
    )
    selected_adjustment(result, -3)
    assert check(position, result) == []
    result["related_positions"] = []
    assert (
        check(position, result) == []
    )  # No guessed technological split if its ordinary proof is absent.


def test_structured_adjustment_aliases_are_restored_and_schema_is_internal():
    from normative_agent import restore_source_ids
    from normative_position_check import AgentAnswer

    answer = AgentAnswer(
        summary="",
        items=[],
        adjustments=[
            dict(
                base_source_id="s0",
                adjustment_source_id="s1",
                signed_steps=2,
                base_quote="толщина 4 см",
                adjustment_quote="literal",
            )
        ],
    )
    restore_source_ids(answer, {"s0": "base", "s1": "step"})
    assert (
        answer.adjustments[0].base_source_id == "base"
        and answer.adjustments[0].adjustment_source_id == "step"
    )


@pytest.mark.parametrize(
    "base_table,step_table", [("27-06-031", "27-06-032"), ("27-06-029", "27-06-030")]
)
def test_cross_table_navigation_uses_literal_reference_not_number_adjacency(
    base_table, step_table
):
    base = norm("01", table_code=base_table)
    step = norm(
        "01",
        table_code=step_table,
        text=f"Измеритель: 1000 м2\nПри изменении толщины покрытия на 0,5 см добавлять или исключать:\n{step_table}-01 к норме {base_table}-01",
    )
    assert adjustment_references(step, base)
    kb = Corpus([base, step])
    result = same_table_adjustments(kb, [base], {})
    assert [r["code"] for r in result] == [step_table + "-01"]
    assert result[0]["automatic_application_allowed"] is False
    wrong = deepcopy(step)
    wrong["text"] = wrong["text"].replace(base_table + "-01", base_table + "-02")
    assert not adjustment_references(wrong, base)
    assert (
        len(
            preflight_candidates(kb, ["work"], {}, definition_queries=["work"], limit=1)
        )
        == 1
    )


def test_cross_table_arithmetic_candidate_requires_literal_reference_and_equal_measure():
    position, result = asphalt_case()
    candidates = thickness_adjustments(position.work_name, result["sources"])
    assert len(candidates) == 1 and candidates[0]["signed_steps"] == "2"
    assert not candidates[0]["scope_verified"]
    result["sources"][3]["text"] = result["sources"][3]["text"].replace(
        "1000 м2", "1000 м3"
    )
    assert thickness_adjustments(position.work_name, result["sources"]) == []


@pytest.mark.parametrize("state", ["confirmed", "needs_facts", "possible_mismatch"])
def test_mixed_item_keeps_other_claim_states_and_source_gates(state):
    from normative_thickness_adjustment import apply_guard

    position, result = asphalt_case()
    item = result["items"][0]
    item.update(state=state, claim_ids=["normative:applicability", "method"])
    original = deepcopy(item)
    assert apply_guard(position, result, {74: position}, {})
    remaining = next(i for i in result["items"] if i["claim_ids"] == ["method"])
    assert remaining["state"] == state
    assert remaining["project_source_ids"] == original["project_source_ids"]
    assert remaining["normative_source_ids"] == original["normative_source_ids"]
    assert remaining["explanation"] == original["explanation"]
    assert all(
        i["state"] == "needs_facts"
        for i in result["items"]
        if "normative:applicability" in i["claim_ids"]
    )


def test_independent_quantity_conflict_is_not_erased_by_adjustment_check():
    from normative_thickness_adjustment import apply_guard

    position, result = asphalt_case()
    position.volume = "100"
    result["items"][0].update(
        state="possible_mismatch", claim_ids=["normative:applicability", "quantity"]
    )
    original = deepcopy(result)
    assert not apply_guard(position, result, {74: position}, {})
    assert result == original


@pytest.mark.parametrize(
    "direction",
    [
        "не добавлять или исключать",
        "добавлять запрещается",
        "добавление или исключение",
        "за исключением иных работ",
        "добавлять если предусмотрено проектом",
        "добавлять при работе в сложных условиях",
    ],
)
def test_negated_noun_or_unproved_conditional_direction_is_not_a_rule(direction):
    base = norm("01", table_code="27-06-031")
    step = norm(
        "01",
        table_code="27-06-032",
        text="При изменении толщины покрытия на 0,5 см "
        + direction
        + ":\n27-06-032-01 к норме 27-06-031-01",
    )
    assert not adjustment_references(step, base)


def test_lower_bound_hmin_is_not_reinterpreted_as_an_exact_operation():
    position, result = asphalt_case()
    position.work_name = position.work_name.replace("h = 5 см", "hmin=5 см")
    assert check(position, result) == []


def test_private_adjustment_answer_does_not_extend_public_position_dto():
    from workspace_models import NormativePositionCheck

    assert "adjustments" not in NormativePositionCheck.model_fields


@pytest.mark.parametrize("case", ["unnamed_work_material", "conflicting_norm_material"])
def test_known_thickness_difference_cannot_pass_through_unresolved_material_family(
    case,
):
    position, result = asphalt_case()
    selected_adjustment(result)
    if case == "unnamed_work_material":
        position.work_name = position.work_name.replace(
            "из асфальтобетонной смеси ", ""
        )
    else:
        result["sources"][2]["text"] = result["sources"][2]["text"].replace(
            "асфальтобетонных", "щебеночно-песчаных"
        )
    (issue,) = check(position, result)
    assert issue["reason"] == "unresolved_material_family"
    assert issue["adjustment_candidates"] == []
    assert issue["operation_cm"] == "5.00" and issue["base_cm"] == "4.00"
    checked = enrich(position, result, {74: position}, {})
    assert checked["state"] == "needs_facts"
    assert all(i["state"] != "possible_mismatch" for i in checked["items"])
    assert all(
        c["state"] == "confirmed"
        for c in checked["parameter_cards"]
        if c["id"] in {"quantity", "thickness"}
    )


@pytest.mark.parametrize(
    "missing", ["page", "version", "scope", "literal", "single_proposal"]
)
def test_negative_family_premise_still_requires_one_literal_verified_pinned_source(
    missing,
):
    position, result = asphalt_case()
    position.work_name = position.work_name.replace("из асфальтобетонной смеси ", "")
    if missing == "page":
        result["sources"][0].pop("page")
    elif missing == "version":
        result["sources"][0].pop("version_id")
    elif missing == "scope":
        result["parameter_proposals"][1]["scope_state"] = "unresolved"
    elif missing == "literal":
        result["parameter_proposals"][1]["scope_quote"] = "h=5 см в другой конструкции"
    elif missing == "single_proposal":
        result["parameter_proposals"].append(deepcopy(result["parameter_proposals"][1]))
    assert check(position, result) == []


def test_unknown_family_does_not_invent_an_adjustment_when_thickness_is_equal():
    position, result = asphalt_case()
    position.work_name = position.work_name.replace("из асфальтобетонной смеси ", "")
    result["sources"][2]["text"] = result["sources"][2]["text"].replace(
        "слоя 4 см", "слоя 5 см"
    )
    assert check(position, result) == []
