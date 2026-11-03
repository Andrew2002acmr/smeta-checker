from copy import deepcopy

import pytest

from audit_models import VorPosition, PositionSource
from normative_local_checks import layer_group
from normative_layer_assembly import layer_assembly


def pair():
    source = PositionSource(
        document_sha256="a" * 64,
        format="xlsx",
        sheet="S",
        row=18,
        header_row=1,
        position_id="a",
        sections=[
            dict(address="A10", text="Автомобильная дорога на очистные сооружения")
        ],
    )
    first = VorPosition(
        index=0,
        position_type="Работа",
        unit="м2",
        volume="123.4",
        position_source=source,
        work_name="Устройство слоя основания из ЩПС, h = 11,5см\n(слой1) с уплотнением",
    )
    second = first.model_copy(deep=True)
    second.index = 2
    second.position_source.row = 20
    second.position_source.position_id = "b"
    second.work_name = first.work_name.replace("слой1", "слой2")
    return first, second


def test_explicit_numbered_operations_form_an_assembly_without_template_role_words():
    first, second = pair()
    positions = {0: first, 2: second}
    assert layer_assembly(first, positions)["height"] == 23
    group = layer_group(first, [], positions)
    assert group["height"] == 23 and group["quantity_shared"] is False
    assert (
        layer_group(
            first,
            [dict(work=second.work_name, quantity=second.volume, unit=second.unit)],
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    ["section", "document", "sheet", "unrelated_work", "unlabelled", "unequal_area"],
)
def test_generic_stage_pair_cannot_cross_scope_or_unrelated_work(mutation):
    first, second = pair()
    positions = {0: first, 2: second}
    if mutation == "section":
        second.position_source.sections = []
    if mutation == "document":
        second.position_source.document_sha256 = "b" * 64
    if mutation == "sheet":
        second.position_source.sheet = "Other"
    if mutation == "unlabelled":
        second.work_name = second.work_name.replace("(слой2)", "")
    if mutation == "unequal_area":
        second.volume = "234.5"
    if mutation == "unrelated_work":
        other = deepcopy(first)
        other.index = 1
        other.position_source.row = 19
        other.work_name = "Другая работа"
        positions[1] = other
    assert layer_group(first, [], positions) is None
