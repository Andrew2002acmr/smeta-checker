from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from normative_parameters import build_cards


def fixture(value='1',unit='м3'):
    p=SimpleNamespace(index=0,number='1',volume='12.6',unit='м3',work_name='Материал',
        formula='10*1.26',calculation={},comment='Коэффициент расхода - 1,26',position_source=None)
    quote=f'Коэффициент {value} {unit}'.strip()
    proposal=dict(parameter='coefficient',value=value,unit=unit,source_id='n',quote=quote,conditions=[])
    result: dict[str,Any]=dict(sources=[dict(id='n',kind='normative',origin='native',text=quote,label='Norm')],
        parameter_proposals=[proposal],items=[])
    return p,result


def factor(p,result):
    return next(c for c in build_cards(p,result,{0:p},{}) if c['id']=='factor:0:1.26')


@pytest.mark.parametrize('unit',['м3','т','м2','кг'])
def test_dimensional_consumption_cannot_prove_coefficient_contradiction(unit):
    p,result=fixture(unit=unit)
    before=deepcopy(result)
    card=factor(p,result)
    assert card['state']=='needs_facts' and 'безразмерного' in card['reason']
    assert 'found_value' not in card and 'source_anchors' not in card
    assert result==before


def test_existing_scalar_comparison_behavior_is_preserved():
    p,result=fixture(value='1.3',unit='')
    assert factor(p,result)['state']=='possible_mismatch'


def test_wrong_dimension_exact_number_does_not_displace_scalar_comparison():
    p,result=fixture(value='1.3',unit='')
    bad=dict(parameter='coefficient',value='1.26',unit='м3',source_id='bad',quote='Коэффициент 1.26 м3',conditions=[])
    result['parameter_proposals'].append(bad)
    result['sources'].append(dict(id='bad',kind='normative',origin='native',text=bad['quote'],label='Other'))
    card=factor(p,result)
    assert card['state']=='possible_mismatch' and card['found_value']=='1.3'
