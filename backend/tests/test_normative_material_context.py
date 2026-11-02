"""Neighbouring material text helps retrieval without creating quantity proof."""
from copy import deepcopy

import pytest

from audit_models import VorPosition,PositionSource,SourceHeading
from normative_position_check import adjacent_material_context
from vor_material_links import material_links


def fixture():
    source=PositionSource(document_sha256='vor',format='xlsx',sheet='ВОР',row=98,header_row=1,
        position_id='94',sections=[SourceHeading(address='A97',text='Устройство автобусных остановок')])
    work=VorPosition(index=94,number='95',work_name='Установка бортового камня',volume='161',unit='м',
        position_type='Работа',position_source=source)
    material=VorPosition(index=95,number='96',work_name='Камень бортовой бетонный БР100.30.18 (бетон В30)',
        volume='8.372',unit='м3',position_type='Материал',
        position_source=source.model_copy(update={'row':99,'position_id':'95'}))
    following=work.model_copy(update={'index':96,'work_name':'Устройство основания',
        'position_source':source.model_copy(update={'row':100,'position_id':'96'})})
    return work,{p.index:p for p in (work,material,following)}


def test_generic_work_receives_literal_material_hint_without_a_derived_link_or_expected_quantity():
    owner,positions=fixture()
    before=deepcopy(positions)
    result=adjacent_material_context(owner,positions)
    assert result==[dict(index=95,row=99,work=positions[95].work_name,
        provenance='following_material_row_same_section',role='retrieval_hint_only')]
    assert material_links([p.legacy_task() for p in positions.values()])=={}
    assert positions==before
    positions[95]=positions[95].model_copy(update={'volume':'99999','formula':'=99999/123'})
    assert adjacent_material_context(owner,positions)==result


@pytest.mark.parametrize('change',[
    {'sheet':'Другой лист'}, {'document_sha256':'another-vor'}, {'header_row':2}, {'format':'pdf'},
    {'sections':[SourceHeading(address='A97',text='Устройство тротуаров')]}, {'sections':[]}, {'row':97},
])
def test_hint_does_not_cross_document_sheet_table_section_or_precede_owner(change):
    owner,positions=fixture()
    positions[95]=positions[95].model_copy(update={
        'position_source':positions[95].position_source.model_copy(update=change)})
    assert adjacent_material_context(owner,positions)==[]


def test_hint_does_not_skip_intervening_work_or_another_section_boundary():
    owner,positions=fixture()
    positions[93]=positions[96].model_copy(update={'index':93,
        'position_source':positions[96].position_source.model_copy(update={'row':99})})
    positions[95]=positions[95].model_copy(update={
        'position_source':positions[95].position_source.model_copy(update={'row':100})})
    assert adjacent_material_context(owner,positions)==[]
    positions[93]=positions[93].model_copy(update={'position_type':'Материал',
        'position_source':positions[93].position_source.model_copy(update={'sections':[]})})
    assert adjacent_material_context(owner,positions)==[]


def test_hint_is_bounded_and_requires_an_explicit_work_owner_and_section():
    owner,positions=fixture()
    material=positions.pop(95)
    positions.pop(96)
    for index in range(95,105):
        positions[index]=material.model_copy(update={'index':index,
            'position_source':material.position_source.model_copy(update={'row':index+4})})
    assert len(adjacent_material_context(owner,positions))==3
    assert adjacent_material_context(material,positions)==[]
    owner=owner.model_copy(update={'position_source':owner.position_source.model_copy(update={'sections':[]})})
    assert adjacent_material_context(owner,positions)==[]
