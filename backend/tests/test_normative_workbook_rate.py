"""Dimensioned workbook rates are read without inventing PDF facts or density."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from normative_workbook_rate import analyze, annotate, related_area


def position(**updates):
    p = SimpleNamespace(index=7, work_name='Розлив эмульсии на выравнивающий слой покрытия', unit='т',
        volume='2.092432', comment='Расход 0,4 л/м2', formula='5303,08*0,4/1000',
        calculation={'expression': '=5231.08*0.4/1000'},
        position_source=SimpleNamespace(sheet='Лист', row=36, document_sha256='a'*64,
            sections=[{'text': 'Улица', 'address': 'A1'}]))
    for k, v in updates.items():
        setattr(p, k, v)
    return p


def construction(**updates):
    p = position(index=2, work_name='Устройство выравнивающего слоя из асфальтобетона',
        unit='м2', volume='5303', calculation={}, comment='', formula='')
    p.position_source.row = 34
    for k, v in updates.items():
        setattr(p, k, v)
    return p


def test_litres_do_not_become_tonnes_without_density():
    proof = analyze(position())
    assert proof['area'] == '5231.08' and proof['rate'] == '0.4'
    assert proof['rate_origin'] == 'vor_comment' and not proof['rate_applicability_verified']
    assert proof['density_required'] and proof['conversion'] == 'litres_to_cubic_metres'
    assert not proof['project_quantity_verified']


@pytest.mark.parametrize('expression', ['=5231.08*0.4/1000', '=0.4*(5231.08/1000)', '=(5231.08*0.4)/1000'])
def test_mass_conversion_is_recognized_only_from_typed_rate(expression):
    proof = analyze(position(comment='Расход: 0,4 кг/м²', calculation={'expression': expression}))
    assert proof['conversion'] == 'kilograms_to_tonnes' and not proof['density_required']


@pytest.mark.parametrize('comment,expression', [
    ('0,4', '=5231.08*0.4/1000'),
    ('Расход 0,5 кг/м2', '=5231.08*0.4/1000'),
    ('Расход 0,4 л/м2; расход 0,6 кг/м2', '=5231.08*0.4/1000'),
    ('Расход 0,4 кг/м2', '=A34*0.4/1000'),
    ('Расход 0,4 кг/м2', '=5231.08*0.4/100'),
    ('Расход 0,4 кг/м2', '=5231.08*0.4*1000'),
    ('Расход 0,4 кг/м2', '=5231.08*0.4/1000+1'),
    ('Расход 0,4 кг/м2', '=5231.08*0.4/1000*2'),
    ('Расход 0,4 кг/м2', '=1e10*0.4/1000'),
])
def test_ambiguous_unsupported_or_other_arithmetic_is_not_certified(comment, expression):
    assert analyze(position(comment=comment, calculation={'expression': expression})) is None


def test_dependency_uses_section_and_layer_never_expected_number_or_nearest_row():
    work, parent = position(), construction(volume='9999')
    # The number intentionally disagrees. A material between them is not the area owner.
    material = position(index=6, work_name='Асфальтобетонная смесь', unit='т')
    found = related_area(work, {7: work, 6: material, 2: parent})
    assert len(found) == 1 and found[0]['index'] == 2 and found[0]['quantity'] == '9999'
    assert not found[0]['project_quantity_verified']
    another = construction(index=3)
    assert len(related_area(work, {2: parent, 3: another})) == 2


@pytest.mark.parametrize('change', ['section', 'document', 'sheet', 'later', 'layer'])
def test_other_scope_never_supplies_area(change):
    work, parent = position(), construction()
    if change == 'section':
        parent.position_source.sections = [{'text': 'Съезды'}]
    elif change == 'document':
        parent.position_source.document_sha256 = 'b'*64
    elif change == 'sheet':
        parent.position_source.sheet = 'Другой лист'
    elif change == 'later':
        parent.position_source.row = 40
    elif change == 'layer':
        parent.work_name = 'Устройство нижнего слоя основания'
    assert not related_area(work, {2: parent})


def test_annotations_explain_all_operands_without_confirming_geometry_or_litres():
    work = position()
    result = dict(evidence_trace=[], parameter_cards=[
        dict(id='calculation:7', state='confirmed', role='calculation'),
        *[dict(id='factor:7:'+n, state='needs_facts', role='project_value') for n in ['5231.08', '0.4', '1000']]])
    before = deepcopy(vars(work))
    annotate(work, result, {2: construction(), 7: work})
    assert all(c['state'] == 'needs_facts' for c in result['parameter_cards'][1:])
    assert result['parameter_cards'][1]['label'] == 'Исходная площадь расчёта'
    assert '34' in result['parameter_cards'][1]['reason']
    assert result['parameter_cards'][2]['quote'] == 'Расход 0,4 л/м2'
    assert 'плотность' in result['parameter_cards'][3]['reason']
    assert work.calculation == before['calculation']
    annotate(work, result, {2: construction(), 7: work})
    assert len(result['evidence_trace']) == 1
    work.comment = 'Расход 0,4 кг/м2'
    annotate(work, result, {7: work})
    assert result['parameter_cards'][3]['state'] == 'confirmed'
    assert result['parameter_cards'][1]['state'] == 'needs_facts'


def test_comment_rate_and_unit_conversion_do_not_erase_real_operand_disagreement():
    from test_normative_workbook_conflicts import fixture
    from normative_workbook_conflicts import conflicts
    from human_basis import position_key
    p = fixture()
    finding, = conflicts(p, position_key(p))
    assert finding['inputs']['different_operands'] == [('5231.08', '5303.08')]
    assert '0.4 л/м2' in finding['message'] and '1000' in finding['message']
    assert Decimal(finding['inputs']['actual_value']) != Decimal(finding['inputs']['display_value'])
