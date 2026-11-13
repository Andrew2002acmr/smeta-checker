from audit_core import build_claim_specs
from audit_models import ClaimKind, ClaimSpec, VorPosition
from normative_claim_definitions import definition
from normative_verification_contract import contract, definitions_for_plan, plan
import pytest


def test_four_source_responsibilities_do_not_make_refinements_optional():
    assert contract("quantity")["family"] == "project_requirement"
    assert contract("normative:applicability")["family"] == "normative_condition"
    assert contract("derived:rate")["family"] == "calculation_basis"
    refinement = contract("attribute:name:designation:1")
    assert refinement["family"] == "vor_refinement"
    assert refinement["relation"] == "satisfies_project_constraints"
    assert refinement["required"] and not refinement["automatic_confirmation"]
    assert not refinement["project_literal_required"]
    assert not refinement["missing_project_literal_is_mismatch"]


def test_semantic_norm_instruction_overrides_legacy_consumption_handle():
    spec = ClaimSpec(
        claim_id="basis:comment:consumption:1",
        kind=ClaimKind.BASIS,
        property_label="normative_instruction",
        expected_text="Нормировать в два слоя",
    )
    assert definition(spec)["verification"]["family"] == "normative_condition"
    assert definition(spec)["evidence_task"]["kind"] == "normative_condition"


def test_work_plan_preserves_all_claims_and_exact_expected_properties():
    position = VorPosition(
        index=0,
        work_name="Щебеночно-песчаная смесь М800",
        volume="4",
        unit="м3",
        position_type="Материал",
        comment="Коэффициент расхода - 1,26",
    )
    specs = build_claim_specs(position)
    ids = {s.claim_id for s in specs} | {
        "derived:parent",
        "derived:rate",
        "normative:applicability",
    }
    tasks = plan(position, ids)
    assert {t["claim_id"] for t in tasks} == ids
    mark = next(t for t in tasks if ":designation:" in t["claim_id"])
    assert mark["expected_text"] == "М800"
    assert mark["verification"]["family"] == "vor_refinement"
    assert mark["verification"]["required"]


def test_unreviewed_image_evidence_cannot_confirm_or_publish_mismatch():
    from normative_position_check import CheckedItem, validate_items

    for state in ("confirmed", "possible_mismatch"):
        item = CheckedItem(
            topic="Материал",
            state=state,
            explanation="Совместимость",
            claim_ids=["attribute:name:designation:1"],
            project_source_ids=["p"],
            normative_source_ids=["n"],
        )
        for source in ("p", "n"):
            sources = [dict(id="p", kind="project"), dict(id="n", kind="normative")]
            sources[0 if source == "p" else 1]["origin"] = (
                "ocr_inline_unreviewed"
                if source == "p"
                else "ocr_unreviewed_inline_raster"
            )
            assert validate_items([item], sources)[0]["state"] == "needs_facts"


def test_requested_normative_symbol_survives_context_packing(monkeypatch):
    from types import SimpleNamespace
    from normative_agent import EvidenceRequest, compact
    from test_normative_agent import toolkit

    tools = toolkit()
    tools.facts = {}
    tools.kb = SimpleNamespace(search=lambda *args, **kwargs: [dict(id="norm")])
    text = "Scope " * 300 + "Марка В₁; Пл₁."
    candidate = dict(
        id="norm",
        text=text,
        citations=[dict(page=7, source_id="pdf")],
        designation="Test standard",
        code="5.8",
        extraction_status="native_with_visual_transcriptions",
        resources=[],
    )
    monkeypatch.setattr("normative_service.candidate", lambda *args: candidate)
    result = tools.execute(
        EvidenceRequest(tool="search_norms", query="ГОСТ 1234-2022 В1")
    )
    sid = result["results"][0]["source_id"]
    packed = compact({}, tools.sources, tools.preferred)
    received = next(s for s in packed["sources"] if s["id"] == sid)
    assert "В₁" in received["text"] and "Пл₁" in received["text"]


def test_followup_retains_typed_obligation_from_v2_trace():
    from normative_evidence_followup import plan_for

    task = dict(
        key="claim:attribute:name:designation:1",
        action="retrieve_normative_or_project_basis",
        retry_fingerprint="same-input",
        verification=contract("attribute:name:designation:1"),
    )
    payload = dict(
        position_check=dict(
            pipeline_version=9,
            execution_state="complete",
            state="needs_facts",
            evidence_trace=[dict(kind="evidence_work_plan_v2", actions=[task])],
            coverage={},
        )
    )
    assert (
        plan_for(payload)["actions"][0]["verification"]["relation"]
        == "satisfies_project_constraints"
    )


def threshold_plan(comment, work="Устройство среднего слоя основания из ЩПС h=10 см"):
    position = VorPosition(
        index=0,
        work_name=work,
        volume="10",
        unit="м2",
        position_type="Работа",
        comment=comment,
    )
    specs = build_claim_specs(position)
    tasks = {
        t["claim_id"]: t
        for t in plan(
            position, {s.claim_id for s in specs} | {"normative:applicability"}
        )
    }
    return position, specs, tasks


def test_full_comment_routes_normative_threshold_without_replacing_project_geometry_or_action():
    condition = "При толщине однослойного основания или покрытия более 18 см его нормировать в два слоя"
    comment = (
        "Тех.часть ГЭСН 27 п. 1.27.18. "
        + condition
        + "\nУстройство основания производится бульдозером мощностью 79 кВт"
    )
    _, specs, tasks = threshold_plan(comment)
    spec = next(s for s in specs if s.claim_id == "geometry:comment:thickness:1")
    task = tasks[spec.claim_id]
    assert spec.property_label == "thickness" and spec.predicate == "measurement"
    assert spec.expected_text == task["expected_text"] == "18 см"
    assert task["verification"]["version"] == "verification-relations-v4"
    assert task["verification"]["family"] == "normative_condition"
    assert task["verification"]["source_routes"] == ["project", "normative"]
    assert (
        task["verification"]["required"]
        and not task["verification"]["project_literal_required"]
    )
    assert not task["verification"]["automatic_confirmation"]
    evidence = task["evidence_task"]
    assert evidence["subject"] == condition
    assert evidence["threshold"] == dict(
        operator="gt", operator_text="более", value="18", unit="см"
    )
    assert evidence["related_claim_ids"] == ["basis:comment:consumption:2"]
    assert tasks["basis:comment:consumption:2"]["expected_text"] == "в два слоя"
    assert "79 кВт" in tasks["attribute:comment:separate_notes"]["expected_text"]
    assert tasks["normative:applicability"]["verification"]["required"]
    assert {s.claim_id for s in specs} | {"normative:applicability"} == set(tasks)
    # Without the complete comment, definition() cannot make this binding.
    assert definition(spec)["verification"]["family"] == "project_requirement"


@pytest.mark.parametrize(
    "comment",
    [
        "Толщина слоя 8 см",
        "При толщине слоя более 18 см проверить ПД",
        "Толщина слоя 18 см. Нормировать в два слоя",
        "Нормировать в два слоя. Толщина слоя 18 см",
        "При толщине основания более 18 см его нормировать в три слоя",
        "При толщине основания более 18 см его не нормировать в два слоя",
    ],
)
def test_ordinary_or_incomplete_thickness_comment_keeps_project_routing(comment):
    _, specs, tasks = threshold_plan(comment, work="Устройство основания h=8 см")
    thickness = [
        s for s in specs if s.claim_id.startswith("geometry:comment:thickness:")
    ]
    assert thickness
    for spec in thickness:
        assert tasks[spec.claim_id]["verification"]["family"] == "project_requirement"
        assert tasks[spec.claim_id]["verification"]["project_literal_required"]
        assert "evidence_task" not in tasks[spec.claim_id]


def test_ordinary_8cm_and_normative_18cm_in_same_comment_keep_distinct_subjects():
    comment = (
        "Толщина слоя 8 см. При толщине однослойного основания более 18 см "
        "его нормировать в два слоя"
    )
    _, _, tasks = threshold_plan(comment, work="Устройство основания h=8 см")
    ordinary = tasks["geometry:comment:thickness:1"]
    threshold = tasks["geometry:comment:thickness:2"]
    assert ordinary["expected_text"] == "8 см"
    assert ordinary["verification"]["family"] == "project_requirement"
    assert ordinary["verification"]["project_literal_required"]
    assert threshold["expected_text"] == "18 см"
    assert threshold["verification"]["family"] == "normative_condition"
    assert threshold["evidence_task"]["threshold"]["operator"] == "gt"
    assert not threshold["verification"]["automatic_confirmation"]


def test_position_aware_definitions_reuse_plan_context_without_mutating_tasks():
    from copy import deepcopy

    comment = (
        "Толщина слоя 8 см. При толщине однослойного основания более 18 см "
        "его нормировать в два слоя"
    )
    _, specs, mapping = threshold_plan(comment)
    tasks = list(mapping.values())
    original = deepcopy(tasks)
    definitions = {d["claim_id"]: d for d in definitions_for_plan(specs, tasks)}
    for key in ("geometry:comment:thickness:1", "geometry:comment:thickness:2"):
        assert definitions[key]["verification"] == mapping[key]["verification"]
    assert definitions["geometry:comment:thickness:1"] == definition(
        next(s for s in specs if s.claim_id == "geometry:comment:thickness:1")
    )
    bound = definitions["geometry:comment:thickness:2"]
    assert (
        bound["evidence_task"]
        == mapping["geometry:comment:thickness:2"]["evidence_task"]
    )
    bound["evidence_task"]["threshold"]["value"] = "99"
    assert tasks == original


@pytest.mark.parametrize(
    "prefix,operator,value,unit,relation",
    [
        ("однослойного основания", "более", "18", "см", "gt"),
        ("нижнего слоя основания", "не менее", "19", "см", "ge"),
        ("верхнего слоя основания", "не более", "0,21", "м", "le"),
    ],
)
def test_threshold_plan_keeps_the_declared_role_operator_value_and_units(
    prefix, operator, value, unit, relation
):
    comment = (
        f"При толщине {prefix} {operator} {value} {unit} его нормировать в два слоя"
    )
    _, _, tasks = threshold_plan(comment)
    evidence = tasks["geometry:comment:thickness:1"]["evidence_task"]
    assert evidence["subject"] == comment
    assert evidence["threshold"] == dict(
        operator=relation, operator_text=operator, value=value, unit=unit
    )
    assert not evidence["automatic_confirmation"]


@pytest.mark.parametrize(
    "operator,threshold,base_codes",
    [
        ("более", "18", "27-04-003-06 и 27-04-003-07"),
        ("не менее", "19", "27-04-003-06 и 27-04-003-06"),
    ],
)
def test_compound_normative_action_and_every_existing_tail_claim_survive_routing(
    operator, threshold, base_codes
):
    comment = (
        f"При толщине однослойного основания или покрытия {operator} {threshold} см его "
        f"нормировать в два слоя по нормам {base_codes} с изменением толщины ±1 см "
        "для каждого слоя отдельно по норме 27-04-003-08."
    )
    _, specs, tasks = threshold_plan(comment)
    threshold_task = tasks["geometry:comment:thickness:1"]
    assert threshold_task["verification"]["family"] == "normative_condition"
    assert threshold_task["evidence_task"]["comment_context"] == dict(
        field="position.comment", quote=comment
    )
    assert threshold_task["evidence_task"]["related_claim_ids"] == [
        "basis:comment:consumption:2"
    ]
    for spec in specs:
        task = tasks[spec.claim_id]
        assert task["expected_text"] == spec.expected_text
        assert task["verification"]["required"] == spec.required
        assert not task["verification"]["automatic_confirmation"]
        if spec.claim_id != "geometry:comment:thickness:1":
            assert task["verification"] == contract(spec.claim_id, spec)
    assert base_codes in tasks["basis:comment:consumption:3"]["expected_text"]
    assert tasks["basis:comment:consumption:5"]["expected_text"] == "27-04-003-08"
    assert all(
        tasks[key]["verification"]["required"]
        for key in [
            "basis:comment:consumption:2",
            "basis:comment:consumption:3",
            "geometry:comment:thickness:4",
            "basis:comment:consumption:5",
        ]
    )


def test_threshold_routing_does_not_make_middle_project_20cm_an_applicable_single_base():
    from copy import deepcopy
    from normative_layer_conditions import layer_condition_proof
    from normative_local_checks import layer_group
    from normative_parameters import enrich
    from test_normative_layer_role_scope import actual

    position, result = actual(4)
    original = deepcopy(result)
    tasks = {
        t["claim_id"]: t
        for t in plan(
            position,
            {s.claim_id for s in build_claim_specs(position)}
            | {"normative:applicability"},
        )
    }
    assert (
        tasks["geometry:comment:thickness:1"]["evidence_task"]["threshold"]["value"]
        == "18"
    )
    assert (
        layer_condition_proof(
            position, result, layer_group(position, result["related_positions"])
        )
        is None
    )
    checked = enrich(position, result, {position.index: position}, {})
    card = next(c for c in checked["parameter_cards"] if c["id"] == "thickness")
    assert (card["state"], card["found_value"], card["found_unit"]) == (
        "confirmed",
        "20",
        "см",
    )
    assert checked["state"] == "needs_facts"
    assert all(
        i["state"] == "needs_facts"
        for i in checked["items"]
        if "normative:applicability" in i.get("claim_ids", [])
    )
    assert result == original


@pytest.mark.parametrize(
    "condition",
    [
        "При толщине нижнего слоя основания более 18 см его нормировать в два слоя.",
        "При толщине нижнего слоя основания не менее 15 см его нормировать в два слоя.",
    ],
)
def test_routed_comment_does_not_confirm_a_different_normative_threshold_or_operator(
    condition,
):
    from normative_layer_conditions import layer_condition_proof
    from test_normative_layer_conditions import fixture

    position, result, group = fixture()
    tasks = plan(position, {s.claim_id for s in build_claim_specs(position)})
    task = next(t for t in tasks if t["claim_id"] == "geometry:comment:thickness:1")
    assert task["evidence_task"]["threshold"] == dict(
        operator="gt", operator_text="более", value="15", unit="см"
    )
    result["sources"][0]["text"] = condition
    assert layer_condition_proof(position, result, group) is None
