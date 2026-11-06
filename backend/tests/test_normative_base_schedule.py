from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from audit_models import PositionSource, VorPosition
from normative_agent import EvidenceTools
from normative_parameters import build_cards
from normative_schedule_quantity import prepare, project


def fixture():
    positions = {}
    for index in (0, 1):
        positions[index] = VorPosition(
            index=index,
            number=str(index + 1),
            volume="30",
            unit="м2",
            position_type="Работа",
            work_name=f"Устройство нижнего слоя основания из ЩПС, h=11 см (двухслойного основания - слой{index + 1})",
            position_source=PositionSource(
                document_sha256="a" * 64,
                format="xlsx",
                sheet="S",
                row=10 + index,
                header_row=2,
                position_id=str(index),
                sections=[dict(text="Улица", address="A3")],
            ),
        )
    cells = []

    def header(sid, row, col, span, text):
        cells.append(
            dict(
                cell_id=sid, row=row, column=col, colspan=span, text=text, role="header"
            )
        )

    for col, side in ((2, "Справа"), (4, "Слева")):
        header(f"b{col}", 0, col, 2, "Основание")
        header(f"s{col}", 1, col, 2, side)
        header(f"w{col}", 2, col, 2, "Ровик уширения")
        header(f"m{col}", 3, col, 2, "Щебеночно-песчаная смесь фр. 0/31,5")
        header(f"h{col}", 4, col, 2, "h=0,22m" if col == 2 else "")
        header(f"u{col}", 5, col, 1, "Площадь, м2")
        cells.append(
            dict(
                cell_id=f"v{col}",
                row=8,
                column=col,
                text="19" if col == 2 else "11",
                role="cell",
                origin="ocr",
                numeric_verified=True,
                quality="usable",
                bbox=[col / 10, 0.8, (col + 1) / 10, 0.9],
                header_ids=[f"{p}{col}" for p in ("b", "s", "w", "m", "h", "u")],
            )
        )
    cells.append(dict(cell_id="total", row=8, column=0, role="cell", text="Всего:"))
    table = dict(
        table_id="synthetic",
        cells=cells,
        row_count=9,
        col_count=6,
        column_units={"2": dict(unit="м2"), "4": dict(unit="м2")},
    )
    tools = EvidenceTools.__new__(EvidenceTools)
    for name in ("sources", "events", "quantity_sums", "preferred", "snapshot_refs"):
        setattr(tools, name, [])
    tools.cells = {}
    tools.coverage = {}
    tools.version = "synthetic-pdf"
    tools.filename = "synthetic.pdf"
    tools.index = SimpleNamespace(tables=[[table]], texts=[""], metadata={})
    native = dict(
        id="native",
        kind="project",
        origin="native",
        version_id=tools.version,
        page=2,
        text="Тип IV – Конструкция ровиков уширения:\nНижний слой основания — Щебеночно-песчаная смесь фр. 0/31,5, h=22 см;",
    )
    result = dict(
        pipeline_version=4,
        execution_state="complete",
        sources=[native],
        items=[],
        evidence_trace=[],
        parameter_proposals=[],
        related_positions=[
            dict(work=positions[1].work_name, quantity="30", unit="м2", row=11)
        ],
    )
    return positions, result, tools, ["Щебеночно-песчаная смесь типа 0/31,5"]


def test_complete_pair_and_blank_depth_are_bound_without_model():
    positions, result, tools, materials = fixture()
    assert project(positions[0], result, tools, materials, positions=positions)
    q = next(
        c
        for c in build_cards(positions[0], result, positions, {})
        if c["id"] == "quantity"
    )
    assert q["state"] == "confirmed" and q["found_value"] == "30"
    assert len(q["source_anchors"][0]["rects"]) == 0  # No invented page transform.
    assert result["evidence_trace"][-1]["model_requests"] == 0
    json.dumps(result)  # Database persistence must not contain Decimal objects.
    assert not project(positions[0], result, tools, materials, positions=positions)


def test_empty_model_source_does_not_veto_independent_schedule_proof():
    positions, result, tools, materials = fixture()
    result['parameter_proposals'] = [
        dict(parameter=parameter, scope_state='verified', source_id='', value='500', unit=unit)
        for parameter, unit in [('quantity', 'м2'), ('thickness', 'см')]
    ]
    assert project(positions[0], result, tools, materials, positions=positions)
    cards = {c['id']: c for c in build_cards(positions[0], result, positions, {})}
    assert cards['quantity']['found_value'] == '30'
    assert cards['thickness']['found_value'] == '22'


def test_existing_direct_parts_are_replaced_by_their_proven_whole():
    positions, result, tools, materials = fixture()
    assert project(positions[0], result, tools, materials, positions=positions)
    from normative_quantity_anchors import quantity_parameter_candidates

    parts = [
        p["proposal"]
        for p in quantity_parameter_candidates(tools.sources, tools.cells)
        if p["proposal"]["operation"] == "direct"
    ]
    assert len(parts) == 2
    for part in parts:
        part["scope_state"] = "verified"
    result["parameter_proposals"] = parts
    assert project(positions[0], result, tools, materials, positions=positions)
    quantities = [
        p for p in result["parameter_proposals"] if p["parameter"] == "quantity"
    ]
    assert len(quantities) == 1
    assert quantities[0]["operation"] == "sum"
    thickness = next(
        c
        for c in build_cards(positions[0], result, positions, {})
        if c["id"] == "thickness"
    )
    assert thickness["state"] == "confirmed" and thickness["found_value"] == "22"


@pytest.mark.parametrize("declared", ["30", "999"])
def test_preparation_reads_saved_native_description_before_model(declared):
    positions, result, tools, materials = fixture()
    tools.index.texts = [result["sources"][0]["text"]]
    tools.index.quality = {0: dict(origin="native")}
    # A single layer can identify its material directly without child rows.
    for p in positions.values():
        p.work_name = p.work_name.replace("из ЩПС", "из ЩПС типа 0/31,5")
        p.volume = declared
    result["related_positions"][0]["work"] = positions[1].work_name
    result["related_positions"][0]["quantity"] = declared
    proof = prepare(positions[0], positions, tools, result["related_positions"])
    assert proof and proof["quantity_candidates"][0]["proposal"]["value"] == "30"
    assert proof["state"] == "candidate" and proof["model_decision_required"] is True
    assert proof["quantity_candidates"][0]["proposal"]["scope_state"] == "unresolved"
    assert not any(
        e.get("kind") == "physical_schedule_layer_quantity_v2" for e in tools.events
    )
    assert {r["tool"] for r in proof["read_requests"]} == {"read_source", "read_table"}


def test_parent_sum_supersedes_only_the_childs_exact_direct_parts():
    positions, basis, tools, materials = fixture()
    assert project(positions[0], basis, tools, materials, positions=positions)
    basis["parameter_cards"] = build_cards(positions[0], basis, positions, {})
    from normative_quantity_anchors import quantity_parameter_candidates

    parts = [
        p["proposal"]
        for p in quantity_parameter_candidates(tools.sources, tools.cells)
        if p["proposal"]["operation"] == "direct"
    ]
    for part in parts:
        part["scope_state"] = "verified"
    child = positions[0].model_copy(
        update=dict(
            index=3,
            position_type="Материал",
            unit="м3",
            work_name="Щебеночно-песчаная смесь типа 0/31,5",
            volume="4",
        ),
        deep=True,
    )
    child.position_source.row = 13
    child.position_source.position_id = "material"
    positions[3] = child
    result = {
        **deepcopy(basis),
        "parameter_proposals": parts,
        "items": [],
        "evidence_trace": [],
    }
    from normative_parent_basis import _reuse_geometry

    reused = _reuse_geometry(
        result, positions[0], basis, positions, {}, {"id": "donor"}, child=child
    )
    quantities = [
        p for p in reused["parameter_proposals"] if p["parameter"] == "quantity"
    ]
    assert len(quantities) == 1 and quantities[0]["operation"] == "sum"
    card = next(
        c
        for c in build_cards(child, reused, positions, {"parent": {"index": 0}})
        if c["id"] == "quantity"
    )
    assert card["state"] == "confirmed"

    from normative_parent_basis import _clear_geometry

    restored = _clear_geometry(reused, positions[0].index)
    assert restored["parameter_proposals"] == parts
    assert not restored["evidence_trace"]
    assert _clear_geometry(restored, positions[0].index) == restored
    rebound = _reuse_geometry(
        restored, positions[0], basis, positions, {}, {"id": "donor"}, child=child
    )
    assert rebound == reused


@pytest.mark.parametrize(
    "declared,actual,accepted",
    [
        ("Съезд №1", "Съезд №2", False),
        ("Съезд №1", "Съезд №10", False),
        ("Съезд №1", "Съезды", False),
        ("Съезд №1", "Съезд №1", True),
        ("Съезды", "Съезд №2", True),
        ("Остановка «Мост»", "Остановка «Завод»", False),
        ("Остановка «Мост»", "Остановка «Мост»", True),
    ],
)
def test_native_construction_requires_the_declared_subsite(declared, actual, accepted):
    from normative_base_schedule import identity, native_construction

    positions, result, tools, materials = fixture()
    for position in positions.values():
        position.position_source.sections[0].text = declared
    source = result["sources"][0]
    source["text"] = source["text"].replace("ровиков уширения", actual)
    key = identity(positions[0], materials, positions)
    assert (native_construction(result, key, tools.version) is not None) is accepted


@pytest.mark.parametrize(
    "title,accepted", [("Съезд №1", True), ("Съезд №2", False), ("Съезды", False)]
)
def test_numbered_subsite_requires_the_same_schedule(title, accepted):
    from normative_base_schedule import identity, select

    positions, _, tools, materials = fixture()
    for position in positions.values():
        position.position_source.sections[0].text = "Съезд №1"
    key = identity(positions[0], materials, positions)
    table = tools.index.tables[0][0]
    for cell in table["cells"]:
        if cell["cell_id"].startswith("w"):
            cell["text"] = title
    native = dict(scope="exits", height_value="22", height_unit="см")
    assert (select([(1, table)], key, native) is not None) is accepted


def test_multi_layer_page_excerpt_does_not_compete_with_exact_layer_sentence():
    positions, result, tools, materials = fixture()
    assert project(positions[0], result, tools, materials, positions=positions)
    text = "Нижний слой основания h=22 см. Средний слой основания h=22 см."
    result["sources"].append(
        dict(
            id="broad",
            kind="project",
            origin="native",
            version_id=tools.version,
            page=2,
            text=text,
        )
    )
    result["parameter_proposals"].append(
        dict(
            parameter="thickness",
            operation="direct",
            value="22",
            unit="см",
            source_id="broad",
            quote="h=22 см",
            scope_quote=text,
            scope_state="verified",
        )
    )
    card = next(
        c
        for c in build_cards(positions[0], result, positions, {})
        if c["id"] == "thickness"
    )
    assert card["state"] == "confirmed" and card["source_ids"] == ["native"]


def test_wrong_expected_total_does_not_change_selected_operands():
    positions, result, tools, materials = fixture()
    positions[0].volume = positions[1].volume = "999"
    result["related_positions"][0]["quantity"] = "999"
    assert project(positions[0], result, tools, materials, positions=positions)
    event = result["evidence_trace"][-1]
    assert event["kind"] == "located_schedule_area_v1" and event["value"] == "30"
    found = {p["parameter"]: (p["value"], p["scope_state"]) for p in result["parameter_proposals"]}
    assert found == {"quantity": ("30", "unresolved"), "thickness": ("22", "unresolved")}


def test_other_construction_cannot_block_a_verified_schedule():
    positions, result, tools, materials = fixture()
    quote = "Тротуар: основание из ЩПС фр. 0/31,5, площадь 70 м2"
    source = dict(id="other", kind="project", origin="native", page=3,
                  version_id=tools.version, text=quote)
    result["sources"].append(source)
    tools.sources.append(source)
    result["parameter_proposals"].append(dict(
        parameter="quantity", operation="direct", value="70", unit="м2",
        source_id="other", scope_quote=quote, quote="площадь 70 м2",
        scope_state="verified", operand_source_ids=[]))
    assert project(positions[0], result, tools, materials, positions=positions)
    proposals = {p["value"]: p["scope_state"] for p in result["parameter_proposals"] if p["parameter"] == "quantity"}
    assert proposals == {"30": "verified", "70": "unresolved"}


def test_preparation_selects_documented_depth_independently_of_declared_split():
    positions, result, tools, _ = fixture()
    tools.index.texts = [result["sources"][0]["text"]]
    tools.index.quality = {0: dict(origin="native")}
    for p in positions.values():
        p.work_name = p.work_name.replace("из ЩПС", "из ЩПС типа 0/31,5")
    positions[0].work_name = positions[0].work_name.replace("11 см", "12 см")
    from normative_layer_assembly import layer_assembly
    assert layer_assembly(positions[0], positions)["height"] == 23
    assert layer_assembly(positions[1], positions)["first"] == 12
    prepared = prepare(positions[0], positions, tools, result["related_positions"])
    assert prepared and prepared["construction_candidate"]["height_value"] == "22"
    assert prepared["quantity_candidates"][0]["proposal"]["scope_state"] == "unresolved"


@pytest.mark.parametrize(
    "change",
    [
        "missing_pair",
        "wrong_depth",
        "unverified_number",
        "wrong_side",
        "different_fraction",
        "ambiguous_table",
        "ambiguous_native",
        "other_version",
        "other_section",
        "missing_layer",
    ],
)
def test_incomplete_or_ambiguous_proof_never_confirms(change):
    positions, result, tools, materials = fixture()
    table = tools.index.tables[0][0]
    by_id = {c["cell_id"]: c for c in table["cells"]}
    if change == "missing_pair":
        table["cells"].remove(by_id["v4"])
    elif change == "wrong_depth":
        by_id["h4"]["text"] = "h=0,30m"
    elif change == "unverified_number":
        by_id["v4"]["numeric_verified"] = False
    elif change == "wrong_side":
        by_id["s4"]["text"] = "Справа"
    elif change == "different_fraction":
        by_id["m4"]["text"] = "Щебеночно-песчаная смесь фр. 0/63"
    elif change == "ambiguous_table":
        tools.index.tables.append(deepcopy(tools.index.tables[0]))
        tools.index.texts.append("")
    elif change == "ambiguous_native":
        result["sources"].append(
            {
                **result["sources"][0],
                "id": "other",
                "text": result["sources"][0]["text"].replace("Тип IV", "Тип VI"),
            }
        )
    elif change == "other_version":
        result["sources"][0]["version_id"] = "other"
    elif change == "other_section":
        positions[0].position_source.sections[0].text = "Остановки"
    else:
        positions.pop(1)
    before = deepcopy(result)
    assert not project(positions[0], result, tools, materials, positions=positions)
    assert result == before
