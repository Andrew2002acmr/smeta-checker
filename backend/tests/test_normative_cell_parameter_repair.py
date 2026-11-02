"""Repair literal formatting in one cited cell, never select a convenient cell."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_parameters import build_cards
from normative_position_check import AgentAnswer,canonicalize_cell_parameters


def fixture():
    source=dict(id='agent:p92:cell:known',kind='project',origin='ocr',numeric_verified=True,
        unit_verified=True,unit='м',text='Автобусные\nостановки | Бортовой камень\nБР 100.30.18, м\nЕдиница физического столбца: м\nВсего: | \n161')
    proposal=dict(parameter='quantity',operation='direct',value='161',unit='м',source_id=source['id'],
        quote='Автобусные остановки | Бортовой камень БР 100.30.18, м ... Всего: | 161',
        scope_quote='Автобусные остановки | Бортовой камень БР 100.30.18, м ... Всего: | 161',
        scope_state='verified',operand_source_ids=[source['id']])
    answer=AgentAnswer(summary='Literal cell',items=[],parameters=[proposal])
    return answer,source


def test_same_cell_exact_value_and_verified_scope_survive_ellipsis_and_whitespace():
    answer,source=fixture()
    original=deepcopy(source)
    canonicalize_cell_parameters(answer,[source],work_scope='Установка бортового камня')
    proposal=answer.parameters[0]
    assert proposal.source_id==source['id'] and proposal.value=='161'
    assert proposal.quote==proposal.scope_quote==source['text']
    assert proposal.scope_state=='verified' and proposal.operand_source_ids==[]
    position=SimpleNamespace(index=0,number='1',work_name='Установка бортового камня',volume='161',unit='м',
        formula='',calculation={},position_source=None)
    result=dict(pipeline_version=4,sources=[source],items=[],parameter_proposals=[proposal.model_dump()])
    assert next(c for c in build_cards(position,result,{0:position},{}) if c['id']=='quantity')['state']=='confirmed'
    assert source==original


@pytest.mark.parametrize('change',[
    lambda a,s:setattr(a.parameters[0],'scope_state','unresolved'),
    lambda a,s:setattr(a.parameters[0],'source_id','another-cell'),
    lambda a,s:setattr(a.parameters[0],'value','160'),
    lambda a,s:setattr(a.parameters[0],'unit','м2'),
    lambda a,s:setattr(a.parameters[0],'scope_quote','Проезжая часть ... Всего: | 161'),
    lambda a,s:setattr(a.parameters[0],'scope_quote','161'),
    lambda a,s:setattr(a.parameters[0],'operand_source_ids',['another-cell']),
    lambda a,s:s.update(kind='normative'),
    lambda a,s:s.update(numeric_verified=False),
    lambda a,s:s.update(unit_verified=False),
    lambda a,s:s.update(origin='uncertain_table'),
    lambda a,s:s.update(text=s['text']+' 2163'),
    lambda a,s:s.update(text=s['text']+'\n161.001'),
])
def test_wrong_cell_value_unit_scope_or_unverified_source_is_never_repaired(change):
    answer,source=fixture()
    change(answer,source)
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[source],work_scope='Установка бортового камня')
    assert answer.model_dump()==before


def test_equal_value_in_another_source_cannot_repair_a_prose_citation():
    answer,cell=fixture()
    prose=dict(id='p50',kind='project',origin='native',text='Описание конструкции без количества.')
    answer.parameters[0].source_id=prose['id']
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[prose,cell])
    assert answer.model_dump()==before


def test_literal_scope_of_a_different_material_is_not_canonicalized():
    answer,source=fixture()
    source['text']='Бетон тяжелый h=20 см\nЕдиница физического столбца: м3\nВсего:\n161'
    source['unit']='м3'
    proposal=answer.parameters[0]
    proposal.unit='м3'
    proposal.scope_quote='Бетон тяжелый h=20 см ... Всего:\n161'
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[source],work_scope='ЩПС 0/31,5',height_cm='20')
    assert answer.model_dump()==before


def test_unknown_abbreviated_material_cannot_stand_for_a_known_vor_family():
    answer,source=fixture()
    source['text']='А22НТ на БНД70/100 h=0,20m\nЕдиница физического столбца: м2\nВсего:\n161'
    source['unit']='м2'
    proposal=answer.parameters[0]
    proposal.unit='м2'
    proposal.scope_quote='А22НТ на БНД70/100 h=0,20m ... Всего:\n161'
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[source],work_scope='Устройство основания ЩПС 0/31,5 h=20 см',height_cm='20')
    assert answer.model_dump()==before


@pytest.mark.parametrize('work,section',[
    ('Установка бортового камня слева',''),
    ('Установка бортового камня','Раздел. Правая ось'),
    ('Установка бортового камня ПК 5+20 - ПК 6+00',''),
    ('Установка бортового камня','Раздел. Устройство тротуаров'),
])
def test_narrow_partition_or_different_section_is_not_inferred_by_quote_repair(work,section):
    answer,source=fixture()
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[source],work_scope=work,section_context=section)
    assert answer.model_dump()==before


@pytest.mark.parametrize('work,section',[
    ('Установка бортового камня на тротуарах','Раздел. Тротуары'),
    ('Установка бортового камня','Автомобильная дорога. Раздел. Тротуары'),
])
def test_common_project_or_work_words_do_not_erase_the_section_constraint(work,section):
    answer,source=fixture()
    source['text']='Автомобильная дорога. '+source['text']
    before=answer.model_dump()
    canonicalize_cell_parameters(answer,[source],work_scope=work,section_context=section)
    assert answer.model_dump()==before


@pytest.mark.parametrize('section,expected',[
    ('Раздел. Устройство автобусных остановок','confirmed'),
    ('Раздел. Устройство тротуаров','needs_facts'),
])
def test_frozen_replay_uses_identical_cell_and_section_guards_without_mutating_evidence(section,expected):
    from audit_models import SourceHeading
    from test_normative_frozen_evidence import _fixture,_invoke
    position,row,record,context=_fixture()
    answer,cell=fixture()
    position=position.model_copy(update={'work_name':'Установка бортового камня','volume':'161','unit':'м',
        'position_source':position.position_source.model_copy(update={'sections':[SourceHeading(address='A10',text=section)]})})
    result=row['payload']['position_check']
    result['sources']=[dict(**cell,version_id='pdf')]
    result['items']=[]
    result['parameter_proposals']=[p.model_dump() for p in answer.parameters]
    original=deepcopy(row)
    saved=_invoke(position,row,record,context)['payload']['position_check']
    quantity=next(card for card in saved['parameter_cards'] if card['id']=='quantity')
    assert quantity['state']==expected
    assert saved['telemetry']['requests']==0
    assert saved['sources']==original['payload']['position_check']['sources']
    assert row==original
    assert context.repository.save_normative_review.call_count==1
