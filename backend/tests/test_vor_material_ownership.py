from copy import deepcopy

import pytest

from vor_material_links import material_links
from vor_material_ownership import material_owners, ownership_requirement


def pair():
    source=dict(document_sha256='a'*64,sheet='ВОР',header_row=10,
        sections=[dict(address='A11',text='Дорожная одежда')])
    return [dict(idx=0,number='1',w_name='Установка бортового камня',w_vol='161',w_unit='м',
        position_type='Работа',position_source={**deepcopy(source),'row':12}),
        dict(idx=1,number='2',w_name='Камень бортовой бетонный',w_vol='8.372',w_unit='м3',
        position_type='Материал',position_source={**deepcopy(source),'row':13},
        w_formula='161*0.052',calculation=dict(expression='=161*0.052',status='calculated',value='8.372'))]


def test_structural_owner_does_not_infer_units_rates_or_material_compatibility():
    tasks=pair()
    before=deepcopy(tasks)
    link=material_links(tasks)[1]
    assert link['index']==0 and link['relation_basis']=='explicit_workbook_owner_linear_curb'
    assert link['conversion_verified'] is False and link['material_compatibility_verified'] is False
    owner=material_owners(tasks)[1]
    assert owner['index']==0 and owner['state']=='ownership_only'
    assert owner['conversion_verified'] is False
    assert 'quantity' not in owner and 'rate' not in owner and 'confirmed' not in owner
    requirement=ownership_requirement(tasks[1],owner)
    assert requirement['state']=='needs_facts' and requirement['required'] is True
    assert requirement['declared_value']=='8.372' and requirement['unit']=='м3'
    assert tasks==before and material_links(tasks)=={1:link}


@pytest.mark.parametrize('mutate',[
    lambda t:t[1].update(w_vol='999999',w_unit='кг',w_name='Семена трав'),
    lambda t:t[1]['calculation'].update(status='error',error='bad formula',cached_mismatch=True),
    lambda t:t[1].update(w_formula='(357.13*0.2)*1.26',calculation={'expression':'=105.84*1.26'}),
])
def test_wrong_or_conflicting_calculations_do_not_relocate_structural_owner(mutate):
    tasks=pair()
    mutate(tasks)
    before=deepcopy(tasks)
    owner=material_owners(tasks)[1]
    assert owner['index']==0 and owner['conversion_verified'] is False
    assert ownership_requirement(tasks[1],owner)['state']=='needs_facts'
    assert tasks==before


def test_product_without_formula_keeps_its_own_mass_and_requires_product_basis():
    tasks=pair()
    tasks[0].update(w_name='Установка автопавильона',w_vol='1',w_unit='шт')
    tasks[1].update(w_name='Автопавильон',w_vol='1.2',w_unit='т',w_formula='',calculation=None)
    owner=material_owners(tasks)[1]
    assert owner['formula_present'] is False
    card=ownership_requirement(tasks[1],owner)
    assert card['declared_value']=='1.2' and card['unit']=='т' and card['state']=='needs_facts'
    assert 'паспортом' in card['reason']
    assert material_links(tasks)=={}


@pytest.mark.parametrize('mutate',[
    lambda t:t[1]['position_source'].update(document_sha256='b'*64),
    lambda t:t[1]['position_source'].update(document_sha256=None),
    lambda t:t[1]['position_source'].update(sheet='Другой'),
    lambda t:t[1]['position_source'].update(header_row=11),
    lambda t:t[1]['position_source'].update(sections=[dict(address='A20',text='Другой участок')]),
    lambda t:t[1]['position_source'].update(sections=[]),
    lambda t:t[1]['position_source'].update(row=12),
    lambda t:t[1].update(idx=0),
    lambda t:t[0].update(position_type='Неизвестно'),
    lambda t:t[1].update(position_type='Работа'),
])
def test_ambiguous_boundaries_or_identity_never_establish_ownership(mutate):
    tasks=pair()
    mutate(tasks)
    assert material_owners(tasks)=={}


def test_material_siblings_are_allowed_but_unknown_rows_and_new_work_are_boundaries():
    tasks=pair()
    tasks[1]['position_source']['row']=14
    middle=deepcopy(tasks[1])
    middle.update(idx=2,number='3')
    middle['position_source']['row']=13
    assert material_owners([*tasks,middle])[1]['index']==0
    middle['position_type']='Неизвестно'
    assert 1 not in material_owners([*tasks,middle])
    middle['position_type']='Работа'
    assert material_owners([*tasks,middle])[1]['index']==2


def test_returned_source_metadata_cannot_mutate_original_workbook():
    tasks=pair()
    owner=material_owners(tasks)[1]
    owner['provenance']['sections'][0]['text']='changed'
    assert tasks[1]['position_source']['sections'][0]['text']=='Дорожная одежда'


def test_unlocated_intervening_row_is_not_silently_skipped():
    tasks=pair()
    tasks[1].update(idx=2)
    tasks[1]['position_source']['row']=14
    middle=deepcopy(tasks[0])
    middle.update(idx=1,position_source={})
    assert material_owners([*tasks,middle])=={}


def domain_case(found_unit='м', found_value='161'):
    from audit_models import VorPosition
    from vor_material_ownership import navigation_owner
    positions={t['idx']:VorPosition(index=t['idx'],number=t['number'],work_name=t['w_name'],
        volume=t['w_vol'],unit=t['w_unit'],position_type=t['position_type'],
        position_source={**t['position_source'],'format':'xlsx','position_id':str(t['idx'])}) for t in pair()}
    relation=dict(parent=None,children=[])
    relation['material_owner']=navigation_owner(1,positions,relation)
    text=f'Камень бортовой бетонный: {found_value} {found_unit}'
    result=dict(pipeline_version=4,execution_state='complete',sources=[
        dict(id='p',kind='project',origin='native',page=1,version_id='pd',text=text),
        dict(id='n',kind='normative',origin='native',page=1,label='Норма',text='Бетонный борт')],
        items=[dict(topic='Количество',state='possible_mismatch',claim_ids=['quantity','unit'],
            project_source_ids=['p'],normative_source_ids=[],explanation='Модель сравнила разные единицы.'),
            dict(topic='Применимость',state='confirmed',claim_ids=['normative:applicability'],
                project_source_ids=['p'],normative_source_ids=['n'],explanation='Применимость подтверждена.')],
        parameter_proposals=[dict(parameter='quantity',source_id='p',value=found_value,unit=found_unit,
            quote=text,scope_quote=text,scope_state='verified')])
    return positions,relation,result


@pytest.mark.parametrize('unit',['м','м2'])
def test_weak_owner_cannot_turn_unproved_conversion_into_fix_even_with_confirmed_applicability(unit):
    from normative_parameters import enrich
    from normative_verdict import publish
    positions,relation,result=domain_case(unit)
    checked=enrich(positions[1],result,positions,relation)
    quantity=next(c for c in checked['parameter_cards'] if c['id']=='quantity')
    assert quantity['state']=='needs_facts'
    assert quantity['declared_value']=='8.372' and quantity['unit']=='м3'
    assert 'структуре ВОР' in quantity['reason']
    assert checked['items'][0]['state']=='needs_facts'
    report=publish({'Статус аудита':'Требуется ручная проверка'},
        dict(checks=[dict(claim_id='quantity',state='not_found',required=True)]),'key',
        {'stage':'done','review':dict(checked,position_key='key')},enabled=True)
    assert report['Статус аудита']=='Требуется ручная проверка'
    # Workbook arithmetic contradictions remain authoritative and independent.
    report=publish({'Статус аудита':'Требуется ручная проверка'},
        dict(workbook_notes=[dict(kind='formula-mismatch',message='Разные исходные формулы D/E')]),'key',
        {'stage':'done','review':dict(checked,position_key='key')},enabled=True)
    assert report['Статус аудита']=='Обнаружено расхождение'
    assert report['Что не так']=='Разные исходные формулы D/E'


def test_same_unit_actual_quantity_conflict_and_confirmed_own_quantity_are_preserved():
    from normative_parameters import enrich
    positions,relation,result=domain_case('м3','9')
    checked=enrich(positions[1],result,positions,relation)
    assert next(c for c in checked['parameter_cards'] if c['id']=='quantity')['state']=='possible_mismatch'
    positions,relation,result=domain_case('м3','8.372')
    result['items'][0]['state']='confirmed'
    checked=enrich(positions[1],result,positions,relation)
    assert next(c for c in checked['parameter_cards'] if c['id']=='quantity')['state']=='confirmed'


def test_navigation_hint_never_replaces_formal_parent_or_adds_quantity_query_children():
    from vor_material_ownership import navigation_owner
    positions,relation,_=domain_case()
    assert relation['parent'] is None and relation['children']==[]
    assert navigation_owner(0,positions,relation) is None
    strict=dict(parent={'index':0},children=[])
    assert navigation_owner(1,positions,strict) is None
    assert strict==dict(parent={'index':0},children=[])
