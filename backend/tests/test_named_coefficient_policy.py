from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from normative_coefficient_policy import apply,accepted_claims,named


def fixture(comment='Купл-1,26',formula='431.61*0.12*1.26',unit='м3',parent_unit='м2'):
    p=SimpleNamespace(index=1,comment=comment,formula=formula,calculation={},unit=unit)
    r: dict[str, Any]=dict(items=[dict(topic='Расход',state='needs_facts',required=True,claim_ids=['derived:rate'],
        project_source_ids=[],normative_source_ids=[],explanation='Коэффициент не подтверждён'),
        dict(topic='Марка',state='needs_facts',required=True,claim_ids=['attribute:name:designation:25'],
        project_source_ids=[],normative_source_ids=[],explanation='Марка неизвестна')],
        parameter_cards=[dict(id='quantity',state='needs_facts',unit=parent_unit),
            dict(id='factor:1:1.26',role='coefficient',declared_value='1.26',state='needs_facts')])
    return p,r


def test_named_multiplier_is_accepted_without_claiming_normative_proof_or_geometry():
    p,r=fixture()
    original=deepcopy(r)
    out=apply(p,r)
    assert out['parameter_cards'][1]['state']=='confirmed'
    assert out['parameter_cards'][0]['state']=='needs_facts'
    assert any(i['claim_ids']==['derived:rate'] and i['state']=='confirmed' for i in out['items'])
    assert any(i['claim_ids']==['attribute:name:designation:25'] and i['state']=='needs_facts' for i in out['items'])
    assert not out['evidence_trace'][-1]['normative_value_proven']
    assert r==original
    again=apply(p,out)
    assert again['items']==out['items']


def test_estimator_provenance_is_explicit_and_not_a_normative_citation():
    p,r=fixture()
    out=apply(p,r)
    basis=out['evidence_trace'][-1]['entries'][0]['value_basis']
    assert basis['kind']=='estimator_provided'
    assert basis['document_reference'] is None
    assert basis['independently_verified'] is False
    assert 'сметчиками' in out['parameter_cards'][1]['reason']
    assert not out['evidence_trace'][-1]['normative_value_proven']
    for comment in ('Купл-1,25','Коэффициент расхода - 1,26'):
        other,_=fixture(comment)
        assert named(other)[0]['value_basis']['kind']=='estimator_provided'


def test_consumption_coefficient_generalizes_to_another_value():
    p,r=fixture('Коэффициент расхода - 1,03','0.345*1.03','т','т')
    assert 'derived:rate' in accepted_claims(p,r)
    assert 'basis:comment:consumption:1' in accepted_claims(p,r)


def test_other_explicit_coefficient_names_retain_their_purpose_and_provenance():
    for label in ('Коэффициент потерь', 'Коэффициент запаса', 'Коэффициент перехода'):
        p,r=fixture(label+' = 1,07','345*1.07','т','т')
        r['parameter_cards'][1].update(id='factor:1:1.07',declared_value='1.07')
        entry,=named(p)
        assert entry['purpose']==label.casefold()
        assert entry['value_basis']['kind']=='estimator_provided'
        out=apply(p,r)
        assert out['parameter_cards'][1]['state']=='confirmed'
        assert out['parameter_cards'][0]['state']=='needs_facts'
        assert not out['evidence_trace'][-1]['normative_value_proven']


def test_generic_name_does_not_waive_conditional_applicability_or_arithmetic():
    for label in ('Коэффициент потерь при увлажнении', 'Коэффициент только для песка'):
        p,r=fixture(label+' = 1,07','345*1.07','т','т')
        assert named(p)==[] and apply(p,r)==r
    p,r=fixture('Коэффициент потерь = 1,07','345*1.07','т','т')
    r['parameter_cards'][1].update(id='factor:1:1.07',declared_value='1.07',state='possible_mismatch')
    assert apply(p,r)['parameter_cards'][1]['state']=='possible_mismatch'


def test_compound_comment_does_not_confirm_its_material_conditions():
    p,r=fixture('ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26')
    assert len(named(p))==1
    assert 'attribute:comment:condition' not in accepted_claims(p,r)


def test_strict_policy_keeps_unknown_factor_open_without_erasing_independent_facts():
    from normative_coefficient_policy import STRICT
    for value in ('1.26', '1.37'):
        p,r=fixture('Коэффициент расхода - '+value, '12*'+value, 'т', 'т')
        r['coverage'] = dict(coefficient_policy=STRICT)
        r['parameter_cards'][1].update(id='factor:1:'+value, declared_value=value)
        r['items'][0]['state']='confirmed'
        independent=dict(topic='Площадь',state='confirmed',claim_ids=['quantity'],
            project_source_ids=['project-area'],normative_source_ids=[],explanation='Проверено')
        r['items'].append(independent)
        original=deepcopy(r)
        out=apply(p,r)
        assert out['parameter_cards'][1]['state']=='needs_facts'
        assert out['items'][0]['state']=='needs_facts'
        assert out['items'][-1]==independent
        assert accepted_claims(p,out)==set()
        assert out['evidence_trace'][-1]['policy']==STRICT
        assert r==original


def test_strict_policy_does_not_erase_an_independently_validated_factor():
    from normative_coefficient_policy import STRICT
    p,r=fixture()
    r['coverage']=dict(coefficient_policy=STRICT)
    r['parameter_cards'][1].update(state='confirmed',source_ids=['normative-source'])
    r['items'][0]['state']='confirmed'
    out=apply(p,r)
    assert out['items'][0]['state']=='confirmed'
    assert out['parameter_cards'][1]['source_ids']==['normative-source']


def test_strict_policy_does_not_treat_a_missing_factor_card_as_a_proof():
    from normative_coefficient_policy import STRICT
    p,r=fixture()
    r['coverage']=dict(coefficient_policy=STRICT)
    r['parameter_cards']=r['parameter_cards'][:1]
    r['items'][0]['state']='confirmed'
    assert apply(p,r)['items'][0]['state']=='needs_facts'


def test_strict_policy_partitions_dependent_rate_from_independent_area_and_thickness():
    from normative_coefficient_policy import STRICT
    p,r=fixture()
    r['coverage']=dict(coefficient_policy=STRICT)
    r['items'][0].update(state='confirmed',claim_ids=['quantity','geometry:thickness','derived:rate'],
        project_source_ids=['project-area','project-thickness'])
    out=apply(p,r)
    by_claim={claim:item['state'] for item in out['items'] for claim in item['claim_ids']}
    assert by_claim['derived:rate']=='needs_facts'
    assert by_claim['quantity']==by_claim['geometry:thickness']=='confirmed'
    assert r['items'][0]['state']=='confirmed'


def test_unknown_names_or_extra_conditions_are_not_silently_decoded():
    for text in ('Кнеизвестный-1,26','Купл-1,26 только для влажной смеси'):
        p,r=fixture(text)
        assert not named(p) and apply(p,r)==r


def test_actual_numeric_contradiction_stays_visible():
    p,r=fixture()
    r['parameter_cards'][1]['state']='possible_mismatch'
    assert apply(p,r)['parameter_cards'][1]['state']=='possible_mismatch'


def test_summary_does_not_keep_closed_coefficient_as_the_remaining_numeric_gap():
    p,r=fixture()
    r['parameter_cards'][0]['state']='confirmed'
    c=r['parameter_cards'][1]
    c.update(label='Коэффициент',reason='Не найден источник')
    r['summary']='Коэффициент: Не найден источник'
    out=apply(p,r)
    assert out['summary']=='Марка неизвестна'
    r['summary']='Найдены другие противоречия'
    assert apply(p,r)['summary']==r['summary']


def test_different_unit_scales_and_complex_consumption_are_not_proved_by_decoding():
    p,r=fixture('Коэффициент расхода - 1,03','345*1.03','т','кг')
    assert 'derived:rate' not in accepted_claims(p,r)
    p,r=fixture(formula='431.61*0.12*1.26*2.39')
    assert 'derived:rate' not in accepted_claims(p,r)


def conditional_fixture(value='1.26'):
    from normative_coefficient_policy import CONDITIONAL
    p,r=fixture('Купл-'+value, '431.61*0.12*'+value)
    p.work_name='Щебеночно-песчаная смесь М800 типа 0/63 мм'
    r['coverage']=dict(coefficient_policy=CONDITIONAL)
    r['parameter_cards'][1].update(id='factor:1:'+value,declared_value=value)
    r['parameter_cards'][0].update(state='confirmed',source_ids=['area'])
    r['sources']=[dict(id='area',kind='project',text='Щебеночно-песчаная смесь фр. 0/63 | Площадь, м2\n431,611'),
        dict(id='sp',kind='normative',source_id='pdf-sp78',version_id='sha-sp78',page=40,origin='native',
            label='СП 78.13330.2012 с изменениями1-3 · 10.2',
            text='10.2 Объем каменного материала в насыпном виде следует определять с учетом коэффициента запаса на '
                'уплотнение. Для песчано-гравийных (щебеночных) смесей и щебня фракций 31,5 - 63 и 63 - 90 мм марки по '
                'дробимости 800 и более коэффициент запаса материала на уплотнение следует ориентировочно принимать '
                '1,25 - 1,3, а для щебня марок по дробимости ниже 800 - 1,3 - 1,5. '
                'Для определения фактического коэффициента следует проводить пробную укатку.')]
    return p,r


def test_estimator_range_proves_allowance_not_actual_measurement_or_material_compatibility():
    p,r=conditional_fixture()
    before=deepcopy(r)
    out=apply(p,r)
    assert out['parameter_cards'][1]['state']=='confirmed'
    assert out['parameter_cards'][1]['source_ids']==['sp']
    assert next(i for i in out['items'] if i['claim_ids']==['derived:rate'])['state']=='confirmed'
    assert next(i for i in out['items'] if i['claim_ids']==['attribute:name:designation:25'])['state']=='needs_facts'
    proof=out['evidence_trace'][-1]['range_proofs'][0]
    assert (proof['lower'],proof['upper'])==('1.25','1.3')
    assert proof['actual_site_coefficient_proven'] is False
    assert r==before
    assert apply(p,out)['items']==out['items']


def test_estimator_range_generalizes_to_other_values_and_never_accepts_outside_range():
    for value in ('1.25','1.27','1.30'):
        p,r=conditional_fixture(value)
        assert apply(p,r)['parameter_cards'][1]['state']=='confirmed'
    for value in ('1.24','1.31','1.37'):
        p,r=conditional_fixture(value)
        assert apply(p,r)['parameter_cards'][1]['state']=='needs_facts'
        assert accepted_claims(p,r)==set()


def test_range_does_not_hide_numeric_contradiction_or_unresolved_geometry():
    p,r=conditional_fixture()
    r['parameter_cards'][1]['state']='possible_mismatch'
    assert accepted_claims(p,r)==set()
    assert apply(p,r)['parameter_cards'][1]['state']=='possible_mismatch'
    p,r=conditional_fixture()
    r['parameter_cards'][0]['state']='needs_facts'
    assert apply(p,r)==r


def test_range_requires_correct_material_branch_and_readable_registered_source():
    for field,value in [('origin','uncertain_table'),('origin','ocr'),('source_id',''),
                        ('page',None),('label','Другой СП · 10.2')]:
        p,r=conditional_fixture()
        r['sources'][1][field]=value
        assert apply(p,r)==r
    for name in ('Щебень М800 фр. 0/63','Песок','Щебеночно-песчаная смесь М600 типа 0/63 мм'):
        p,r=conditional_fixture()
        p.work_name=name
        assert apply(p,r)==r
    p,r=conditional_fixture()
    r['sources'][0]['text']=r['sources'][0]['text'].replace('0/63','0/31,5')
    assert apply(p,r)==r


def test_range_does_not_accept_unrecognized_extra_material_qualifier_or_conflicting_bounds():
    p,r=conditional_fixture()
    r['sources'][1]['text']=r['sources'][1]['text'].replace('смесей и щебня','смесей только для морских дорог и щебня')
    assert apply(p,r)==r
    p,r=conditional_fixture()
    other=deepcopy(r['sources'][1])
    other['id']='other'
    other['text']=other['text'].replace('1,25 - 1,3,','1,2 - 1,4,')
    r['sources'].append(other)
    assert apply(p,r)==r


def test_compound_comment_closes_only_when_remaining_constraints_match_verified_project_scope():
    p,r=conditional_fixture()
    p.comment='ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26'
    text='Средний слой основания — Щебеночно-песчаная смесь фр. 0/63 по ГОСТ Р 70458-2022, категории K90, марке M1, h=20 см'
    r['sources'].append(dict(id='pd',kind='project',text=text))
    r['parameter_cards'].append(dict(id='thickness',state='confirmed',source_ids=['pd']))
    r['parameter_proposals']=[dict(parameter='thickness',source_id='pd',scope_state='verified',scope_quote=text)]
    r['items'].append(dict(claim_ids=['attribute:comment:condition'],state='needs_facts',explanation='Не доказан 1,26'))
    out=apply(p,r)
    assert next(i for i in out['items'] if i['claim_ids']==['attribute:comment:condition'])['state']=='confirmed'
    assert next(i for i in out['items'] if i['claim_ids']==['attribute:name:designation:25'])['state']=='needs_facts'
    for extra in (' только для влажной смеси',' с дополнительным условием'):
        other=deepcopy(p)
        other.comment=other.comment.replace('марка М1','марка М1'+extra)
        assert 'attribute:comment:condition' not in accepted_claims(other,r)
    for field,value in [('scope_state','unresolved'),('scope_quote',text.replace('M1','M2'))]:
        other=deepcopy(r)
        other['parameter_proposals'][0][field]=value
        assert 'attribute:comment:condition' not in accepted_claims(p,other)
