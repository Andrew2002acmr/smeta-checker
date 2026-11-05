"""Formatting repair cannot select a new cell, layer, value or unit."""
from copy import deepcopy

import pytest

from normative_position_check import AgentAnswer, canonicalize_cell_parameters


@pytest.mark.parametrize('change', ['valid','total_row_label','wrong_column','wrong_header','wrong_value','wrong_unit','wrong_pdf','unresolved','unverified'])
def test_sum_scope_uses_only_the_same_verified_physical_operands(change):
    header='Нижний слой основания | Смесь фр. 0/45 | h=0,27м | Площадь, м2'
    a=dict(id='a',kind='project',origin='native',version_id='pdf',page=8,unit='м2',
        text=header+' | Слева\n41.1',rects=[[.1,.1,.2,.2]],numeric_verified=True,unit_verified=True)
    b={**a,'id':'b','text':header+' | Справа\n58.9','rects':[[.3,.1,.4,.2]]}
    text=a['text'].rsplit('\n',1)[0]+'\n'+b['text'].rsplit('\n',1)[0]+'\n41.1 + 58.9 = 100 м2'
    summed={**a,'id':'sum','origin':'calculated_from_cells','text':text,'rects':a['rects']+b['rects']}
    if change=='total_row_label':
        a['text']=a['text'].replace('\n41.1','\nВсего:\n41.1')
        b['text']=b['text'].replace('\n58.9','\nВсего:\n58.9')
    answer=AgentAnswer(summary='Read',items=[],parameters=[dict(parameter='quantity',operation='sum',
        source_id='sum',operand_source_ids=['a','b'],value='100',unit='м2',quote='41.1 + 58.9 = 100 м2',
        scope_quote=header+' | Слева ... '+header+' | Справа ... 41.1 + 58.9 = 100 м2',scope_state='verified')])
    if change=='wrong_column':
        b['rects']=[[.6,.1,.7,.2]]
    if change=='wrong_header':
        b['text']=b['text'].replace('0/45','0/80')
    if change=='wrong_value':
        answer.parameters[0].value='101'
    if change=='wrong_unit':
        answer.parameters[0].unit='т'
    if change=='wrong_pdf':
        b['version_id']='different'
    if change=='unresolved':
        answer.parameters[0].scope_state='unresolved'
    if change=='unverified':
        b['numeric_verified']=False
        b['origin']='ocr'
    original=deepcopy(answer.parameters[0])
    canonicalize_cell_parameters(answer,[a,b,summed],work_scope='Нижний слой основания из смеси фр. 0/45',height_cm='27')
    if change in {'valid','total_row_label'}:
        assert answer.parameters[0].scope_quote==text
        assert answer.parameters[0].value==original.value
        assert answer.parameters[0].operand_source_ids==original.operand_source_ids
    else:
        assert answer.parameters[0]==original


@pytest.mark.parametrize('change',['valid','scope_unit','scope_site'])
def test_sum_header_unit_formatting_does_not_allow_another_unit_or_site(change):
    left='Основание | Слева | Смесь фр. 0/45 | Площадь,'
    right='Основание | Справа | Смесь фр. 0/45 | Площадь,'
    a=dict(id='a',kind='project',origin='native',version_id='pdf',page=8,unit='м2',
        text=left+' |\nЕдиница физического столбца: м2\nИтого:\n4',
        rects=[[.1,.1,.2,.2]],numeric_verified=True,unit_verified=True)
    b={**a,'id':'b','text':a['text'].replace(left,right).replace('\n4','\n6'),'rects':[[.3,.1,.4,.2]]}
    total={**a,'id':'sum','origin':'calculated_from_cells','rects':a['rects']+b['rects'],
        'text':left+' |\nЕдиница физического столбца: м2\n'+right+' |\nЕдиница физического столбца: м2\n4 + 6 = 10 м2'}
    scope=left+' м2 ... '+right+' м2 ... 4 + 6 = 10 м2'
    if change=='scope_unit':
        scope=scope.replace('Площадь, м2','Площадь, т')
    if change=='scope_site':
        scope=scope.replace('Основание','Другой участок')
    answer=AgentAnswer(summary='Read',items=[],parameters=[dict(parameter='quantity',operation='sum',
        source_id='sum',operand_source_ids=['a','b'],value='10',unit='м2',quote='4 + 6 = 10 м2',
        scope_quote=scope,scope_state='verified')])
    canonicalize_cell_parameters(answer,[a,b,total],work_scope='Основание из смеси фр. 0/45')
    assert answer.parameters[0].scope_quote==(total['text'] if change=='valid' else scope)
