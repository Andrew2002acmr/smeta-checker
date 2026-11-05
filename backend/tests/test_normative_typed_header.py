from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_parameters import build_cards
from normative_layer_scope import technological_part


def test_verified_header_depth_cannot_be_reused_as_a_quantity():
    position=SimpleNamespace(index=0,number='1',volume='25',unit='м2',formula='',calculation={},
        work_name='Устройство основания',position_source=SimpleNamespace(row=1))
    source=dict(id='header',kind='project',origin='ocr_verified_header',numeric_verified=True,
        text='Площадь 25м2, h=25см',verified_parameters=[dict(parameter='thickness',value='25',unit='см')])
    result=dict(pipeline_version=8,sources=[source],items=[],parameter_proposals=[
        dict(parameter='quantity',source_id='header',value='25',unit='м2',quote='Площадь 25м2',
             scope_quote=source['text'],scope_state='verified')])
    card=build_cards(position,result,{0:position},{})[0]
    assert card['state']=='needs_facts'
    assert 'другой параметр' in card['reason']


def fixture():
    position=SimpleNamespace(position_source=SimpleNamespace(sections=[dict(text='Участок Северный')]))
    scope='Участок Северный | ЩПС 0/31,5 | Верхний слой основания (24см)'
    proposal=dict(scope_relation='technological_part',scope_state='verified',scope_quote=scope)
    source=dict(kind='project',version_id='pdf-A',page=1,text=scope,
        verified_parameters=[dict(parameter='thickness',value='24',unit='см')])
    return position,proposal,source,dict(quantity='50',height='24'),'Основание ЩПС 0/31,5'


def test_model_must_explicitly_bind_the_operation_to_the_same_physical_assembly():
    assert technological_part(*fixture())


@pytest.mark.parametrize('change',['unresolved','missing_relation','site','material','depth','missing_proof'])
def test_technological_relation_cannot_erase_a_real_scope_gap(change):
    position,proposal,source,group,work=deepcopy(fixture())
    if change=='unresolved':
        proposal['scope_state']='unresolved'
    if change=='missing_relation':
        proposal['scope_relation']='same_layer'
    if change=='site':
        position.position_source.sections=[dict(text='Участок Южный')]
    if change=='material':
        work='Основание ЩПС 0/63'
    if change=='depth':
        group['height']='30'
    if change=='missing_proof':
        source['verified_parameters']=[]
    assert not technological_part(position,proposal,source,group,work)
