from copy import deepcopy
from types import SimpleNamespace
from normative_parameters import build_cards, quoted, conditions


def position(index=0, volume='431.61', unit='м2', **extra):
    return SimpleNamespace(index=index, number=str(index+1), volume=volume, unit=unit,
        work_name='Устройство основания из ЩПС, h=12 см', formula='', calculation={},
        position_source=SimpleNamespace(row=18+index), **extra)


def source(id='p1', kind='project', text='Площадь 431,611 м2; толщина 120 мм.', origin='native'):
    return dict(id=id, kind=kind, text=text, origin=origin, label='Source')


def proposal(parameter, value, unit, quote, id='p1', rules=None):
    return dict(parameter=parameter, value=value, unit=unit, quote=quote, source_id=id, conditions=rules or [])


def test_native_values_are_converted_and_rounded_without_trusting_model_verdict():
    p = position()
    result = dict(sources=[source()], parameter_proposals=[
        proposal('quantity', '431.611', 'м2', 'Площадь 431,611 м2'),
        proposal('thickness', '120', 'мм', 'толщина 120 мм')])
    cards = build_cards(p, result, {0:p}, {})
    assert [c['state'] for c in cards] == ['confirmed', 'confirmed']
    result['parameter_proposals'][1]['unit'] = 'м'
    assert build_cards(p, result, {0:p}, {})[1]['state'] == 'needs_facts'
    result['parameter_proposals'][0]['quote'] = 'Вымышленная площадь 431,611 м2'
    assert build_cards(p, result, {0:p}, {})[0]['state'] == 'needs_facts'


def test_ocr_and_ambiguous_or_wrong_dimension_values_cannot_be_confirmed():
    p = position()
    result = dict(sources=[source(origin='ocr_unverified')], parameter_proposals=[
        proposal('quantity', '431.611', 'м2', 'Площадь 431,611 м2')])
    assert build_cards(p, result, {0:p}, {})[0]['state'] == 'needs_facts'
    result['sources']=[source(text='Площадь 431,611 м3')]
    result['parameter_proposals'][0].update(unit='м3',quote='Площадь 431,611 м3')
    assert build_cards(p, result, {0:p}, {})[0]['state'] == 'possible_mismatch'
    result['parameter_proposals'].append(deepcopy(result['parameter_proposals'][0]))
    result['parameter_proposals'][1]['value']='400'
    assert build_cards(p, result, {0:p}, {})[0]['state'] == 'needs_facts'


def test_related_material_checks_parent_quantity_and_arithmetic_separately_from_factor():
    parent, child = position(), position(1, '65.259432', 'м3')
    child.formula='(431.61*0.12)*1.26'
    child.calculation={'expression':child.formula}
    cards=build_cards(child, dict(sources=[], parameter_proposals=[]), {0:parent,1:child}, {'parent':{'index':0}})
    assert cards[0]['declared_value']=='431.61' and cards[0]['unit']=='м2'
    assert next(c for c in cards if c['role']=='calculation')['state']=='confirmed'
    factor=next(c for c in cards if c['role']=='coefficient')
    assert factor['declared_value']=='1.26' and factor['state']=='needs_facts'
    assert factor.get('required', True)
    parent_cards = build_cards(parent, dict(sources=[]), {0:parent,1:child}, {'children':[{'material_index':1}]})
    assert next(c for c in parent_cards if c['role']=='coefficient')['required'] is False
    child.volume='66'
    cards=build_cards(child, dict(sources=[]), {0:parent,1:child}, {'parent':{'index':0}})
    assert next(c for c in cards if c['role']=='calculation')['state']=='possible_mismatch'


def test_normative_scale_does_not_turn_100_square_metres_into_one():
    p=position()
    result=dict(sources=[source('n1','normative','Измеритель: 100 м2.')], parameter_proposals=[
        proposal('unit_scale','100','м2','Измеритель: 100 м2.',id='n1')])
    card=next(c for c in build_cards(p,result,{0:p},{}) if c['role']=='unit_scale')
    assert card['state']=='confirmed' and '4.3161' in card['reason']


def test_conditions_need_real_quotes_correct_operator_units_and_confirmed_facts():
    s=source('n1','normative','Коэффициент 1,26. При толщине более 18 см.')
    rule=dict(parameter='thickness',operator='gt',value='18',unit='см',quote='При толщине более 18 см.')
    p=proposal('coefficient','1.26','','Коэффициент 1,26.',id='n1',rules=[rule])
    from decimal import Decimal
    assert conditions(p,s,{'thickness':('length',Decimal('.24'))})[1]==[True]
    assert conditions(p,s,{})[1]==[None]
    rule['unit']='м'
    assert conditions(p,s,{'thickness':('length',Decimal('.24'))})[1]==[None]
    rule.update(unit='см',operator='lt')
    assert conditions(p,s,{'thickness':('length',Decimal('.24'))})[1]==[None]
    assert quoted('1.26','Коэффициент 1,26.',s['text'])
    assert not quoted('1.26','Коэффициент 51,26.','Коэффициент 51,26.')


def test_coefficient_requires_conditions_and_arithmetic_does_not_confirm_it():
    p, child = position(), position(1,'65.259432','м3')
    child.formula='431.61*0.12*1.26'
    result=dict(sources=[source(), source('n1','normative','Коэффициент 1,26. При толщине не более 18 см.')], parameter_proposals=[
        proposal('quantity','431.611','м2','Площадь 431,611 м2'),
        proposal('thickness','120','мм','толщина 120 мм'),
        proposal('coefficient','1.26','','Коэффициент 1,26.',id='n1',rules=[
            dict(parameter='thickness',operator='le',value='18',unit='см',quote='При толщине не более 18 см.')])])
    relation={'children':[{'material_index':1}]}
    def factor(r):
        return next(c for c in build_cards(p,r,{0:p,1:child},relation) if c['role']=='coefficient')
    assert factor(result)['state']=='confirmed'
    result['parameter_proposals'][-1]['conditions']=[]
    assert factor(result)['state']=='needs_facts'


def test_project_bound_is_not_a_proved_quantity_mismatch():
    p=position()
    result=dict(sources=[source(text='Площадь не более 500 м2.')], parameter_proposals=[
        proposal('quantity','500','м2','Площадь не более 500 м2.')])
    assert build_cards(p,result,{0:p},{})[0]['state']=='needs_facts'


def test_unbounded_numbers_are_rejected_instead_of_overflowing():
    from normative_parameters import measure
    assert measure('1e999999','м') is None
    assert measure('NaN','м') is None
    assert measure('999999999999999999999','м') is None


def test_material_inherits_verified_geometry_with_unambiguous_source_names():
    from normative_parameters import inherit_work_evidence
    parent=dict(sources=[source()],local_work_basis=dict(quantity='431.61',unit='м2',thickness_cm='24',norm_scale='1000',
        project_source_ids=['p1'],normative_source_ids=[],anchors={'quantity':[dict(source_id='p1',quote='Площадь',rects=[])]}),
        quantity_composition=dict(source_id='p1',total='431.611'))
    original=deepcopy(parent)
    child=dict(sources=[source(text='Другой исходный фрагмент')])
    value=inherit_work_evidence(child,parent,0)
    assert len(value['sources'])==2 and value['sources'][0]['id']=='p1'
    assert value['local_work_basis']['project_source_ids']==['work-0:p1']
    assert value['quantity_composition']['source_id']=='work-0:p1'
    assert inherit_work_evidence(value,parent,0)==value
    assert parent==original and len(child['sources'])==1
