from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_addition_basis import addition_basis
from normative_parameters import build_cards


def fixture(expression='108+53', *, volume='161', unit='м', source_unit='м', comment=''):
    work=SimpleNamespace(index=0,number='1',work_name='Установка бортовых камней',position_type='Работа',
        volume=volume,unit=unit,formula=expression,calculation={},comment=comment,
        position_source=SimpleNamespace(row=10))
    text=f'Бортовые камни остановок. Всего 161 {source_unit}'
    result=dict(pipeline_version=4,sources=[dict(id='p',kind='project',origin='native',
        text=text,version_id='pdf',page=92,label='ПД')],
        items=[dict(claim_ids=['geometry:left'],state='needs_facts',required=True)],
        parameter_proposals=[dict(parameter='quantity',value='161',unit=source_unit,source_id='p',
            quote=text,scope_quote=text,scope_state='verified',operation='direct')])
    return work,result


def test_proved_total_closes_arithmetic_addends_but_not_named_project_facts():
    work,result=fixture()
    before=deepcopy(result)
    cards=build_cards(work,result,{0:work},{})
    assert next(c for c in cards if c['id']=='quantity')['state']=='confirmed'
    addends=[c for c in cards if c['id'].startswith('factor:')]
    assert len(addends)==2 and all(c['state']=='confirmed' and c['role']=='calculation' for c in addends)
    assert all(c['source_ids']==['p'] and 'found_value' not in c for c in addends)
    assert result==before and result['items'][0]['state']=='needs_facts'


@pytest.mark.parametrize('expression',['108-(-53)','108+53*1','161/1','SUM(108,53)','A1+53','0+161'])
def test_arithmetic_grouping_proof_rejects_any_other_operation(expression):
    work,_=fixture(expression)
    quantity=dict(state='confirmed',source_ids=['p'],source_anchors=[dict(source_id='p',quote='Всего 161 м')])
    assert addition_basis(work,work,expression,quantity,arithmetic_confirmed=True) is None


@pytest.mark.parametrize('changes',[
    {'expression':'108+54'}, {'source_unit':'м2'}, {'volume':'160'},
])
def test_wrong_arithmetic_total_or_dimension_does_not_confirm_addends(changes):
    work,result=fixture(**changes)
    factors=[c for c in build_cards(work,result,{0:work},{}) if c['id'].startswith('factor:')]
    assert factors and all(c['state']!='confirmed' for c in factors)


def test_missing_quantity_proof_and_material_dependency_remain_unresolved():
    work,result=fixture()
    result['parameter_proposals']=[]
    assert all(c['state']!='confirmed' for c in build_cards(work,result,{0:work},{}) if c['id'].startswith('factor:'))
    quantity=dict(state='confirmed',source_ids=['p'],source_anchors=[dict(source_id='p',quote='Всего 161 м')])
    work.position_type='Материал'
    assert addition_basis(work,work,work.formula,quantity,arithmetic_confirmed=True) is None


def test_explicit_coefficient_is_not_relabelled_as_arithmetic():
    work,result=fixture(comment='Коэффициент 108')
    card=next(c for c in build_cards(work,result,{0:work},{}) if c['id']=='factor:0:108')
    assert card['state']=='needs_facts' and card['role']=='coefficient'
