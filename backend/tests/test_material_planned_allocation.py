"""A conserved plan is neither individual PD geometry nor gross consumption."""

from copy import deepcopy
from decimal import Decimal

import pytest

from audit_models import PositionSource, SourceHeading, VorPosition
from normative_layer_assembly import (
    material_allocation_proof,
    verified_material_allocation,
)


def fixture(upper=False):
    role, fraction, height, bases = (
        ("верхнего", "0/63", 10, ("91.17", "97.57"))
        if upper
        else ("нижнего", "0/31,5", 11, ("105.84", "107.67"))
    )
    positions = {}
    for stage, base in enumerate(bases, 1):
        w = (stage - 1) * 2
        src = PositionSource(
            document_sha256="a" * 64,
            format="xlsx",
            sheet="S",
            row=10 + w,
            header_row=1,
            position_id=str(w),
            sections=[dict(address="A2", text="Съезды")],
        )
        positions[w] = VorPosition(
            index=w,
            position_type="Работа",
            unit="м2",
            volume="90",
            position_source=src,
            work_name=f"Устройство {role} слоя основания из ЩПС h={height} см "
            f"(двухслойного основания - слой{stage})",
        )
        src = src.model_copy(deep=True)
        src.row += 1
        src.position_id = str(w + 1)
        product = str(Decimal(base) * Decimal("1.26"))
        positions[w + 1] = VorPosition(
            index=w + 1,
            position_type="Материал",
            unit="м3",
            volume=product,
            position_source=src,
            work_name=f"Щебеночно-песчаная смесь М800 типа {fraction} мм",
            comment="ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26",
            formula=f"({base})*1,26",
            calculation=dict(
                expression=f"=({base})*1.26",
                status="calculated",
                value=product,
                cached_mismatch=False,
            ),
        )
    total = str(sum(map(Decimal, bases)))
    construction = (
        f"{'Верхний' if upper else 'Нижний'} слой основания — "
        f"Щебеночно-песчаная смесь фр. {fraction} по ГОСТ Р 70458-2022, К90, М1, h={height * 2} см"
    )
    volume = f"Съезды | Щебеночно-песчаная смесь типа {fraction} мм М800 | h={height * 2} см | Объем, м3 | Всего: {total}"
    sources = [
        dict(
            id="construction",
            kind="project",
            origin="native",
            version_id="v1",
            page=50,
            text="Тип III — Конструкция съездов\n" + construction,
        ),
        dict(
            id="total",
            kind="project",
            origin="ocr_verified_header",
            version_id="v1",
            page=90,
            text=volume,
            numeric_verified=True,
            unit_verified=True,
            unit="м3",
            verified_parameters=[dict(parameter="quantity", value=total, unit="м3")],
        ),
    ]
    proposals = [
        dict(
            parameter="quantity",
            value=total,
            unit="м3",
            source_id="total",
            quote=volume,
            scope_quote=volume,
            scope_state="verified",
        ),
        dict(
            parameter="thickness",
            value=str(height * 2),
            unit="см",
            source_id="construction",
            quote=f"h={height * 2} см",
            scope_quote=construction,
            scope_state="verified",
        ),
    ]
    return positions, dict(sources=sources, parameter_proposals=proposals, items=[]), {}


def proof(data):
    positions, result, relation = data
    return material_allocation_proof(positions[1], result, positions, relation)


@pytest.mark.parametrize("upper,total", [(False, "213.51"), (True, "188.74")])
def test_planned_group_conserves_full_project_resource_without_certifying_parts(
    upper, total
):
    data = fixture(upper)
    original = deepcopy(data)
    found = proof(data)
    assert found and found["project_volume"] == total
    assert found["declared_volume_sum"] == total
    assert found["material_indices"] == [1, 3]
    for flag in (
        "individual_project_volumes_verified",
        "stage_areas_verified",
        "coefficient_applicability_verified",
        "normative_rate_verified",
        "quantity_shared",
    ):
        assert found[flag] is False
    assert data == original
    positions, result, _ = data
    result["evidence_trace"] = [found]
    assert verified_material_allocation(result, positions[1])
    assert verified_material_allocation(result)
    assert not verified_material_allocation(result, positions[3])


@pytest.mark.parametrize(
    "change",
    [
        "other_site",
        "different_spec",
        "different_coefficient",
        "missing_stage",
        "missing_material",
        "unbalanced",
        "gross",
        "project_part_constraint",
        "unit",
        "unverified_header",
        "unverified_number",
        "duplicate_material",
        "source_version",
        "wrong_pd_thickness",
        "ambiguous_total",
        "ambiguous_construction",
        "raw_formula",
        "raw_calculation",
        "fake_cache",
        "same_wrong_standard",
        "same_wrong_grade",
        "unknown_source",
        "unlocated_row",
        "same_extra_property",
        "raw_source_cell",
        "same_material_other_section",
        "duplicate_stage",
    ],
)
def test_invalid_or_incomplete_group_has_no_proof(change):
    data = fixture()
    positions, result, _ = data
    material = positions[3]
    if change == "other_site":
        result["sources"][0]["text"] = result["sources"][0]["text"].replace(
            "съездов", "остановок"
        )
    if change == "different_spec":
        material.comment = material.comment.replace("К90", "К80")
    if change == "different_coefficient":
        material.comment = material.comment.replace("1,26", "1,30")
        material.formula = "107.67*1.30"
        material.calculation.update(expression=material.formula, value="139.971")
        material.volume = "139.971"
    if change == "missing_stage":
        del positions[2]
    if change == "missing_material":
        del positions[3]
    if change == "unbalanced":
        material.formula = "108*1.26"
        material.calculation.update(expression=material.formula, value="136.08")
        material.volume = "136.08"
    if change == "gross":
        result["items"] = [
            dict(topic="Gross расход материала", explanation="С учетом всех потерь")
        ]
    if change == "project_part_constraint":
        result["sources"].append(
            dict(
                id="part",
                kind="project",
                origin="native",
                version_id="v1",
                page=90,
                text="Съезды. Щебеночно-песчаная смесь типа 0/31,5. Слой 1: объем 100 м3",
            )
        )
    if change == "unit":
        material.unit = "т"
    if change == "unverified_header":
        result["sources"][1]["origin"] = "ocr"
    if change == "unverified_number":
        result["sources"][1]["numeric_verified"] = False
    if change == "duplicate_material":
        positions[4] = material.model_copy(deep=True)
        positions[4].index = 4
        positions[4].position_source.row += 1
    if change == "source_version":
        result["sources"][1]["version_id"] = "another-project"
    if change == "wrong_pd_thickness":
        result["sources"][0]["text"] = result["sources"][0]["text"].replace(
            "22 см", "24 см"
        )
        result["parameter_proposals"][1].update(
            value="24",
            quote="h=24 см",
            scope_quote=result["parameter_proposals"][1]["scope_quote"].replace(
                "22 см", "24 см"
            ),
        )
    if change in {"ambiguous_total", "ambiguous_construction"}:
        which = 1 if change == "ambiguous_total" else 0
        source = deepcopy(result["sources"][which])
        source["id"] += "2"
        result["sources"].append(source)
        proposal = deepcopy(result["parameter_proposals"][1 - which])
        proposal["source_id"] = source["id"]
        result["parameter_proposals"].append(proposal)
    if change == "raw_formula":
        material.formula = "107.67*1.26+0"
    if change == "raw_calculation":
        material.calculation["expression"] = "107.67*1.25"
    if change == "fake_cache":
        material.calculation["value"] = "123"
    if change == "same_wrong_standard":
        for i in [1, 3]:
            positions[i].comment = positions[i].comment.replace("2022", "2024")
    if change == "same_wrong_grade":
        for i in [1, 3]:
            positions[i].work_name = positions[i].work_name.replace("М800", "М600")
    if change == "unknown_source":
        result["parameter_proposals"][0]["source_id"] = "absent"
    if change == "unlocated_row":
        positions[4] = material.model_copy(deep=True)
        positions[4].index = 4
        positions[4].position_source = None
    if change == "same_extra_property":
        for i in [1, 3]:
            positions[i].work_name += " F200"
    if change == "raw_source_cell":
        material.position_source.cells = {"E13": "999*1.26"}
    if change == "same_material_other_section":
        material.position_source.sections = [
            SourceHeading(address="A3", text="Остановки")
        ]
    if change == "duplicate_stage":
        positions[2].work_name = positions[0].work_name
    assert proof(data) is None


def test_balanced_redistribution_is_only_a_plan_and_not_individual_geometry():
    data = fixture()
    for i, base in [(1, "100"), (3, "113.51")]:
        row = data[0][i]
        row.formula = f"{base}*1.26"
        row.volume = str(Decimal(base) * Decimal("1.26"))
        row.calculation.update(expression=row.formula, value=row.volume)
    found = proof(data)
    assert found and not found["individual_project_volumes_verified"]


@pytest.mark.parametrize("change", ["source", "proposal", "formula", "proof"])
def test_stored_event_must_rebuild_from_current_literals(change):
    positions, result, relation = fixture()
    event = material_allocation_proof(positions[1], result, positions, relation)
    result["evidence_trace"] = [event]
    if change == "source":
        result["sources"][1]["text"] += " changed"
    if change == "proposal":
        result["parameter_proposals"][0]["value"] = "999"
    if change == "formula":
        positions[1].formula = "100*1.26"
    if change == "proof":
        event["individual_project_volumes_verified"] = True
    assert not verified_material_allocation(result, positions[1])


def test_preexisting_green_flags_are_neither_needed_nor_sufficient():
    positions, result, relation = fixture()
    positions[1].calculation["status"] = "not_checked"
    assert material_allocation_proof(positions[1], result, positions, relation)
    result.update(
        material_compatibility_verified=True,
        complete_total=True,
        parameter_cards=[dict(id="quantity", state="confirmed")],
    )
    result["sources"][1]["numeric_verified"] = False
    assert material_allocation_proof(positions[1], result, positions, relation) is None


def table_fixture():
    """Synthetic physical table projection: no invented exit caption."""
    import hashlib
    from analysis_replay import digest

    positions, result, relation = fixture()
    source = result["sources"][1]
    table, cell_id = "table-test", "table-test:164"
    source_id = (
        "agent:p90:cell:"
        + hashlib.sha256(f"v1:90:{table}:{cell_id}".encode()).hexdigest()[:24]
    )
    headers = [
        dict(
            cell_id=f"{table}:{i}",
            row=r,
            column=7,
            colspan=1,
            text=text,
            bbox=[7, r, 8, r + 1],
        )
        for i, r, text in [
            (8, 2, "Щебеночно-песчаная смесь типа 0/31,5 мм М800"),
            (14, 3, "h=0,22м"),
            (25, 4, "Объем, м3"),
        ]
    ]
    labels = [
        dict(
            cell_id=f"{table}:label{i}",
            row=5 + i,
            column=0,
            text=text,
            bbox=[0, 5 + i, 1, 6 + i],
        )
        for i, text in enumerate(
            ["Съезд №1", "ул. Пролетарская", *[f"Съезд №{i}" for i in range(2, 9)]]
        )
    ]
    total = dict(
        cell_id=cell_id,
        row=14,
        column=7,
        text="213.51",
        bbox=[7, 14, 8, 15],
        header_ids=[h["cell_id"] for h in headers],
    )
    source.update(
        id=source_id,
        origin="ocr",
        text=" | ".join(h["text"] for h in headers)
        + "\nЕдиница физического столбца: м3\nВсего:\n213.51",
        table_scope_context=dict(
            version="independent_visual_row_labels_v1",
            source_binding=dict(
                source_id=source_id,
                version_id="v1",
                page=90,
                table_id=table,
                cell_id=cell_id,
                row=14,
                column=7,
                header_ids=total["header_ids"],
            ),
            body_first_row=5,
            total_row=14,
            row_labels=labels,
            header_cells=headers,
            total_cell=total,
            caption="Ведомость работ по устройству дорожной одежды",
            provenance=dict(
                table_sha256="a" * 64,
                actual_cells_sha256="b" * 64,
                header_cells_sha256=digest(headers),
                row_labels_sha256=digest(labels),
                document_sha256="c" * 64,
                extraction_sha256="0" * 64,
                render_sha256="d" * 64,
                review_sha256=["e" * 64, "f" * 64],
                origin="ocr",
                quality="usable",
            ),
        ),
    )
    result["parameter_proposals"][0].update(
        source_id=source_id,
        quote="Единица физического столбца: м3\nВсего:\n213.51",
        scope_quote=source["text"],
    )
    return positions, result, relation


def test_complete_physical_row_labels_scope_total_without_inventing_caption():
    data = table_fixture()
    found = proof(data)
    assert found and "Съезды" not in found["project_basis"]["volume"]["scope_quote"]
    assert found["table_scope_context"]["row_labels"][1]["text"] == "ул. Пролетарская"
    assert found["source_document_sha256"] == "c" * 64
    data[1]["evidence_trace"] = [found]
    assert verified_material_allocation(data[1], data[0][1])


@pytest.mark.parametrize(
    "change",
    [
        "missing_body_row",
        "header_swap",
        "wrong_table",
        "wrong_source",
        "wrong_site_labels",
        "wrong_total_literal",
        "unverified_numeric",
    ],
)
def test_physical_table_projection_cannot_borrow_other_body_or_header(change):
    from analysis_replay import digest

    data = table_fixture()
    source = data[1]["sources"][1]
    context = source["table_scope_context"]
    if change == "missing_body_row":
        context["row_labels"].pop()
    if change == "header_swap":
        context["header_cells"][0]["text"] = "ЩПС типа 0/63"
        context["provenance"]["header_cells_sha256"] = digest(context["header_cells"])
    if change == "wrong_table":
        context["source_binding"]["table_id"] = "other"
    if change == "wrong_source":
        context["source_binding"]["source_id"] = "other"
    if change == "wrong_site_labels":
        for row in context["row_labels"]:
            row["text"] = "Автобусная остановка"
        context["provenance"]["row_labels_sha256"] = digest(context["row_labels"])
    if change == "wrong_total_literal":
        context["total_cell"]["text"] = "999"
    if change == "unverified_numeric":
        source["numeric_verified"] = False
    assert proof(data) is None
