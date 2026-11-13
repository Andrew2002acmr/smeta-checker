from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import pytest

from audit_reports import build_user_report, DISCREPANCY, MANUAL
from vor_material_links import _family, material_links


def tasks():
    source = dict(sheet="ВОР", sections=[dict(address="A17", text="Улица")])
    parent = dict(idx=0, number="1", w_name="Устройство основания из ЩПС, h = 12 см",
                  w_vol="431.61", w_unit="м2", position_type="Работа",
                  position_source={**source, "row": 18})
    material = dict(idx=1, number="2", w_name="Щебеночно-песчаная смесь М800 типа 0/31,5 мм",
                    w_vol="65.259432", w_unit="м3", position_type="Материал",
                    position_source={**source, "row": 19},
                    calculation=dict(status="calculated", value="65.259432",
                                     expression="=(431.61*0.12)*1.26",
                                     dependencies=[dict(value=v) for v in ["431.61", "0.12", "1.26"]]))
    return [parent, material]


def test_links_area_and_thickness_without_claiming_normative_rate():
    pair = tasks()
    assert material_links(pair)[1]["row"] == 18
    pair[1]["calculation"]["dependencies"][0] = dict(cell="$D$18", value="431.61")
    assert material_links(pair)[1]["number"] == "1"


@pytest.mark.parametrize('change',['valid','unknown_document','wrong_section','no_thickness','known_other_material'])
def test_generic_base_uses_explicit_structural_owner_without_inventing_material_compatibility(change):
    pair=tasks()
    for task in pair:
        task['position_source'].update(document_sha256='a'*64,header_row=15)
    pair[0]['w_name']='Устройство слоя основания, h = 12 см бульдозером'
    if change=='unknown_document':
        pair[0]['position_source'].pop('document_sha256')
    elif change=='wrong_section':
        pair[1]['position_source']['sections']=[dict(address='A20',text='Другой участок')]
    elif change=='no_thickness':
        pair[0]['w_name']='Устройство слоя основания бульдозером'
    elif change=='known_other_material':
        pair[0]['w_name']='Устройство основания из бетона, h = 12 см'
    links=material_links(pair)
    if change=='valid':
        assert links[1]['index']==0
        assert links[1]['relation_basis']=='explicit_workbook_owner_generic_base'
        assert links[1]['material_compatibility_verified'] is False
        assert 'quantity' not in links[1] and 'rate' not in links[1]
    else:
        assert links=={}


def asphalt_tasks():
    pair = tasks()
    pair[0].update(w_name='Устройство основания из асфальтобетонной смеси, h=10 см')
    pair[1].update(w_name='Асфальтобетонная смесь А32От', w_unit='т', w_vol='95.7138336')
    pair[1]['calculation'].update(value='95.7138336', expression='=431.61/1000*221.76',
                                 dependencies=[dict(value=v) for v in ['431.61','1000','221.76']])
    return pair


def test_asphalt_mass_has_area_parent_but_rate_is_still_unchecked():
    pair = asphalt_tasks()
    link = material_links(pair)[1]
    assert link['index'] == 0
    assert 'confirmed' not in link and 'rate' not in link
    source = dict(status='НЕ НАЙДЕНО', work_found=False, checks=[])
    from audit_review_policy import review_source
    reviewed = review_source(source, {**pair[1], 'derived_material': link})
    assert any(c['claim_id'] == 'derived:rate' and c['state'] == 'review_required'
               for c in reviewed['checks'])


@pytest.mark.parametrize('change', [
    lambda p: p[1]['position_source'].update(sections=[]),
    lambda p: p[1].update(w_name='Бетон тяжелый'),
    lambda p: p[0].update(w_unit='м'),
])
def test_asphalt_mass_link_requires_same_section_material_and_compatible_units(change):
    pair = asphalt_tasks()
    change(pair)
    assert material_links(pair) == {}


def test_rejects_unrelated_rows_wrong_calculations_and_missing_thickness():
    for change in [
        lambda p: p[0].update(w_name="Устройство асфальтобетонного покрытия, h = 12 см"),
        lambda p: p[1]["position_source"].update(sections=[]),
        lambda p: p[1].update(w_vol="66"),
        lambda p: p[1]["calculation"].update(cached_mismatch=True),
        lambda p: p[0].update(w_name="Устройство основания из ЩПС"),
    ]:
        pair = tasks()
        change(pair)
        assert material_links(pair) == {}


def emulsion_tasks(parent_volume='0.345288', base='0.345'):
    pair = tasks()
    amount = str(Decimal(base)*Decimal('1.03'))
    pair[0].update(w_name='Подгрунтовочные работы на средний слой основания',w_vol=parent_volume,w_unit='т')
    pair[1].update(w_name='Битумная эмульсия ЭБДК Б',w_unit='т',w_vol=amount)
    pair[1]['calculation'].update(value=amount,expression=f'={base}*1.03',
        dependencies=[dict(value=v) for v in [base,'1.03']])
    return pair


def numeric_cards(pair):
    from normative_parameters import build_cards
    positions = {p['idx']:SimpleNamespace(index=p['idx'],number=p['number'],work_name=p['w_name'],
        volume=p['w_vol'],unit=p['w_unit'],formula=(p.get('calculation') or {}).get('expression',''),
        calculation=p.get('calculation') or {},comment='',
        position_source=SimpleNamespace(row=p['position_source']['row'])) for p in pair}
    parent=positions[0]
    quote=f'{parent.work_name}. Объём {parent.volume} {parent.unit}'
    result=dict(pipeline_version=3,sources=[dict(id='pd',kind='project',origin='native',text=quote)],
        parameter_proposals=[dict(parameter='quantity',source_id='pd',value=parent.volume,
            unit=parent.unit,quote=quote,scope_state='verified',scope_quote=quote)])
    return build_cards(positions[1],result,positions,{'parent':material_links(pair)[1]})


@pytest.mark.parametrize('parent_volume,base', [('0.345288','0.345'),('0.067048','0.334')])
def test_rounded_or_wrong_base_retains_real_parent_but_never_confirms_operand(parent_volume,base):
    pair=emulsion_tasks(parent_volume,base)
    before=deepcopy(pair)
    link=material_links(pair)[1]
    assert link['index']==0 and link['expression']==f'={base}*1.03'
    assert 'confirmed' not in link and 'rate' not in link and 'quantity' not in link
    cards=numeric_cards(pair)
    quantity=next(c for c in cards if c['id']=='quantity')
    assert quantity['declared_value']==parent_volume and quantity['state']=='confirmed'
    base_card=next(c for c in cards if c['id']==f'factor:1:{base}')
    assert base_card['state']=='needs_facts'
    assert next(c for c in cards if c['id']=='calculation:1')['state']=='confirmed'
    assert pair==before


def test_wrong_asphalt_area_and_wrong_aggregate_depth_are_numeric_issues_not_unrelated_work():
    pair=asphalt_tasks()
    pair[1]['w_vol']='1970.78112'
    pair[1]['calculation'].update(value='1970.78112',expression='=8887/1000*221.76',
        dependencies=[dict(value=v) for v in ['8887','1000','221.76']])
    assert material_links(pair)[1]['index']==0
    assert next(c for c in numeric_cards(pair) if c['id']=='factor:1:8887')['state']=='needs_facts'
    pair=tasks()
    pair[1]['w_vol']='54.38286'
    pair[1]['calculation'].update(value='54.38286',expression='=431.61*0.10*1.26',
        dependencies=[dict(value=v) for v in ['431.61','0.10','1.26']])
    assert material_links(pair)[1]['index']==0


@pytest.mark.parametrize('change', [
    lambda p:p[1]['position_source'].update(sections=[]),
    lambda p:p[1]['position_source'].update(sheet='Другой лист'),
    lambda p:p[1]['position_source'].update(document_sha256='other-document'),
    lambda p:p[1]['position_source'].update(header_row=40),
    lambda p:p[0].update(w_name='Устройство асфальтобетонного покрытия'),
    lambda p:p[0].update(w_name='Розлив битума'),
    lambda p:p[1].update(w_name='Акриловая эмульсия'),
    lambda p:p[0].update(w_unit='м3'),
    lambda p:p[0].update(w_unit=''),
])
def test_emulsion_structure_rejects_other_sources_families_and_units(change):
    pair=emulsion_tasks()
    change(pair)
    assert material_links(pair)=={}


def test_material_siblings_are_allowed_but_an_intervening_other_work_is_not_skipped():
    pair=emulsion_tasks()
    pair[1]['position_source']['row']=20
    middle=deepcopy(pair[1])
    middle.update(idx=2,number='3')
    middle['position_source']['row']=19
    assert material_links([*pair,middle])[1]['index']==0
    middle.update(position_type='Работа',w_name='Устройство бетонного лотка')
    assert 1 not in material_links([*pair,middle])


def test_emulsion_and_bitumen_are_distinct_resource_families():
    assert _family('Эмульсия битумно-дорожная')==_family('Битумная эмульсия ЭБДК Б')=='эмульсия'
    assert _family('Битум дорожный')=='битум'
    assert _family('Асфальтобетон на битуме')=='асфальтобетон'


@pytest.mark.parametrize("evaluation", [None, "evidence-claims-v1"])
def test_missing_material_is_explained_but_actual_conflicts_remain(evaluation):
    pair = tasks()
    source = dict(status="НЕ НАЙДЕНО", work_found=False, found_volume="431.61", found_unit="м2",
                  evaluation_version=evaluation, evidence=[{}],
                  checks=[dict(claim_id="quantity", state="contradiction", reasons=["unit_mismatch"])])
    before = deepcopy(source)
    rows = [{"№": "1", "Статус аудита": "OK"}, {"№": "2", "Статус аудита": source["status"]}]
    report = build_user_report(rows, [dict(status="OK", work_found=True), source], pair)
    material = next(r for r in report if r["№"] == "2")
    assert material["Статус аудита"] == MANUAL
    assert "Отдельная строка материала в ПД не требуется" in material["Что не так"]
    assert "Коэффициенты расхода требуют проверки" in material["Что не так"]
    assert source == before
    # A name match without a bound unit/quantity is not a complete material row.
    source.update(work_found=True, found_unit="")
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == MANUAL
    source.update(work_found=False)
    source["workbook_notes"] = [dict(kind="formula-mismatch", message="Арифметика не совпала")]
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == DISCREPANCY
    source.pop("workbook_notes")
    source.update(work_found=True, unit_confirmed=True, found_volume="60", found_unit="м3")
    source["checks"][0].update(reasons=["value_mismatch"])
    material = next(r for r in build_user_report(rows, [dict(status="OK"), source], pair) if r["№"] == "2")
    assert material["Статус аудита"] == DISCREPANCY
