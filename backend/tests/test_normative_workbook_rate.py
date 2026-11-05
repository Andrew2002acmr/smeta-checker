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


@pytest.mark.parametrize('unit,comment,expression,rate_unit,divisor,dimension',[
    ('м3','Расход воды-0,15м3/м2','=1743*0.15','м3/м2','1','volume'),
    ('кг','Расход 3кг/100м2','=1743*3/100','кг/100м2','100','mass'),
    ('т','Расход 3кг/100м2','=1743*3/100/1000','кг/100м2','100000','mass'),
    ('м3','Расход 0,7 л/м2','=1743*0.7/1000','л/м2','1000','volume'),
])
def test_area_input_is_typed_for_dimensioned_volume_and_mass_rates(unit,comment,expression,rate_unit,divisor,dimension):
    p=position(unit=unit,comment=comment,calculation={'expression':expression})
    proof=analyze(p)
    assert proof['area']=='1743' and proof['rate_unit']==rate_unit
    assert proof['divisor']==divisor and proof['output_dimension']==dimension
    assert not proof['density_required'] and not proof['project_quantity_verified']
    assert not proof['rate_applicability_verified']
    from normative_workbook_rate import quantity_contract
    contract=quantity_contract(p)
    assert contract['project_input']['claim_id']=='input:area' and not contract['verified']


@pytest.mark.parametrize('unit,comment,expression',[
    ('кг','Расход воды-0,15м3/м2','=1743*0.15'),
    ('м3','Расход 3кг/100м2','=1743*3/100'),
    ('кг','Расход 3кг/100м2','=1743*3/1000'),
    ('м3','Расход 0,7 л/м2','=1743*0.7'),
    ('м3','Расход 0,15м3/м2','=1743*0.15*2'),
])
def test_dimension_mismatch_or_unexplained_multiplier_is_not_an_area_recipe(unit,comment,expression):
    assert analyze(position(unit=unit,comment=comment,calculation={'expression':expression})) is None


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


@pytest.mark.parametrize('comment,formula,area,rate,equation', [
    ('Расход ЩМА на 1000м2 Тех.часть ГЭСН 81-02-27-2022 прил. 27.1'
     '(Ү-2,7-2,9т/м3) 98,64+(12,33*2)=123,3т',
     '=8921/1000*123.3', '8921', '123.3', '98,64+(12,33*2)'),
    ('Расход а/б смеси на 1000м2 Тех.часть ГЭСН 81-02-27-2022 прил. 27.1'
     '(Ү-2,7-2,9т/м3) 98,56+(12,32*8)=197,12т',
     '=874.42/1000*197.12', '874.42', '197.12', '98,56+(12,32*8)'),
    ('Норма расхода смеси на 100 м² (проектный расчёт) 10-(2*2)=6 т',
     '=310*6/100', '310', '6', '10-(2*2)'),
])
def test_reversed_rate_declaration_exposes_area_without_certifying_appendix_inputs(
        comment, formula, area, rate, equation):
    p = position(comment=comment, calculation={'expression': formula})
    proof = analyze(p)
    assert proof is not None
    assert proof['area'] == area and proof['rate'] == rate
    assert proof['rate_unit'] == 'т/' + proof['rate_denominator_area'] + 'м2'
    assert proof['output_dimension'] == 'mass' and not proof['density_required']
    assert proof['declared_equation']['expression'] == equation
    assert not proof['declared_equation']['inputs_verified']
    assert not proof['declared_equation']['normative_basis_verified']
    assert not proof['rate_applicability_verified'] and not proof['project_quantity_verified']
    # No amount in the declaration is relabelled a measured material density.
    assert 'density' not in proof['declared_equation']


@pytest.mark.parametrize('comment,formula,unit', [
    ('Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,4т', '=106/1000*123.4', 'т'),
    ('Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,3т', '=106/1000*123.4', 'т'),
    ('Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,3т', '=106*123.3', 'т'),
    ('Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,3т', '=106/1000*123.3', 'м3'),
    ('Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,3т; Расход 1кг/м2',
     '=106/1000*123.3', 'т'),
    ('Расход на 1000м2 (приложение) 98,64/0=123,3т', '=106/1000*123.3', 'т'),
    ('Расход на 1000м2 (приложение) 2**3=8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 ^ 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) x * 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 >> 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 & 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 | 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 @ 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) 2 ! 8 = 8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) '
     '1000000000000000+0.0000000000000001-1000000000000000+8=8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (приложение) '
     '100000000000000+0.0000000000000001-100000000000000+8=8т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (варианты) 8=8т или 9=9т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (варианты) 8=8т +1т', '=106/1000*8', 'т'),
    ('Расход на 1000м2 (варианты) 8=8т/м2', '=106/1000*8', 'т'),
])
def test_invalid_or_competing_reversed_declarations_are_not_typed(comment, formula, unit):
    assert analyze(position(comment=comment, unit=unit, calculation={'expression': formula})) is None


def test_tonnes_per_area_do_not_need_a_fictitious_mass_density_conversion():
    proof = analyze(position(comment='Расход 123,3т/1000м2',
        calculation={'expression': '=106/1000*123.3'}))
    assert proof['rate_unit'] == 'т/1000м2' and proof['divisor'] == '1000'
    assert proof['conversion'] == 'same_mass_unit' and not proof['density_required']


def test_reversed_declaration_alone_leaves_rate_quantity_and_area_unconfirmed():
    p = position(comment='Расход на 1000м2 (приложение) 98,64+(12,33*2)=123,3т',
        calculation={'expression': '=106/1000*123.3'})
    result = dict(evidence_trace=[], parameter_cards=[
        dict(id='calculation:7', state='confirmed', role='calculation'),
        *[dict(id='factor:7:' + n, state='needs_facts', role='project_value')
          for n in ['106', '123.3', '1000']],
        dict(id='quantity', state='needs_facts', role='project_value')])
    annotate(p, result, {7: p})
    cards = {c['id']: c for c in result['parameter_cards']}
    assert all(cards[c]['state'] == 'needs_facts'
        for c in ['factor:7:106', 'factor:7:123.3', 'quantity'])
    assert cards['factor:7:1000']['state'] == 'confirmed'
    assert 'declared_equation' in result['evidence_trace'][0]
