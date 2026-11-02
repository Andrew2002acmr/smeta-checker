from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_quantity_scope import partial_side_issue
from normative_parameters import build_cards
from normative_position_check import AgentAnswer,answer_feedback
from test_normative_parameters import position


def pair():
    def source(side,value):
        return dict(id='agent:p89:cell:'+side,kind='project',page=89,version_id='pin',
            text=f'Покрытие | {side} | Усиление | А22Нт | h=0,08m | Площадь |\nСтрока итога: Всего:\nЕдиница физического столбца: м2\nВсего:\n{value}',
            rects=[[.1,.8,.2,.9] if side=='Слева' else [.3,.8,.4,.9]],
            unit='м2',unit_verified=True,numeric_verified=True,origin='ocr')
    return [source('Слева','2582,95'),source('Справа','2719,86')]


def proposal(sources):
    source=sources[1]
    return dict(parameter='quantity',operation='direct',operand_source_ids=[],source_id=source['id'],
        value='2719.86',unit='м2',quote='Единица физического столбца: м2\nВсего:\n2719,86',
        scope_quote=source['text'],scope_state='verified',conditions=[])


@pytest.mark.parametrize('volume',['5303','2719.86'])
def test_readable_one_side_neither_confirms_nor_rejects_unspecified_whole_work(volume):
    sources=pair()
    p=position(volume=volume)
    p.work_name='Устройство выравнивающего слоя А22Нт h=8 см'
    result=dict(pipeline_version=4,sources=sources,parameter_proposals=[proposal(sources)],items=[])
    original=deepcopy(result)
    card=build_cards(p,result,{0:p},{})[0]
    assert card['state']=='needs_facts' and 'одна сторона' in card['reason']
    assert 'found_value' not in card and result==original


@pytest.mark.parametrize('work,section',[('Работа справа',''),('Работа','Правая сторона'),('Правой части дороги','')])
def test_explicit_owner_side_keeps_existing_comparison(work,section):
    sources=pair()
    assert partial_side_issue(proposal(sources),sources,work,section) is None


@pytest.mark.parametrize('work',['Работа слева','Правая и левая сторона','Левый поворот'])
def test_other_or_mixed_scope_does_not_authorize_right_column(work):
    sources=pair()
    assert partial_side_issue(proposal(sources),sources,work)


@pytest.mark.parametrize('changed',[
    dict(page=88),dict(version_id='different'),dict(unit='м3'),dict(unit_verified=False),
    dict(numeric_verified=False),dict(text='Other material | Слева\n5'),
    dict(rects=[]),dict(rects=[[.1,.2,.2,.3]]),dict(rects=[[.3,.8,.4,.9]])])
def test_unrelated_or_unverified_other_cell_cannot_establish_comparable_pair(changed):
    sources=pair()
    sources[0].update(changed)
    assert partial_side_issue(proposal(sources),sources) is None


def test_sum_or_native_whole_quantity_is_not_a_partial_side_cell():
    sources=pair()
    candidate=proposal(sources)
    candidate['operation']='sum'
    assert partial_side_issue(candidate,sources) is None
    candidate['operation']='direct'
    sources[1]['id']='page-paragraph'
    candidate['source_id']='page-paragraph'
    assert partial_side_issue(candidate,sources) is None


def test_feedback_reports_pair_before_paid_final_answer_without_changing_values():
    sources=pair()
    candidate=AgentAnswer(summary='',items=[],parameters=[proposal(sources)])
    tools=SimpleNamespace(parameter_evidence={'quantity':dict(stale=True)})
    original=deepcopy(candidate)
    issues=answer_feedback(candidate,sources,tools,work_scope='Выравнивающий слой')
    assert len(issues)==1 and 'обе колонки' in issues[0]
    assert tools.parameter_evidence=={} and tools.invalid_project_parameter_kinds=={'quantity'}
    assert all(getattr(candidate.parameters[0],key)==getattr(original.parameters[0],key)
        for key in ('source_id','value','unit','scope_state','operation'))


def test_child_quantity_uses_parent_scope_not_material_name():
    sources=pair()
    parent=position()
    parent.work_name='Выравнивающий слой А22Нт h=8 см'
    child=position(1,'100','т')
    child.work_name='А22Нт справа (название материала не задаёт участок работы)'
    result=dict(pipeline_version=4,sources=sources,parameter_proposals=[proposal(sources)],items=[])
    assert 'одна сторона' in build_cards(child,result,{0:parent,1:child},{'parent':{'index':0}})[0]['reason']


def test_feedback_uses_owner_scope_not_unrelated_child_side_word():
    sources=pair()
    answer=AgentAnswer(summary='',items=[],parameters=[proposal(sources)])
    issues=answer_feedback(answer,sources,SimpleNamespace(),work_scope='Материал справа',quantity_work_scope='Покрытие')
    assert any('одна сторона' in issue for issue in issues)


@pytest.mark.parametrize('full_height,operation,expected',[
    (None,'Работа h=12 см (двухслойного основания - слой2)',True),
    (24,'Работа h=12 см (двухслойного основания - слой2)',False),
    (None,'Работа h=22 см (двухслойного основания - слой2)',False),
    (None,'Работа h=12 см',False)])
def test_unestablished_group_cannot_compare_full_layer_to_one_operation(full_height,operation,expected):
    from normative_quantity_scope import unresolved_layer_issue
    source=pair()[0]
    source['text']=source['text'].replace('0,08m','0,22m')
    assert bool(unresolved_layer_issue(dict(parameter='quantity'),source,operation,full_height))==expected
