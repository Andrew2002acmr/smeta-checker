from copy import deepcopy
from types import SimpleNamespace

from normative_formula_scale import scale_operand


def fixture():
    work=SimpleNamespace(index=1,unit='т')
    owner=SimpleNamespace(index=0,volume='874.42',unit='м2')
    source=dict(id='n',kind='normative',origin='native',text='Измеритель: 1000 м2',label='ГЭСН 27')
    card=dict(id='unit_scale:n',state='confirmed',source_ids=['n'],found_value='1000',found_unit='м2',
        quote='Измеритель: 1000 м2')
    result=dict(sources=[source],items=[dict(claim_ids=['normative:applicability'],state='needs_facts',
        normative_source_ids=['n'])],parameter_cards=[card])
    return work,owner,result


def test_scale_is_independent_of_unproven_rate_and_norm_conditions():
    work,owner,result=fixture()
    original=deepcopy(result)
    proof=scale_operand(work,owner,result,'1000','874.42/1000*197.12',arithmetic_confirmed=True)
    assert proof['state']=='confirmed' and proof['role']=='calculation'
    assert result==original


def test_wrong_area_units_source_or_arithmetic_cannot_supply_scale():
    work,owner,result=fixture()
    assert scale_operand(work,owner,result,'1000','8887/1000*197.12',arithmetic_confirmed=True) is None
    assert scale_operand(work,owner,result,'1000','874.42/1000*197.12',arithmetic_confirmed=False) is None
    result['sources'][0]['text']='Измеритель: 100 м2'
    assert scale_operand(work,owner,result,'1000','874.42/1000*197.12',arithmetic_confirmed=True) is None
    result['sources'][0]['text']='Измеритель: 1000 м3'
    assert scale_operand(work,owner,result,'1000','874.42/1000*197.12',arithmetic_confirmed=True) is None
