"""Material scope must survive the actual initial and evidence prompt paths."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from audit_models import VorPosition
from normative_agent import EvidenceRequest, run
from normative_position_check import AgentAnswer, answer_feedback
from workspace_models import NormativeParameterProposal
from normative_verification_contract import INSTRUCTIONS, plan, review_scope
from test_normative_agent import toolkit


def check_instructions():
    # Evaluate the real constant expression passed to run_agent, without running
    # check(), loading a workspace, credentials, a corpus or a model provider.
    import normative_position_check

    tree = ast.parse(Path(normative_position_check.__file__).read_text(encoding="utf-8"))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "run_agent")
    return eval(compile(ast.Expression(call.args[3]), "<check instructions>", "eval"),
        {"__builtins__": {}, "relation_instructions": INSTRUCTIONS})


@pytest.mark.parametrize("followup", [False, True])
def test_complete_wire_keeps_material_subject_in_initial_and_evidence_rounds(followup):
    material = VorPosition(index=0, work_name="ЩПС", position_type="Материал",
        unit="м3", volume="12")
    scope = review_scope(material)
    tasks = plan(material, {"normative:applicability", "derived:rate"})
    tools = toolkit()
    tools.sources = [dict(id="norm", kind="normative", text="Измеритель: 1000 м2")]
    tools.execute = Mock(return_value=dict(source_id="norm", text="Измеритель: 1000 м2"))
    inputs: dict[str, Any] = dict(position=dict(work="ЩПС", unit="м3"), verification_plan=tasks,
        normative_review_scope=scope, required_validation_claims=["normative:applicability"],
        normative_work_owner=dict(work="Устройство основания", unit="м2", is_parent=True))
    if followup:
        inputs["evidence_followup"] = dict(actions=[dict(claim_id="normative:applicability")])
    ask = Mock(side_effect=[AgentAnswer(summary="Чтение", items=[],
        requests=[EvidenceRequest(tool="read_source", source_id="norm")]), AgentAnswer(summary="Вывод", items=[])])
    run(ask, None, None, check_instructions(), inputs, AgentAnswer, None, {"queries": []}, tools)
    assert ask.call_count == 2
    for call in ask.call_args_list:
        system, wire = call.args[2], call.args[3]
        assert wire["normative_review_scope"] == scope
        assert "normative:applicability" in wire["required_validation_claims"]
        task = next(t for t in wire["verification_plan"] if t["claim_id"] == "normative:applicability")
        assert task["verification"]["subject"] == "material_specification_and_quantity_basis"
        assert task["verification"]["required"]
        assert not task["verification"]["automatic_confirmation"]
        assert "Для самостоятельного материала normative:applicability ОБЯЗАТЕЛЕН" in system
        assert "потерь и коэффициентов" in system
        assert "Если расход материала получен из ресурса рабочей нормы" in system
        assert "докажи применимость именно" in system
        assert "unit_scale не обязателен" in system
        assert "Не назначай всем материалам площадь родителя" in system
        assert "заявленное распределение" in system
        assert "Норму и unit_scale подбирай для normative_work_owner" not in system
        assert "у расчётного материала это родительская работа" not in system
        assert "quantity — количество родительской работы" not in system
        assert "Расход материала ищи в ресурсах той же применимой нормы" not in system


def test_work_scope_and_material_norm_dependency_are_not_disabled():
    work = VorPosition(index=0, work_name="Устройство основания", unit="м2", volume="10")
    assert review_scope(work)["subject"] == "work_norm_applicability"
    material = work.model_copy(update={"position_type": "Материал"})
    scope = review_scope(material)
    assert scope["required"]
    assert scope["work_norm_conditions_required_when"] == "quantity_depends_on_work_norm_resource_rate"
    assert "Непроверенную зависимость от нормы нельзя заменить подтверждённой геометрией" in check_instructions()


def scale_candidate(kind="normative", unit="м2", quote="Измеритель: 1000 м2"):
    sources = [dict(id="n", kind=kind, text=quote)]
    tools = SimpleNamespace(sources=sources, events=[], coverage={}, preferred=[], table_search_done=True)
    answer = AgentAnswer(summary="Измеритель", items=[], parameters=[NormativeParameterProposal(parameter="unit_scale",
        value="1000", unit=unit, source_id="n", quote=quote, scope_quote=quote, scope_state="verified")])
    return answer, sources, tools


@pytest.mark.parametrize("explicit", [False, True])
def test_typed_area_input_allows_literal_area_norm_without_retyping_material_mass(explicit):
    p = VorPosition(index=0, work_name="Материал", position_type="Материал", unit="т", volume="10",
        formula="1001*10/1000", comment="Расход 10 кг/м2")
    args: dict[str, Any] = dict(position=p)
    if explicit:
        # This is also the path when check() derived the contract from a parent.
        p.formula, p.comment = "", ""
        args["project_quantity_contract"] = dict(project_input=dict(unit="м2"), output_unit="т")
    answer, sources, tools = scale_candidate()
    assert answer_feedback(answer, sources, tools, owner_unit="т", **args) == []
    assert tools.parameter_evidence["unit_scale"]["unit"] == "м2"
    assert not answer.items  # No mechanical applicability confirmation.


@pytest.mark.parametrize("kind,unit,quote,reason", [
    ("project", "м2", "Измеритель: 1000 м2", "нормативный источник"),
    ("normative", "м2", "Смесь; м2: 1000", "не из количества или единицы ресурса"),
    ("normative", "м3", "Измеритель: 1000 м3", "размерность"),
])
def test_typed_input_still_rejects_fabricated_resource_or_unrelated_measures(kind, unit, quote, reason):
    answer, sources, tools = scale_candidate(kind, unit, quote)
    issues = answer_feedback(answer, sources, tools, owner_unit="т",
        project_quantity_contract=dict(project_input=dict(unit="м2")))
    assert any(reason in issue for issue in issues)
    assert not tools.parameter_evidence


def test_independent_material_geometry_does_not_require_a_norm_measure():
    p = VorPosition(index=0, work_name="ЩПС", position_type="Материал", unit="м3", volume="12")
    answer = AgentAnswer(summary="Геометрия", items=[], parameters=[])
    tools = SimpleNamespace(sources=[], events=[])
    assert answer_feedback(answer, [], tools, owner_unit="м2", position=p) == []
    # No invented default scale is introduced by either feedback or contract.
    assert not answer.parameters
    assert not tools.parameter_evidence
