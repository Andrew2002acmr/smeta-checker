"""A physical VOR quantity cannot silently certify total resource consumption."""

from copy import deepcopy

import pytest

from audit_models import VorPosition
from normative_verification_contract import (
    NORMATIVE_SCOPE_INSTRUCTIONS,
    plan,
    quantity_subject,
    review_scope,
)
from normative_workbook_rate import quantity_contract
from normative_position_check import AgentAnswer, answer_feedback
from test_material_prompt_contract import scale_candidate


def material(**updates):
    return VorPosition(
        index=101,
        work_name="Щебеночно-песчаная смесь",
        position_type="Материал",
        volume="54",
        unit="м3",
        **updates,
    )


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({}, "project_physical_volume"),
        (
            {"formula": "1931*20/100", "comment": "Расход 20м3/100м2"},
            "project_physical_volume",
        ),
        (
            {"formula": "431.61*0.1*1.26", "comment": "Купл-1,26"},
            "project_volume_with_declared_adjustment",
        ),
        (
            {
                "formula": "107.67*1.26",
                "comment": "Коэффициент запаса на уплотнение 1,26",
            },
            "project_volume_with_declared_adjustment",
        ),
        ({"comment": "Полный расход материала"}, "normative_total_consumption"),
        (
            {"comment": "Общий сметный расход с учетом потерь"},
            "normative_total_consumption",
        ),
        (
            {"comment": "С учетом трудноустранимых отходов"},
            "normative_total_consumption",
        ),
        (
            {"comment": "ГЭСН расход П; проектный объём без учета потерь"},
            "project_physical_volume",
        ),
        ({"formula": "100*0.2**", "comment": "Купл-1,26"}, "unresolved"),
    ],
)
def test_subject_follows_physical_frame_dimensions_and_explicit_vor_claim(
    updates, expected
):
    position = material(**updates)
    before = position.model_dump()
    metadata = quantity_subject(position)
    assert metadata["quantity_subject"] == expected
    assert position.model_dump() == before
    limits = metadata["proof_limits"]
    assert limits["scope_classification_only"]
    assert not any(
        limits[key]
        for key in (
            "numbers_verified",
            "geometry_verified",
            "adjustments_verified",
            "allocation_verified",
            "total_resource_consumption_verified",
            "losses_included_verified",
            "zero_losses_verified",
        )
    )
    scope = review_scope(position)
    task = next(
        t
        for t in plan(position, {"normative:applicability"})
        if t["claim_id"] == "normative:applicability"
    )
    assert (
        scope["quantity_subject"]
        == task["verification"]["quantity_subject"]
        == expected
    )
    assert scope["required"] and task["verification"]["required"]
    assert not task["verification"]["automatic_confirmation"]


def test_area_rate_to_mass_never_claims_volume_or_guesses_density():
    p = VorPosition(
        index=44,
        work_name="Жидкий материал",
        position_type="Материал",
        volume="1",
        unit="т",
        formula="1001*0.8/1000",
        comment="Расход 0,8 л/м2",
    )
    metadata = quantity_subject(p)
    assert metadata["quantity_subject"] == "unresolved"
    assert metadata["proof_limits"]["density_required"]
    typed = quantity_contract(p)
    assert typed is not None
    assert typed["project_input"]["unit"] == "м2" and typed["output_unit"] == "т"
    assert typed["density_required"] and not typed["verified"]
    assert typed["quantity_subject"] == "unresolved"


def test_existing_typed_input_is_unchanged_by_subject_metadata():
    p = material(formula="1931*20/100", comment="Расход 20м3/100м2")
    typed = quantity_contract(p)
    assert typed is not None
    assert typed["quantity_subject"] == "project_physical_volume"
    assert typed["project_input"] == dict(
        claim_id="input:area", parameter="quantity", role="calculation_input", unit="м2"
    )
    assert typed["rate_unit"] == "м3/100м2" and not typed["verified"]
    assert typed["proof_limits"]["total_resource_consumption_verified"] is False


def test_metadata_only_does_not_change_numeric_feedback_or_introduce_area_input():
    p = material(formula="431.61*0.1*1.26", comment="Купл-1,26")
    answer, sources, tools = scale_candidate()
    plain = answer_feedback(
        deepcopy(answer),
        deepcopy(sources),
        deepcopy(tools),
        owner_unit="м3",
        position=p,
    )
    answer2, sources2, tools2 = scale_candidate()
    scoped = answer_feedback(
        AgentAnswer.model_validate(answer2.model_dump()),
        sources2,
        tools2,
        owner_unit="м3",
        position=p,
        project_quantity_contract=quantity_subject(p),
    )
    assert plain == scoped
    assert "project_input" not in quantity_subject(p)


def test_loss_condition_is_conditional_and_does_not_erase_other_obligations():
    text = NORMATIVE_SCOPE_INSTRUCTIONS
    assert (
        "Для normative_total_consumption установи нормативные потери/отходы и их учёт"
        in text
    )
    assert "не объявляй ни gross, ни нулевые потери доказанными" in text
    assert (
        "распределения материала между операциями и всех заявленных коэффициентов"
        in text
    )
    assert "докажи применимость именно" in text


def test_a_model_subject_flag_cannot_override_the_declared_physical_recipe():
    p = material(formula="1931*20/100", comment="Расход 20м3/100м2")
    flagged = p.model_copy(
        update={
            "quantity_subject": "normative_total_consumption",
            "gross_confirmed": True,
            "losses_included_verified": True,
        }
    )
    metadata = quantity_subject(flagged)
    assert metadata["quantity_subject"] == "project_physical_volume"
    assert not metadata["proof_limits"]["total_resource_consumption_verified"]
    assert not metadata["proof_limits"]["losses_included_verified"]
