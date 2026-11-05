"""Independent workbook notes never become another property's operands."""

import pytest

from audit_predicates import technical_specs, predicate_check
from audit_models import VorPosition
from normative_claim_migration import normalize_matching


def parse(comment):
    return technical_specs("", comment, "Работа", [])


@pytest.mark.parametrize("separator", ["\n", "; "])
def test_normative_instruction_and_machine_power_are_separate_required_claims(
    separator,
):
    comment = (
        "При толщине основания более 18 см его нормировать в два слоя"
        + separator
        + "Устройство основания производится бульдозером мощностью 79 кВт"
    )
    specs = parse(comment)
    instruction = next(s for s in specs if s.property_label == "normative_instruction")
    assert instruction.claim_id == "basis:comment:consumption:2"
    assert instruction.expected_text == "в два слоя" and instruction.predicate == "text"
    assert instruction.required and "79" not in instruction.label
    machine = next(s for s in specs if s.claim_id == "attribute:comment:separate_notes")
    assert "79 кВт" in machine.expected_text and machine.required
    assert predicate_check(instruction, []).required
    assert predicate_check(machine, []).required


@pytest.mark.parametrize("verb", ["нормировать", "нормируется", "нормируются"])
def test_other_normative_verbs_keep_required_instruction(verb):
    specs = parse(f"Основание {verb} в три слоя\nУкладка выполняется вручную")
    instruction = next(s for s in specs if s.property_label == "normative_instruction")
    assert instruction.expected_text == "в три слоя"
    assert (
        next(s for s in specs if s.claim_id.endswith(":separate_notes")).expected_text
        == "Укладка выполняется вручную"
    )


def test_wrapped_continuation_is_not_split_or_lost():
    instruction = next(
        s
        for s in parse("Основание нормировать\nв два слоя")
        if s.property_label == "normative_instruction"
    )
    assert instruction.expected_text == "в два слоя"


@pytest.mark.parametrize(
    "note",
    [
        "Монтаж только при температуре выше 5 градусов",
        "Необходимо дополнительное испытание",
        "Устройство выполняется с неизвестной оговоркой",
    ],
)
def test_following_technical_or_unknown_condition_stays_required(note):
    specs = parse("Расход 0,4 кг/м2\n" + note)
    consumption = next(s for s in specs if s.property_label == "consumption")
    assert note not in consumption.expected_text
    independent = next(s for s in specs if s.claim_id.endswith(":separate_notes"))
    assert independent.required and independent.expected_text == note


def test_true_consumption_is_not_reclassified_as_normative_instruction():
    specs = parse("Норма расхода 0,4 кг/м2")
    assert any(s.property_label == "consumption" for s in specs)
    assert not any(s.property_label == "normative_instruction" for s in specs)


def test_frozen_matching_adds_separated_note_as_unproved_requirement():
    position = VorPosition(
        index=0,
        work_name="Основание",
        volume="12",
        unit="м2",
        position_type="Работа",
        comment="Основание нормировать в два слоя\nУстройство бульдозером мощностью 79 кВт",
    )
    frozen = dict(
        checks=[
            dict(
                claim_id="basis:comment:consumption:1",
                kind="basis",
                required=True,
                state="confirmed",
                facts=[],
            )
        ]
    )
    result = normalize_matching(position, frozen)
    assert result["checks"][0] == frozen["checks"][0]
    added = next(
        c
        for c in result["checks"]
        if c["claim_id"] == "attribute:comment:separate_notes"
    )
    assert added["required"] and added["state"] == "not_found" and not added["facts"]


def test_agent_work_plan_routes_normative_instruction_to_norms_not_consumption():
    from normative_evidence_plan import build_work_plan

    p = VorPosition(
        index=0,
        work_name="Основание",
        volume="12",
        unit="м2",
        position_type="Работа",
        comment="Основание нормировать в два слоя\nУстройство бульдозером мощностью 79 кВт",
    )
    matching = normalize_matching(
        p,
        dict(
            checks=[
                dict(
                    claim_id="basis:comment:consumption:1",
                    kind="basis",
                    required=True,
                    state="not_found",
                    facts=[],
                )
            ]
        ),
    )
    review = dict(items=[], sources=[], parameter_cards=[])
    plan = build_work_plan(p.model_dump(mode="json"), review, matching)
    action = next(
        a for a in plan["actions"] if a["key"] == "claim:basis:comment:consumption:1"
    )
    assert (
        action["family"] == "norm_selection"
        and action["action"] == "retrieve_normative_or_project_basis"
    )
    assert any(
        a["key"] == "claim:attribute:comment:separate_notes" for a in plan["actions"]
    )


def test_machine_definition_sets_function_task_without_accepting_it():
    from normative_claim_definitions import definition

    specs = parse("Уплотнение основания производится тандемным виброкатком Hamm HD130")
    function = next(s for s in specs if s.property_label == "equipment_function")
    task = definition(function)["evidence_task"]
    assert task["kind"] == "equipment_function_and_method_compatibility"
    assert task["subject"] == function.expected_text and task["project_link_required"]
    assert (
        task["normative_applicability_separate"]
        and task["automatic_confirmation"] is False
    )
    assert predicate_check(function, []).required
    advisory = next(s for s in specs if not s.required)
    assert "evidence_task" not in definition(advisory)
