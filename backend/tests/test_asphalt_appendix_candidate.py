"""Reviewed primary literals + synthetic project geometry yield a candidate, never C."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from analysis_replay import digest
from audit_models import PositionSource, VorPosition
from normative_parameters import enrich
from normative_parent_basis import _reuse_geometry, verified_geometry_dependency
from normative_resource_consumption import appendix_rate_candidate
from normative_workbook_rate import PROOF, geometric_rate_replay_supported


def fixture(
    material="ЩМА-16", height="5", band="2,7–2,9", base="98.64", step="12.33", index=13
):
    """Normative text is frozen primary; PD here is explicitly a unit fixture."""
    data = json.loads(
        (Path(__file__).parent / "fixtures/collection-road102-pair.json").read_text(
            encoding="utf-8"
        )
    )
    sources = []
    for clause in data["clauses"]:
        sources.append(
            dict(
                id="app" if clause["label"] == "27.1" else "new35",
                kind="normative",
                source_id=clause["source_id"],
                source_sha256=data["source"]["sha256"],
                page=clause["citations"][0]["physical_page"],
                clause_id=clause["id"],
                origin=clause["status"],
                label=clause["designation"] + " · " + clause["label"],
                text=clause["text"],
            )
        )
    sources.append(
        dict(
            id="base",
            kind="normative",
            source_id="pdf-base",
            page=58,
            origin="native",
            label="ГЭСН 27 · 27-06-031-01",
            text=(
                "Таблица ГЭСН 27-06-031 Устройство покрытия из горячих асфальтобетонных смесей\n"
                "Измеритель: 1000 м2\n27-06-031-01 третьего типоразмера, ширина укладки до 6 м, толщина слоя 4 см\n"
                "Состав работ:\n01. Укладка смеси с обрубкой краев."
            ),
        )
    )
    src = PositionSource(
        document_sha256="0" * 64,
        format="xlsx",
        sheet="VOR",
        row=10,
        header_row=1,
        position_id="parent",
    )
    parent = VorPosition(
        index=index - 1,
        work_name=f"Устройство верхнего слоя покрытия, h={height} см",
        volume="106",
        unit="м2",
        position_type="Работа",
        position_source=src,
    )
    steps = (Decimal(height) - 4) / Decimal(".5")
    rate = Decimal(base) + Decimal(step) * steps
    sign = "+" if steps >= 0 else "-"
    child = VorPosition(
        index=index,
        work_name=material,
        position_type="Материал",
        unit="т",
        volume=str(Decimal("106") / 1000 * rate),
        formula=f"106/1000*{rate}",
        comment=f"Расход смеси на 1000м2 прил. 27.1 (Γ {band}т/м3) {base}{sign}({step}*{abs(steps)})={rate}т",
        position_source=src.model_copy(update={"row": 11, "position_id": "child"}),
    )
    scope = f"Верхний слой покрытия {material}; площадь 106 м2, h={height} см."
    project = dict(
        id="p",
        kind="project",
        version_id="synthetic-pd",
        page=1,
        origin="native",
        label="Synthetic PD",
        text=scope,
    )
    geometry = dict(
        pipeline_version=4,
        execution_state="complete",
        state="needs_facts",
        sources=[project],
        evidence_trace=[],
        related_positions=[],
        items=[
            dict(
                topic="Geometry",
                state="confirmed",
                required=True,
                claim_ids=["quantity", "geometry:thickness"],
                project_source_ids=["p"],
                normative_source_ids=[],
                explanation="Fixture scope",
            )
        ],
        parameter_proposals=[
            dict(
                parameter=kind,
                value=value,
                unit=unit,
                source_id="p",
                operation="direct",
                scope_relation="same_layer",
                scope_state="verified",
                scope_quote=scope,
                quote=quote,
                operand_source_ids=[],
            )
            for kind, value, unit, quote in [
                ("quantity", "106", "м2", "площадь 106 м2"),
                ("thickness", height, "см", f"h={height} см"),
            ]
        ],
    )
    positions = {parent.index: parent, child.index: child}
    geometry = enrich(parent, geometry, positions, {})
    result = dict(
        pipeline_version=4,
        execution_state="complete",
        state="needs_facts",
        sources=sources,
        evidence_trace=[],
        related_positions=[],
        parameter_proposals=[],
        items=[
            dict(
                topic="Applicability",
                state="confirmed",
                required=True,
                claim_ids=["normative:applicability"],
                project_source_ids=[],
                normative_source_ids=["base", "app", "new35"],
                explanation="Model C is deliberately insufficient for scoped absence/edition/edge proof",
            ),
            dict(
                topic="Declared basis",
                state="confirmed",
                required=True,
                claim_ids=["basis:comment:consumption:1", "derived:rate"],
                project_source_ids=[],
                normative_source_ids=["app"],
                explanation="A model C cannot close unknown gates",
            ),
        ],
    )
    result = _reuse_geometry(
        result, parent, geometry, positions, {}, dict(id="parent-review"), child=child
    )
    result["items"][0]["project_source_ids"] = [
        s["id"] for s in result["sources"] if s["kind"] == "project"
    ]
    relation = dict(parent=dict(index=parent.index), children=[])
    return child, result, positions, relation


def run(args):
    work, result, positions, relation = args
    return enrich(work, result, positions, relation)


def candidate(output):
    return next(
        (e for e in output["evidence_trace"] if e.get("kind") == PROOF), {}
    ).get("appendix_rate_candidate")


def test_primary_sma_composition_binds_sources_and_geometry_without_promoting_rate():
    args = fixture()
    work = args[0]
    output = run(args)
    proof = candidate(output)
    assert proof and proof["rate"] == "123.30" and proof["signed_steps"] == 2
    assert proof["density_band"]["bounds"] == dict(
        lower="2.7", lower_operator="gt", upper="2.9", upper_operator="le"
    )
    assert set(proof["pending_condition_ids"]) == {
        "edition_chain",
        "project_consumption_priority",
        "edge_prescription",
        "density_interval_operators",
    }
    assert (
        not proof["normative_basis_verified"] and not proof["dependent_output_verified"]
    )
    lookup = {s["id"]: s for s in output["sources"]}
    assert proof["source_sha256"] == {
        sid: digest(lookup[sid]) for sid in proof["root_source_ids"]
    }
    cards = {c["id"]: c for c in output["parameter_cards"]}
    assert (
        cards["quantity"]["state"] == "confirmed"
        and cards["quantity"]["found_unit"] == "м2"
    )
    assert (
        cards["thickness"]["state"] == "confirmed"
        and cards[f"calculation:{work.index}"]["state"] == "confirmed"
    )
    assert cards[f"factor:{work.index}:123.30"]["state"] == "needs_facts"
    assert any(
        i["required"]
        and i["state"] == "needs_facts"
        and "normative:applicability" in i["claim_ids"]
        for i in output["items"]
    )
    assert geometric_rate_replay_supported(work, output)


@pytest.mark.parametrize(
    "material,height,band,base,step,expected",
    [
        ("А16Вл", "6", ">2,7≤2,9", "99.13", "12.39", "148.69"),
        ("А-5", "4.5", "≤2,7", "94.69", "11.84", "106.53"),
        ("А-32", "3.5", "до2,7", "95.83", "11.98", "83.85"),
        ("SMA-22", "5", ">2,9", "107.08", "13.38", "133.84"),
        ("ЩМА-22", "5", ">2,7≤2,9", "98.64", "12.33", "123.30"),
    ],
)
def test_different_types_bands_steps_and_subtraction_are_not_position_rules(
    material, height, band, base, step, expected
):
    args = fixture(material, height, band, base, step, index=91)
    proof = candidate(run(args))
    assert proof and proof["rate"] == expected
    assert proof["normative_basis_verified"] is False


@pytest.mark.parametrize(
    "change",
    [
        "table_cell",
        "table_header",
        "source_pdf",
        "source_hash",
        "unreviewed",
        "fake_status",
        "missing_table",
        "norm_measure",
        "fragment",
        "two_bases",
        "unselected",
        "project_text",
        "proposal",
        "dependency",
        "thickness_card",
        "wrong_operator",
        "changed_step",
        "density_lower_inclusive",
        "density_upper_exclusive",
        "mix_density",
        "different_pd_type",
    ],
)
def test_tampering_or_unbound_inputs_does_not_produce_composition(change):
    args = fixture()
    work, result, positions, relation = args
    if change == "table_cell":
        next(s for s in result["sources"] if s["id"] == "app")["text"] = next(
            s for s in result["sources"] if s["id"] == "app"
        )["text"].replace("98,64", "98,65")
    if change == "table_header":
        next(s for s in result["sources"] if s["id"] == "app")["text"] = next(
            s for s in result["sources"] if s["id"] == "app"
        )["text"].replace("Максимальная", "Средняя")
    if change == "source_pdf":
        next(s for s in result["sources"] if s["id"] == "app")["source_id"] = (
            "other-pdf"
        )
    if change == "source_hash":
        next(s for s in result["sources"] if s["id"] == "app")["source_sha256"] = (
            "f" * 64
        )
    if change == "unreviewed":
        next(s for s in result["sources"] if s["id"] == "app")["origin"] = "native"
    if change == "fake_status":
        s = next(s for s in result["sources"] if s["id"] == "app")
        s.update(
            origin="native_visually_verified",
            text="Fake table 98,64 12,33",
            numeric_verified=True,
        )
    if change == "missing_table":
        result["sources"] = [s for s in result["sources"] if s["id"] != "app"]
    if change == "norm_measure":
        next(s for s in result["sources"] if s["id"] == "base")["text"] = next(
            s for s in result["sources"] if s["id"] == "base"
        )["text"].replace("1000 м2", "100 м2")
    if change == "fragment":
        next(s for s in result["sources"] if s["id"] == "base")["text"] = (
            "Измеритель: 1000 м2"
        )
    if change == "two_bases":
        s = deepcopy(next(s for s in result["sources"] if s["id"] == "base"))
        s.update(
            id="other",
            label=s["label"].replace("031-01", "031-02"),
            text=s["text"].replace("031-01", "031-02"),
        )
        result["sources"].append(s)
        result["items"][0]["normative_source_ids"].append("other")
    if change == "unselected":
        result["items"][0]["state"] = "needs_facts"
    if change == "project_text":
        next(s for s in result["sources"] if s["kind"] == "project")["text"] += (
            " altered"
        )
    if change == "proposal":
        next(p for p in result["parameter_proposals"] if p["parameter"] == "thickness")[
            "value"
        ] = "6"
    if change == "dependency":
        next(
            e
            for e in result["evidence_trace"]
            if e.get("kind") == "parent_geometry_dependency_v1"
        )["sha256"] = "f" * 64
    if change == "different_pd_type":
        next(p for p in result["parameter_proposals"] if p["parameter"] == "thickness")[
            "scope_quote"
        ] = "Верхний слой покрытия А-16 h=5 см"
    if change == "wrong_operator":
        work.comment = work.comment.replace("+", "-")
    if change == "changed_step":
        work.comment = work.comment.replace("12.33*2", "12.34*2")
    if change == "density_lower_inclusive":
        work.comment = work.comment.replace("2,7–2,9", "≥2,7≤2,9")
    if change == "density_upper_exclusive":
        work.comment = work.comment.replace("2,7–2,9", ">2,7<2,9")
    if change == "mix_density":
        work.comment += " Плотность смеси 2,8 т/м3"
    output = run(args)
    if change == "thickness_card":
        next(c for c in output["parameter_cards"] if c["id"] == "thickness")[
            "state"
        ] = "needs_facts"
        assert appendix_rate_candidate(work, output) is None
    else:
        assert candidate(output) is None


def test_repeated_enrich_has_one_owned_finding_and_stale_proof_is_rejected():
    args = fixture()
    work, _, positions, relation = args
    first = run(args)
    second = enrich(work, first, positions, relation)
    assert candidate(first) == candidate(second)
    assert (
        len(
            [
                i
                for i in second["items"]
                if i["topic"] == "Оставшиеся условия расхода по приложению 27.1"
            ]
        )
        == 1
    )
    assert verified_geometry_dependency(second, work)
    changed = deepcopy(second)
    next(s for s in changed["sources"] if s["id"] == "app")["text"] += " altered"
    assert not geometric_rate_replay_supported(work, changed)
    del next(e for e in changed["evidence_trace"] if e.get("kind") == PROOF)[
        "appendix_rate_candidate"
    ]
    assert geometric_rate_replay_supported(
        work, changed
    )  # No positive proof remains to trust.


def test_missing_fallback_literal_and_green_metadata_do_not_close_conditions():
    args = fixture(band=">2,7≤2,9")
    args[1]["sources"] = [s for s in args[1]["sources"] if s["id"] != "new35"]
    args[1]["coverage"] = {
        "complete": True,
        "edition_verified": True,
        "edge_required": False,
    }
    for source in args[1]["sources"]:
        if source["kind"] == "normative":
            source.update(edition_status="verified", automatic_application_allowed=True)
    output = run(args)
    proof = candidate(output)
    assert proof and proof["rate"] == "123.30"
    assert set(proof["pending_condition_ids"]) == {
        "edition_chain",
        "project_consumption_priority",
        "edge_prescription",
        "fallback_clause_source",
    }
    assert any(i["state"] == "needs_facts" and i["required"] for i in output["items"])


def test_another_work_code_does_not_inherit_fallback_scope_from_table_arithmetic():
    args = fixture()
    source = next(s for s in args[1]["sources"] if s["id"] == "base")
    source.update(
        label=source["label"].replace("27-06-031", "27-07-006"),
        text=source["text"].replace("27-06-031", "27-07-006"),
    )
    proof = candidate(run(args))
    assert proof and "work_norm_scope" in proof["pending_condition_ids"]
    assert not proof["normative_applicability_inferred"]


def test_formal_parent_area_precision_stays_separate_from_rate_arithmetic():
    args = fixture()
    child, result, positions, relation = args
    parent = positions[child.index - 1]
    # Rebind the exact same independently verified physical area to a slightly
    # different declared workbook operand; no engineering rounding is invented.
    child.formula = "106.001/1000*123.30"
    child.volume = str(Decimal("106.001") / 1000 * Decimal("123.30"))
    event = next(
        e
        for e in result["evidence_trace"]
        if e.get("kind") == "parent_geometry_dependency_v1"
    )
    event["proof"]["child"] = child.model_dump(mode="json")
    event["sha256"] = digest(event["proof"])
    assert parent.volume == "106"
    output = enrich(child, result, positions, relation)
    proof = candidate(output)
    assert proof and "area_precision" in proof["pending_condition_ids"]
    assert proof["area_operand"]["card"]["found_value"] == "106"
    assert proof["area_operand"]["declared_area"] == "106.001"
    assert not proof["dependent_output_verified"]


@pytest.mark.parametrize(
    "change", ["norm_envelope", "project_envelope", "proposal_scope", "recipe"]
)
def test_full_source_and_input_fingerprints_invalidate_stale_guidance(change):
    args = fixture()
    work = args[0]
    output = run(args)
    assert candidate(output)
    if change == "norm_envelope":
        next(s for s in output["sources"] if s["id"] == "base")["page"] = 59
    if change == "project_envelope":
        next(s for s in output["sources"] if s["kind"] == "project")["version_id"] = (
            "other-pin"
        )
    if change == "proposal_scope":
        output["parameter_proposals"][0]["scope_state"] = "unresolved"
    if change == "recipe":
        work.comment = work.comment.replace("12.33*2", "12.34*2")
    assert not geometric_rate_replay_supported(work, output)


def test_explicit_range_operators_do_not_allow_other_annotation_operators():
    args = fixture(band=">2,7≤2,9")
    args[0].comment = args[0].comment.replace("(Γ ", "(x > 1 Γ ")
    assert candidate(run(args)) is None


@pytest.mark.parametrize("case", ["seed_kg", "seed_tonnes", "asphalt_kg"])
def test_other_material_appendix_reference_keeps_existing_numeric_resource_route(
    case,
):
    from test_areal_resource_consumption import fixture as areal_fixture
    from normative_resource_consumption import EVENT

    work, result = areal_fixture()
    if case == "seed_tonnes":
        work.unit = "т"
        work.volume = "0.03862"
        work.comment = "Расход 0.02т/1000м2"
        work.formula = "1931*0.02/1000"
        work.calculation = {"expression": work.formula}
    if case == "asphalt_kg":
        work.work_name = "ЩМА-16"
        event = next(e for e in result["evidence_trace"] if e.get("kind") == EVENT)
        row = event["resources"][0]
        old_name = row["name"]
        row["name"] = "ЩМА-16"
        row["quote"] = row["quote"].replace(old_name, row["name"])
        resource = next(s for s in result["sources"] if s["id"] == row["source_id"])
        resource["text"] = resource["text"].replace(old_name, row["name"])
        scope = "Покрытие ЩМА-16. Площадь 1931 м2"
        next(s for s in result["sources"] if s["id"] == "p")["text"] = scope
        result["parameter_proposals"][0]["scope_quote"] = scope
    work.comment += " ГЭСН 47 прил.27.1"
    output = enrich(work, result, {work.index: work}, {})
    proof = next(e for e in output["evidence_trace"] if e.get("kind") == PROOF)
    assert "resource_rate_basis" in proof
    assert "appendix_rate_candidate" not in proof
    assert "appendix_scope_finding" not in proof
    assert (
        next(c for c in output["parameter_cards"] if c["id"] == "quantity")["state"]
        == "confirmed"
    )
    assert geometric_rate_replay_supported(work, output)
