from copy import deepcopy
from typing import Any

import pytest

from audit_models import VorPosition
from normative_material_search import (
    TOPIC,
    apply_specification_compatibility,
    specification_proof,
)


def fixture(grade="М800"):
    p = VorPosition(
        index=1,
        work_name=f"Щебеночно-песчаная смесь {grade} типа 0/63 мм",
        unit="м3",
        volume="54",
        comment="ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26",
    )
    from audit_core import build_claim_specs

    claim = next(s.claim_id for s in build_claim_specs(p) if s.expected_text == grade)
    scope = (
        "Средний слой основания — щебеночно-песчаная смесь фр. 0/63 по ГОСТ Р 70458-2022, "
        "категории K90, марке M1, h=20 см"
    )
    definitions = (
        "5.2 Готовые смеси в зависимости от гранулометрического состава делят на категории и марки. "
        "Категорию готовой смеси определяют при ее рассеве в зависимости от проходов через контрольные сита "
        "с размерами ячеек D и 1,4D. Марку готовой смеси определяют при ее рассеве в зависимости от проходов "
        "через контрольные сита с размерами ячеек менее D. Таблица 1: Тип смеси 0/63; Категория К90; Марка М1."
    )
    reference = (
        "5.9 Щебень (гравий), входящий в состав готовых смесей, по дробимости, морозостойкости и устойчивости "
        "структуры против распадов должен соответствовать требованиям ГОСТ 32703."
    )
    aggregate = (
        "5.6 Дробимость. Щебень и гравий подразделяются на шесть марок по значению дробимости. "
        "Марка по дробимости щебня из изверженных и метаморфических горных пород: М1000; М800; М600. "
        "Марка по дробимости щебня из осадочных пород: М1000; М800; М600."
    )
    r: dict[str, Any] = dict(
        items=[
            dict(
                topic="Совместимость",
                state="needs_facts",
                required=True,
                claim_ids=[claim],
                explanation="Не доказана связь М800 с М1/К90",
                project_source_ids=["pd"],
                normative_source_ids=[],
            ),
            dict(
                topic="Расход",
                state="needs_facts",
                claim_ids=["derived:rate"],
                explanation="Расход требует проверки",
                project_source_ids=[],
                normative_source_ids=[],
            ),
        ],
        parameter_cards=[dict(id="thickness", state="confirmed", source_ids=["pd"])],
        parameter_proposals=[
            dict(
                parameter="thickness",
                source_id="pd",
                scope_quote=scope,
                scope_state="verified",
            )
        ],
        sources=[
            dict(
                id="pd",
                kind="project",
                version_id="pinned-pd",
                page=50,
                origin="native",
                text=scope,
            ),
            dict(
                id="def",
                kind="normative",
                source_id="gostmix",
                page=4,
                origin="native",
                label="ГОСТ Р 70458-2022 · 5.2",
                text=definitions,
            ),
            dict(
                id="ref",
                kind="normative",
                source_id="gostmix",
                page=7,
                origin="native",
                label="ГОСТ Р 70458-2022 · 5.9",
                text=reference,
            ),
            dict(
                id="aggregate",
                kind="normative",
                source_id="gostaggregate",
                page=7,
                origin="native",
                label="ГОСТ 32703-2014 · 5.6",
                text=aggregate,
            ),
        ],
    )
    return p, r, claim


@pytest.mark.parametrize("grade", ["М600", "М800", "M1000"])
def test_grade_refines_specification_without_equivalence_or_batch_proof(grade):
    p, r, claim = fixture(grade)
    original = deepcopy(r)
    out = apply_specification_compatibility(p, r)
    item = next(i for i in out["items"] if claim in i["claim_ids"])
    assert item["state"] == "confirmed" and item["project_source_ids"] == ["pd"]
    assert set(item["normative_source_ids"]) == {"def", "ref", "aggregate"}
    proof = out["evidence_trace"][-1]["proof"]
    assert proof["batch_compliance_proven"] is proof["equivalence_asserted"] is False
    assert out["items"][1] == r["items"][1] and r == original
    again = apply_specification_compatibility(p, out)
    assert again["items"] == out["items"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_definition",
        "wrong_definition",
        "missing_link",
        "wrong_link_pdf",
        "wrong_grade",
        "wrong_norm",
        "unreviewed_norm",
        "unreviewed_project",
        "wrong_fraction",
        "wrong_comment",
        "unresolved_scope",
        "unknown_origin",
        "wrong_scope",
        "different_strength_constraint",
        "unwritten_strength_limit",
        "plasticity_constraint",
        "ambiguous_mark",
        "ambiguous_category",
    ],
)
def test_missing_or_conflicting_dependencies_do_not_close_material_claim(change):
    p, r, claim = fixture()
    match change:
        case "missing_definition":
            r["sources"].pop(1)
        case "wrong_definition":
            r["sources"][1]["text"] = r["sources"][1]["text"].replace(
                "гранулометрического состава", "пластичности"
            )
        case "missing_link":
            r["sources"].pop(2)
        case "wrong_link_pdf":
            r["sources"][2]["source_id"] = "different-mixture-document"
        case "wrong_grade":
            r["sources"][3]["text"] = r["sources"][3]["text"].replace("М800", "М8000")
        case "wrong_norm":
            r["sources"][3]["label"] = "ГОСТ 99999-2025 · 5.6"
        case "unreviewed_norm":
            r["sources"][1]["origin"] = "ocr_unreviewed_inline_raster"
        case "unreviewed_project":
            r["sources"][0]["origin"] = "ocr_unreviewed"
        case "wrong_fraction":
            p.work_name = p.work_name.replace("0/63", "0/31,5")
        case "wrong_comment":
            p.comment = p.comment.replace("марка М1", "марка М2")
        case "unresolved_scope":
            r["parameter_proposals"][0]["scope_state"] = "unresolved"
        case "unknown_origin":
            r["sources"][0]["origin"] = ""
        case "wrong_scope":
            r["parameter_proposals"][0]["scope_quote"] += " Не из документа"
        case suffix:
            extra = {
                "different_strength_constraint": " М1000",
                "unwritten_strength_limit": " прочность не ниже 1000",
                "plasticity_constraint": " Пл2",
                "ambiguous_mark": " марке М2",
                "ambiguous_category": " категории К85",
            }[suffix]
            r["sources"][0]["text"] += extra
            r["parameter_proposals"][0]["scope_quote"] += extra
    out = apply_specification_compatibility(p, r)
    assert (
        next(i for i in out["items"] if claim in i["claim_ids"])["state"]
        == "needs_facts"
    )
    assert specification_proof(p, r) is None


def test_source_removal_revokes_earlier_projection_and_keeps_original_answer():
    p, r, claim = fixture()
    out = apply_specification_compatibility(p, r)
    out["sources"] = [s for s in out["sources"] if s["id"] != "ref"]
    reverted = apply_specification_compatibility(p, out)
    item = next(i for i in reverted["items"] if claim in i["claim_ids"])
    assert item == r["items"][0]
    assert "Не доказана связь" in reverted["summary"]


def test_compound_claim_is_partitioned_reversibly_without_erasing_other_obligation():
    p, r, claim = fixture()
    r["items"][0]["claim_ids"] = [claim, "attribute:other"]
    out = apply_specification_compatibility(p, r)
    assert (
        next(i for i in out["items"] if i["claim_ids"] == ["attribute:other"])["state"]
        == "needs_facts"
    )
    assert (
        next(i for i in out["items"] if i["claim_ids"] == [claim])["state"]
        == "confirmed"
    )
    assert apply_specification_compatibility(p, out)["items"] == out["items"]


def test_existing_contradiction_and_corrupt_projection_are_not_promoted():
    p, r, claim = fixture()
    r["items"][0]["state"] = "possible_mismatch"
    assert (
        apply_specification_compatibility(p, r)["items"][0]["state"]
        == "possible_mismatch"
    )
    p, r, claim = fixture()
    out = apply_specification_compatibility(p, r)
    out["evidence_trace"][-1]["original_sha256"] = "wrong"
    assert (
        next(
            i
            for i in apply_specification_compatibility(p, out)["items"]
            if i["topic"] == TOPIC
        )["state"]
        == "needs_facts"
    )


def test_unknown_or_negated_grade_does_not_turn_into_positive_specification():
    p, r, _ = fixture()
    p.work_name = p.work_name.replace("М800", "кроме М800")
    assert specification_proof(p, r) is None
    p, r, _ = fixture("М777")
    assert specification_proof(p, r) is None


def test_aggregate_grade_alone_cannot_supply_missing_mixture_designations():
    p, r, claim = fixture()
    p.comment = "ГОСТ Р 70458-2022\nКупл-1,26"
    assert specification_proof(p, r) is None
    assert (
        next(
            i
            for i in apply_specification_compatibility(p, r)["items"]
            if claim in i["claim_ids"]
        )["state"]
        == "needs_facts"
    )
