"""The canonical reducer is the only status assigner; directions stay independent."""
import json
from pathlib import Path

import pytest

from audit_assessment import (
    PositionAssessment,
    assessment_fingerprint,
    obligation_ids,
    reduce_assessment,
    revision_for,
)
from audit_models import VorPosition


def reference():
    path = Path(__file__).parent / "fixtures" / "road-home-v31-reference.json"
    return json.loads(path.read_text("utf-8"))


def base_input(**overrides):
    payload = {
        "run_id": "run-1",
        "position_index": 0,
        "execution_revision": 0,
        "fingerprint": {"sha256": "abc"},
        "project": {
            "obligations": [
                {
                    "id": "project:binding",
                    "domain": "presence",
                    "subject": "Нижнее основание типа IV",
                    "state": "confirmed",
                    "fact_ids": ["fact-area"],
                    "source_ids": ["project:p89:lower-area"],
                    "reason": "Площадь привязана к физическому столбцу нижнего основания.",
                },
                {
                    "id": "project:own_quantity_inputs",
                    "domain": "quantity",
                    "subject": "Площадь нижнего основания",
                    "state": "confirmed",
                    "fact_ids": ["fact-area"],
                    "source_ids": ["project:p89:lower-area"],
                    "reason": "345,77 + 85,841 = 431,611 м2.",
                },
                {
                    "id": "project:unit",
                    "domain": "unit",
                    "subject": "Единица площади",
                    "state": "confirmed",
                    "fact_ids": ["fact-area"],
                    "source_ids": ["project:p89:lower-area"],
                    "reason": "Единица м2 подтверждена тем же столбцом.",
                },
                {
                    "id": "project:quantity_subject",
                    "domain": "quantity",
                    "subject": "Предмет количества",
                    "state": "confirmed",
                    "reason": "Прямое проектное количество.",
                },
            ]
        },
        "normative": {
            "obligations": [
                {
                    "id": "normative:applicability",
                    "domain": "norm_selection",
                    "subject": "Выбор нормы",
                    "state": "needs_facts",
                    "gap": "norm-choice",
                    "reason": "Норма не выбрана.",
                }
            ]
        },
        "relationships": {"obligations": []},
        "own_quantity": {
            "kind": "direct",
            "state": "confirmed",
            "unit": "м2",
            "declared_value": "431,61",
            "found_value": "431.611",
            "found_unit": "м2",
            "explanation": "Округление до сотых допустимо.",
        },
        "facts": [
            {
                "id": "fact-area",
                "parameter": "quantity",
                "dimension": "area",
                "value": "431.611",
                "unit": "м2",
                "source_id": "project:p89:lower-area",
                "version_id": "pdf-1",
                "page": 89,
                "literal_state": "verified",
                "subject_state": "verified",
            }
        ],
        "sources": [
            {
                "id": "project:p89:lower-area",
                "kind": "project",
                "label": "Нижнее основание",
                "page": 89,
                "version_id": "pdf-1",
                "text": "345,77 + 85,841 = 431,611 м2",
            }
        ],
        "execution_state": "complete",
    }
    payload.update(overrides)
    return payload


def test_project_confirmation_survives_open_normative_gap():
    assessment = reduce_assessment(base_input())
    assert assessment.project.state == "confirmed"
    assert assessment.normative.state == "needs_facts"
    assert assessment.relationships.state == "not_applicable"
    assert assessment.completion == "incomplete"
    assert assessment.open_gaps == ["norm-choice"]


def test_proved_mismatch_needs_comparability_and_provenance():
    payload = base_input()
    payload["project"]["obligations"][0].update(
        state="possible_mismatch", comparable=False, mismatch_level="proved"
    )
    with pytest.raises(ValueError, match="non-comparable"):
        reduce_assessment(payload)
    payload = base_input()
    payload["project"]["obligations"][0].update(state="possible_mismatch")
    with pytest.raises(ValueError, match="provenance"):
        reduce_assessment(payload)
    payload = base_input()
    payload["project"]["obligations"][0].update(
        state="possible_mismatch", mismatch_level="proved",
        reason="1710 м против 1574 м в том же собственном столбце.",
    )
    payload["normative"]["obligations"][0].update(
        state="confirmed",
        reason="Нормативная ветвь решена отдельно.",
        gap="",
    )
    assessment = reduce_assessment(payload)
    assert assessment.project.state == "possible_mismatch"
    assert assessment.completion == "complete"


def test_missing_project_evidence_is_not_a_mismatch():
    payload = base_input()
    payload["project"]["obligations"][1].update(state="needs_facts", gap="area-input")
    assessment = reduce_assessment(payload)
    assert assessment.project.state == "needs_facts"
    assert assessment.completion == "incomplete"


def test_running_execution_keeps_project_pending():
    payload = base_input(execution_state="running")
    assessment = reduce_assessment(payload)
    assert assessment.project.state == "pending"
    assert assessment.completion == "pending"


def test_derived_mass_does_not_become_confirmed_without_density_basis():
    payload = base_input(position_index=8)
    payload["own_quantity"] = {
        "kind": "derived",
        "state": "unresolved",
        "unit": "т",
        "declared_value": "0,345288",
        "operands": ["project:p89:lower-area"],
        "basis": ["comment:0,8 л/м2"],
        "conditions": ["density_unproved"],
        "explanation": "Площадь подтверждена; плотность не подтверждена.",
    }
    assessment = reduce_assessment(payload)
    assert assessment.project.state == "confirmed"
    assert assessment.own_quantity.state == "unresolved"


def test_obligations_must_reference_stored_evidence():
    payload = base_input()
    payload["project"]["obligations"][0]["fact_ids"] = ["missing-fact"]
    with pytest.raises(ValueError, match="stored facts"):
        reduce_assessment(payload)


def test_revision_is_stable_and_binding():
    first = reduce_assessment(base_input())
    second = reduce_assessment(base_input())
    assert first.revision == second.revision
    assert len(first.revision) == 64
    tampered = first.model_dump(mode="json")
    tampered["project"]["summary"] = "Другое пояснение"
    with pytest.raises(ValueError, match="revision"):
        PositionAssessment.model_validate(tampered)
    assert revision_for(first.model_dump(mode="json", exclude={"revision"})) == first.revision


def test_obligation_ids_route_semantic_normative_conditions():
    position = VorPosition(
        index=0,
        work_name="Устройство нижнего слоя основания из ЩПС",
        volume="431,61",
        unit="м2",
        position_type="Работа",
        comment="При толщине однослойного основания более 18 см его нормировать в два слоя",
    )
    routes = obligation_ids(position)
    assert "project:binding" in routes["project"]
    assert "normative:applicability" in routes["normative"]
    assert any(item.startswith("normative:condition:geometry:comment:thickness:") for item in routes["normative"])
    assert not any(item.startswith("project:property:basis:") for item in routes["project"])


def test_fixture_pins_control_inputs():
    data = reference()
    assert data["vor_sha256"] == "4b5d03827ea0ce64892576eac956ba3c2f3c3e794d62be431e0255c61926c7aa"
    assert data["pdf_sha256"] == "e48108c661864b6e086f4c8c265a7ac0edd398cfc22cf0f6155c986751154c9f"
    assert data["selected_indices"] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 18, 28, 32]


def test_fingerprint_covers_profile_and_policy():
    position = VorPosition(index=0, work_name="Работа", volume="1", unit="м2")
    fingerprint = assessment_fingerprint(
        position=position,
        run_config={"pdf_versions": {"doc": "pdf-1"}, "normative_profile": "roads"},
        prompt_schema="request-evidence-v1",
    )
    assert fingerprint["inputs"]["normative"]["profile"] == "roads"
    assert fingerprint["inputs"]["policy"]["assessment"] == "assessment-obligations-v1"
    assert len(fingerprint["sha256"]) == 64

def test_tool_cell_sources_project_to_canonical_shape():
    payload = base_input()
    payload["sources"] = [*payload["sources"], {
        "id": "agent:p89:cell:abc", "kind": "project", "label": "ПД",
        "page": 89, "version_id": "pdf-1", "text": "Основание | Площадь\nВсего:\n345,77",
        "origin": "ocr", "literal_text": "345,77",
        "numeric_verified": True, "unit_verified": True, "unit": "м2",
        "rects": [[0.1, 0.2, 0.3, 0.4]], "source_anchor": {"state": "ok"},
        "table_id": "t1", "ocr_observations": [], "verified_parameters": [],
        "header_observation_addresses": [],
    }]
    assessment = reduce_assessment(payload)
    assert assessment.sources[1].id == "agent:p89:cell:abc"
    assert assessment.sources[1].page == 89
    assert "345,77" in assessment.sources[1].text
