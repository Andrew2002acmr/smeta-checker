from copy import deepcopy

import pytest

from vor_material_links import material_links


def pair():
    source = dict(
        document_sha256="a" * 64,
        sheet="ВОР",
        header_row=15,
        sections=[dict(address="A20", text="Тротуар")],
    )
    return [
        dict(
            idx=0,
            position_type="Работа",
            w_name="Установка бортового камня",
            w_unit="м",
            w_vol="70",
            position_source={**source, "row": 21},
        ),
        dict(
            idx=1,
            position_type="Материал",
            w_name="Камень бортовой бетонный БР100.30.15",
            w_unit="м3",
            w_vol="3.15",
            position_source={**source, "row": 22},
            calculation=dict(status="calculated", value="3.15", expression="70*0.045"),
        ),
    ]


def test_curb_relation_never_proves_profile_or_rate():
    tasks = pair()
    before = deepcopy(tasks)
    link = material_links(tasks)[1]
    assert link["index"] == 0
    assert link["relation_basis"] == "explicit_workbook_owner_linear_curb"
    assert link["conversion_verified"] is False
    assert link["material_compatibility_verified"] is False
    assert "rate" not in link and "confirmed" not in link
    assert tasks == before


@pytest.mark.parametrize(
    "change", ["document", "section", "parent", "unit", "boundary"]
)
def test_curb_relation_requires_explicit_owner(change):
    tasks = pair()
    if change == "document":
        tasks[1]["position_source"]["document_sha256"] = "b" * 64
    elif change == "section":
        tasks[1]["position_source"]["sections"] = []
    elif change == "parent":
        tasks[0]["w_name"] = "Устройство бетонного основания"
    elif change == "unit":
        tasks[0]["w_unit"] = "м2"
    else:
        tasks.insert(
            1,
            dict(
                idx=2,
                position_type="Не определено",
                position_source={**tasks[0]["position_source"], "row": 22},
            ),
        )
        tasks[-1]["position_source"]["row"] = 23
    assert material_links(tasks) == {}
