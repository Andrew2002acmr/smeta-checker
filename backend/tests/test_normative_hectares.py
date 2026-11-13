from decimal import Decimal

import pytest

from normative_corpus import Corpus
from normative_parameter_anchors import parameter_candidates, resolve_anchors
from normative_parameters import build_cards, measure, unit_scale_quoted
from workspace_models import NormativePositionCheckItem
from test_normative_parameters import position, proposal, source


def rate(unit='га',scale='1'):
    return dict(entity_kind='norm',attributes={},status='definition_reviewed',
        unit=dict(status='parsed',unit=unit,scale=scale,raw=f'{scale} {unit}'))


def test_hectare_scale_converts_both_directions_without_density_or_rounding():
    corpus=Corpus.__new__(Corpus)
    checks,_=corpus.checks(rate(),dict(unit='м²',quantity='1931'))
    assert checks[0]['state']=='match'
    assert Decimal(checks[0]['norm_quantity'])==Decimal('0.1931')
    checks,_=corpus.checks(rate('м2','100'),dict(unit='га',quantity='0.1931'))
    assert checks[0]['state']=='match'
    assert Decimal(checks[0]['norm_quantity'])==Decimal('19.31')
    assert measure('0.1931','га')==measure('1931','м²')


@pytest.mark.parametrize('unit',['м','м3','т','кг'])
def test_hectare_never_matches_other_dimensions(unit):
    checks,_=Corpus.__new__(Corpus).checks(rate(),dict(unit=unit,quantity='1'))
    assert checks[0]['state']=='mismatch'
    assert 'norm_quantity' not in checks[0]
    assert measure('1','га')[0]!=measure('1',unit)[0]


def test_hectare_norm_measure_needs_its_literal_source():
    assert unit_scale_quoted('1','га','Измеритель: га','Измеритель: га')
    assert not unit_scale_quoted('1','га','Измеритель: га','Измеритель: м2')
    assert not unit_scale_quoted('10000','га','Измеритель: га','Измеритель: га')


def test_literal_project_hectare_is_compared_in_square_metres():
    work=position(volume='1931',unit='м2')
    text='Площадь газона 0,1931 га'
    result=dict(sources=[source(text=text)],parameter_proposals=[
        proposal('quantity','0.1931','га',text)])
    cards=build_cards(work,result,{0:work},{})
    assert next(c for c in cards if c['id']=='quantity')['state']=='confirmed'


def test_selected_literal_hectare_scale_is_available_for_anchor_completion():
    norm=dict(id='n',kind='normative',text='Измеритель: га\nСостав работ: посев газонов')
    candidates=parameter_candidates([norm])
    assert len(candidates)==1 and candidates[0]['value']=='1' and candidates[0]['unit']=='га'
    item=NormativePositionCheckItem(topic='Норма',state='needs_facts',explanation='Проверяется применимость.',
        claim_ids=['normative:applicability'],project_source_ids=[],normative_source_ids=['n'])
    proposals=resolve_anchors([], [norm], [item], owner_unit='м2')
    assert len(proposals)==1 and proposals[0].quote=='Измеритель: га'
    assert proposals[0].value=='1' and proposals[0].unit=='га'
    assert resolve_anchors([], [norm], [item], owner_unit='м3')==[]
    assert resolve_anchors([], [norm], [], owner_unit='м2')==[]
