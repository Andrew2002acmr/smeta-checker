"""Only a pinned live thickness proof survives mixing with an unrelated child rate."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from audit_models import VorPosition
from normative_agent import run
from normative_geometry_checkpoint import EVENT, reconcile_geometry
from normative_parameters import build_cards
from normative_position_check import AgentAnswer,validate_items


CLAIM='geometry:comment:thickness:1'


def fixture():
    position=VorPosition(index=0,work_name='Устройство нижнего слоя основания из ЩПС h=12 см (двухслойного основания - слой1)',
        position_type='Работа',volume='100',unit='м2')
    related=[dict(index=2,row=20,work=position.work_name.replace('слой1','слой2'),quantity='100',unit='м2')]
    relation=dict(parent=None,children=[dict(material_index=1,work='ЩПС 0/31,5')])
    text='Нижний слой основания — ЩПС фр. 0/31,5, h=24 см'
    sources=[dict(id='p',kind='project',origin='native',version_id='pinned-pdf',page=50,text=text,rects=[]),
        dict(id='n',kind='normative',origin='native',source_id='norm-pdf',page=2,text='При толщине более 18 см нормировать в два слоя.')]
    geometry=dict(topic='Толщина проектной конструкции',state='confirmed',claim_ids=[CLAIM],
        explanation='Полная толщина24 см; два технологических слоя по12 см совпадают с полной проектной толщиной24 см.',
        project_source_ids=['p'],normative_source_ids=['n'])
    parameter=dict(parameter='thickness',value='24',unit='см',source_id='p',quote='h=24 см',
        scope_quote=text,scope_state='verified')
    first=AgentAnswer(summary='Separated proof',items=[geometry],parameters=[parameter])
    mixed={**geometry,'state':'needs_facts','claim_ids':[CLAIM,'basis:comment:consumption:2','related:1:rate'],
        'explanation':'Полная толщина24 см; два слоя по12 см дают24 см — противоречия нет. Коэффициент1,26 дочернего материала не подтверждён.'}
    final=AgentAnswer(summary='Mixed final answer',items=[mixed],parameters=[parameter])
    tools=SimpleNamespace(events=[])
    return position,{0:position},relation,related,sources,tools,first,final


def apply(data,answer):
    position,positions,relation,related,sources,tools,*_=data
    reconcile_geometry(answer,sources,tools,position,positions,relation,related)


def thickness(data,answer):
    position,positions,relation,related,sources,*_=data
    result=dict(pipeline_version=4,sources=sources,items=validate_items(answer.items,sources),
        parameter_proposals=[p.model_dump() for p in answer.parameters],related_positions=related)
    return next(c for c in build_cards(position,result,positions,relation) if c['id']=='thickness')


def test_separately_confirmed_live_geometry_survives_only_unrelated_child_gap():
    data=fixture()
    tools,first,final=data[5:]
    untouched=deepcopy(final)
    apply(data,first)
    assert tools.geometry_checkpoint
    assert thickness(data,final)['state']=='needs_facts'
    apply(data,final)
    assert thickness(data,final)['state']=='confirmed'
    geometry=next(item for item in final.items if CLAIM in item.claim_ids)
    residual=next(item for item in final.items if 'related:1:rate' in item.claim_ids)
    assert geometry==first.items[0]
    assert residual.state=='needs_facts' and 'basis:comment:consumption:2' in residual.claim_ids
    assert final.parameters==untouched.parameters
    assert tools.events[-1]['kind']==EVENT and tools.events[-1]['original_item']==untouched.items[0].model_dump()
    assert all('normative:applicability' not in item.claim_ids for item in final.items)


def test_frozen_or_legacy_mixed_claim_without_live_checkpoint_cannot_be_promoted():
    data=fixture()
    original=deepcopy(data[-1])
    apply(data,data[-1])
    assert data[-1]==original and not getattr(data[5],'geometry_checkpoint',None)
    assert thickness(data,data[-1])['state']=='needs_facts'


@pytest.mark.parametrize('change',['source_version','source_text','source_page','scope_unresolved','parameter_value',
    'different_scope','different_layer','new_geometry_source','independent_uncertainty','possible_mismatch',
    'explicit_geometry_doubt','unrelated_child','without_child'])
def test_changed_or_genuinely_uncertain_geometry_revokes_previous_confirmation(change):
    data=fixture()
    position,_,_,related,sources,tools,first,final=data
    apply(data,first)
    if change=='source_version':
        sources[0]['version_id']='other-pdf'
    elif change=='source_text':
        sources[0]['text']+=' Другой участок.'
    elif change=='source_page':
        sources[0]['page']=51
    elif change=='scope_unresolved':
        final.parameters[0].scope_state='unresolved'
    elif change=='parameter_value':
        final.parameters[0].value='22'
    elif change=='different_scope':
        final.parameters[0].scope_quote='Верхний слой h=24 см'
        final.parameters[0].quote='Верхний слой h=24 см'
    elif change=='different_layer':
        related[0]['work']=position.work_name.replace('12 см','10 см').replace('слой1','слой2')
    elif change=='new_geometry_source':
        sources.append({**sources[0],'id':'other','text':'Другая конструкция h=24 см'})
        final.items[0].project_source_ids.append('other')
    elif change=='independent_uncertainty':
        final.items.append(final.items[0].model_copy(update={'claim_ids':['geometry:section'],
            'explanation':'Принадлежность конструкции участку не подтверждена.'}))
    elif change=='possible_mismatch':
        final.items[0].state='possible_mismatch'
    elif change=='explicit_geometry_doubt':
        final.items[0].explanation='Толщина24 см найдена, но принадлежность конструкции участку не подтверждена. Коэффициент1,26 не подтверждён.'
    elif change=='unrelated_child':
        final.items[0].claim_ids=[CLAIM,'related:99:rate']
    else:
        final.items[0].claim_ids=[CLAIM,'basis:comment:consumption:2']
    original=deepcopy(final)
    apply(data,final)
    assert final==original and not tools.events


def test_new_independent_geometry_uncertainty_cannot_be_revived_by_later_mixing():
    data=fixture()
    apply(data,data[-2])
    withdrawn=data[-2].model_copy(deep=True)
    withdrawn.items[0].state='needs_facts'
    apply(data,withdrawn)
    assert data[5].geometry_checkpoint is None
    original=deepcopy(data[-1])
    apply(data,data[-1])
    assert data[-1]==original and not data[5].events


def test_previously_unverified_numeric_source_cannot_seed_checkpoint():
    data=fixture()
    data[4][0]['origin']='ocr_unverified'
    apply(data,data[-2])
    assert data[5].geometry_checkpoint is None
    apply(data,data[-1])
    assert not data[5].events


def test_eight_item_dto_limit_is_respected_without_discarding_other_obligations():
    data=fixture()
    apply(data,data[-2])
    final=data[-1]
    final.items.extend(final.items[0].model_copy(update={'claim_ids':[f'other:{i}']}) for i in range(7))
    original=deepcopy(final)
    apply(data,final)
    assert final==original and len(final.items)==8 and not data[5].events


def test_final_prompt_allows_dto_eight_items_and_keeps_distinct_scopes():
    tools=SimpleNamespace(sources=[],preferred=[],events=[],coverage={},table_search_done=True)
    answer=AgentAnswer(summary='Answer',items=[])
    ask=Mock(side_effect=[answer.model_copy(deep=True) for _ in range(3)])
    run(ask,None,None,'',{},AgentAnswer,None,{},tools,verify_answer=lambda answer:['Separate evidence'])
    prompt=ask.call_args.args[2]
    assert 'до 8 items' in prompt and 'до 5 items' not in prompt
    assert 'Не объединяй подтверждённую геометрию' in prompt
