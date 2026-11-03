from copy import deepcopy
from types import SimpleNamespace

from normative_coefficient_policy import apply,accepted_claims,named


def fixture(comment='Купл-1,26',formula='431.61*0.12*1.26',unit='м3',parent_unit='м2'):
    p=SimpleNamespace(index=1,comment=comment,formula=formula,calculation={},unit=unit)
    r=dict(items=[dict(topic='Расход',state='needs_facts',required=True,claim_ids=['derived:rate'],
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
        assert 'value_basis' not in named(other)[0]


def test_consumption_coefficient_generalizes_to_another_value():
    p,r=fixture('Коэффициент расхода - 1,03','0.345*1.03','т','т')
    assert 'derived:rate' in accepted_claims(p,r)
    assert 'basis:comment:consumption:1' in accepted_claims(p,r)


def test_compound_comment_does_not_confirm_its_material_conditions():
    p,r=fixture('ГОСТ Р 70458-2022 категория К90, марка М1\nКупл-1,26')
    assert len(named(p))==1
    assert 'attribute:comment:condition' not in accepted_claims(p,r)


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
