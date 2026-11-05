from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from normative_agent import EvidenceRequest, EvidenceTools
from normative_parameters import build_cards
from normative_position_check import AgentAnswer, CheckedItem, answer_feedback
from normative_quantity_sum_repair import repair_quantity_sum
from normative_quantity_anchors import quantity_parameter_candidates
from workspace_models import NormativeParameterProposal


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/quantity-sum26-frozen.json').read_text())


def setup(data=None):
    data=deepcopy(data or fixture())
    table=data['table']
    tools=EvidenceTools.__new__(EvidenceTools)
    # Reacquire this fixture's physical cells from its own table snapshot.
    # Negative cases mutate that snapshot before acquisition; retaining the old
    # source registry would test an identity collision instead of scope guards.
    tools.sources=[]
    # The frozen capture predates the explicit empty header-fact list. Keep
    # source identities strict when reacquiring the same physical cells today.
    for source in tools.sources:
        source.setdefault('verified_parameters',[])
    tools.cells={}
    tools.events=[]
    tools.preferred=[]
    tools.quantity_sums=[]
    tools.version=data['sources'][0]['version_id']
    tools.filename=data['sources'][0]['label']
    tools.snapshot_refs=[]
    tables=[[] for _ in range(data['page'])]
    tables[-1]=[table]
    # Original prepared PD89 page geometry (rotated A3), needed to reproduce
    # the captured normalized highlight rectangles rather than erase them.
    geometry=dict(height=841.8897705078125,width=1190.5511474609375,
        matrix=[0.0,-1.0,1.0,0.0,0.0,841.8897705078125])
    tools.index=SimpleNamespace(tables=tables,texts=['']*data['page'],
        metadata=dict(page_geometry={str(data['page']-1):geometry}))
    tools.execute(EvidenceRequest(tool='read_table',page=data['page'],table_id=table['table_id'],row_ids=[41],column_ids=[2,8]))
    answer=AgentAnswer(summary='Frozen live26',items=[],parameters=data['parameters'])
    kwargs=dict(owner_work=data['position']['work_name'],work_scope=data['work_scope'],
        owner_unit=data['position']['unit'],section_context=data['section_context'])
    return data,tools,answer,kwargs


def test_actual26_pair_becomes_exact_sum_with_provenance_and_ordinary_card_validation():
    data,tools,answer,kwargs=setup()
    before=deepcopy(data['table'])
    assert repair_quantity_sum(answer,tools,**kwargs)
    quantities=[p for p in answer.parameters if p.parameter=='quantity']
    assert len(quantities)==1
    proposal=quantities[0]
    assert proposal.operation=='sum' and proposal.value=='8920.97'
    assert proposal.scope_state=='verified' and len(proposal.operand_source_ids)==2
    assert tools.events[-1]['kind']=='repaired_physical_quantity_pair_v1'
    assert len(tools.events[-1]['original_parameters'])==2
    assert tools.events[-1]['physical_group']['row']==41
    assert data['table']==before
    owner=VorPosition.model_validate(data['position'])
    result=dict(sources=tools.sources,items=[],parameter_proposals=[p.model_dump() for p in answer.parameters])
    card=next(c for c in build_cards(owner,result,{owner.index:owner},{}) if c['id']=='quantity')
    assert card['state']=='confirmed' and card['found_value']=='8920.97'
    assert answer.items==[]
    frozen=deepcopy(answer)
    assert not repair_quantity_sum(answer,tools,**kwargs)
    assert answer==frozen


def test_different_vor_total_is_never_used_and_normal_card_still_reports_mismatch():
    data,tools,answer,kwargs=setup()
    data['position']['volume']='9999'
    assert repair_quantity_sum(answer,tools,**kwargs)
    owner=VorPosition.model_validate(data['position'])
    card=build_cards(owner,dict(sources=tools.sources,items=[],parameter_proposals=[p.model_dump() for p in answer.parameters]),{owner.index:owner},{})[0]
    assert card['found_value']=='8920.97' and card['state']=='possible_mismatch'


@pytest.mark.parametrize('mutation',[
    lambda d,t,a,k:a.parameters.pop(0),
    lambda d,t,a,k:a.parameters.append(a.parameters[0].model_copy(deep=True)),
    lambda d,t,a,k:setattr(a.parameters[0],'scope_state','unresolved'),
    lambda d,t,a,k:setattr(a.parameters[0],'value','-4388.24'),
    lambda d,t,a,k:setattr(a.parameters[0],'value','0'),
    lambda d,t,a,k:setattr(a.parameters[0],'unit','м3'),
    lambda d,t,a,k:setattr(a.parameters[0],'quote','Invented4388.24m2'),
    lambda d,t,a,k:setattr(a.parameters[0],'scope_quote','otherlayer4388.24'),
    lambda d,t,a,k:setattr(a.parameters[0],'source_id',a.parameters[1].source_id),
    lambda d,t,a,k:setattr(a.parameters[0],'operation','sum'),
    lambda d,t,a,k:setattr(a.parameters[0],'operand_source_ids',['foreign']),
    lambda d,t,a,k:t.sources[0].update(numeric_verified=False),
    lambda d,t,a,k:t.sources[0].update(unit_verified=False),
    lambda d,t,a,k:t.sources[0].update(version_id='other'),
    lambda d,t,a,k:t.sources[0].update(rects=t.sources[1]['rects']),
    lambda d,t,a,k:t.sources.append(deepcopy(t.sources[0])),
    lambda d,t,a,k:t.cells[a.parameters[0].source_id].update(table_id='other'),
    lambda d,t,a,k:t.cells[a.parameters[0].source_id].update(page=90),
    lambda d,t,a,k:t.cells[a.parameters[0].source_id]['cell'].update(quality='uncertain'),
    lambda d,t,a,k:t.cells[a.parameters[0].source_id]['cell'].update(numeric_verified=False),
    lambda d,t,a,k:t.cells[a.parameters[0].source_id]['cell'].update(row=40),
    lambda d,t,a,k:k.update(section_context='Раздел4. Устройство дорожной одежды. Съезды'),
    lambda d,t,a,k:k.update(section_context=''),
    lambda d,t,a,k:k.update(owner_work=k['owner_work']+' слева'),
    lambda d,t,a,k:k.update(owner_work=k['owner_work']+' правая ось'),
    lambda d,t,a,k:k.update(owner_work=k['owner_work']+' ПК5+00'),
    lambda d,t,a,k:k.update(owner_work=k['owner_work'].replace('h = 5 см','h = 7 см')),
    lambda d,t,a,k:k.update(owner_work=k['owner_work'].replace('h = 5 см','h = 7 см'),height_cm='5'),
    lambda d,t,a,k:k.update(owner_work='Устройство покрытия без толщины'),
    lambda d,t,a,k:k.update(work_scope='ЩМА-20'),
    lambda d,t,a,k:k.update(work_scope='Асфальтобетонная смесь'),
    lambda d,t,a,k:k.update(work_scope=k['work_scope']+' ЩМА-20'),
    lambda d,t,a,k:k.update(owner_unit='м3'),
])
def test_unproven_partial_wrong_scope_or_duplicate_input_never_repaired(mutation):
    data,tools,answer,kwargs=setup()
    mutation(data,tools,answer,kwargs)
    before=deepcopy(answer)
    assert not repair_quantity_sum(answer,tools,**kwargs)
    assert answer==before
    assert not any(e.get('kind')=='repaired_physical_quantity_pair_v1' for e in tools.events)


def header(data,suffix):
    return next(c for c in data['table']['cells'] if c['cell_id'].endswith(':'+suffix))


@pytest.mark.parametrize('mutation',[
    lambda d:header(d,'cell6').update(text='Слева'),
    lambda d:header(d,'cell6').update(text='Справа ось2'),
    lambda d:header(d,'cell6').update(column=9),
    lambda d:header(d,'cell6').update(colspan=5),
    lambda d:header(d,'cell6').update(row=2),
    lambda d:header(d,'cell6').update(header_ids=[]),
    lambda d:header(d,'cell18').update(column=7,colspan=3),
    lambda d:header(d,'cell18').update(text='ЩМА-20 на ПБВ60'),
    lambda d:header(d,'cell30').update(text='h=0,07m'),
    lambda d:header(d,'cell45').update(text='Объем'),
    lambda d:d['table']['cells'].append(deepcopy(header(d,'cell910'))),
    lambda d:header(d,'cell2').update(row=1),
    lambda d:header(d,'cell910').update(row_scope={'state':'literal','section_id':'other'}),
])
def test_physical_header_or_full_group_ambiguity_is_not_repaired(mutation):
    data=fixture()
    mutation(data)
    data,tools,answer,kwargs=setup(data)
    before=deepcopy(answer)
    assert not repair_quantity_sum(answer,tools,**kwargs)
    assert answer==before


def test_shared_text_in_another_physical_header_is_not_shared_ancestry():
    data=fixture()
    duplicate=deepcopy(header(data,'cell2'))
    old=duplicate['cell_id']
    duplicate['cell_id']='different-common-header'
    data['table']['cells'].append(duplicate)
    for c in data['table']['cells']:
        if c['column']>=8:
            c['header_ids']=[duplicate['cell_id'] if h==old else h for h in c.get('header_ids',[])]
    data,tools,answer,kwargs=setup(data)
    assert not repair_quantity_sum(answer,tools,**kwargs)


def test_existing_unrelated_claims_and_nonquantity_parameters_are_unchanged():
    data,tools,answer,kwargs=setup()
    before=[p.model_dump() for p in answer.parameters if p.parameter!='quantity']
    assert repair_quantity_sum(answer,tools,**kwargs)
    assert [p.model_dump() for p in answer.parameters if p.parameter!='quantity']==before


def coherent(data):
    """Regenerate literal reads after adversarial physical-table changes."""
    data,tools,answer,kwargs=setup(data)
    tools.sources=[]
    tools.execute(EvidenceRequest(tool='read_table',page=data['page'],table_id=data['table']['table_id'],row_ids=[41],column_ids=[2,8]))
    # Preserve the independently observed display geometry for these same IDs.
    for source in tools.sources:
        original=next(s for s in data['sources'] if s['id']==source['id'])
        source['rects']=original['rects']
    candidates=quantity_parameter_candidates(tools.sources,tools.cells)
    assert len(candidates)==2
    answer.parameters=[NormativeParameterProposal(**{**c['proposal'],'scope_state':'verified'}) for c in candidates]
    return data,tools,answer,kwargs


@pytest.mark.parametrize('mutation',[
    lambda d:header(d,'cell6').update(text='Слева'),
    lambda d:header(d,'cell6').update(text='Справа ось2'),
    lambda d:header(d,'cell18').update(text='ЩМА-20 на ПБВ60'),
    lambda d:header(d,'cell30').update(text='h=0,07m'),
    lambda d:header(d,'cell27').update(text='h=0,07m'),
    lambda d:header(d,'cell6').update(column=9),
    lambda d:header(d,'cell6').update(colspan=5),
    lambda d:header(d,'cell6').update(row=2),
    lambda d:header(d,'cell18').update(column=7,colspan=3),
    lambda d:header(d,'cell910').update(bbox=deepcopy(header(d,'cell904')['bbox'])),
    lambda d:header(d,'cell910').update(bbox=[107.249755859375,150,119.84979248046875,425.5611572265625]),
    lambda d:header(d,'cell910').update(row_scope={'state':'unresolved','section_id':'other'}),
])
def test_coherent_but_wrong_physical_scope_cannot_pass_by_valid_quotes(mutation):
    data=fixture()
    mutation(data)
    data,tools,answer,kwargs=coherent(data)
    before=deepcopy(answer)
    assert not repair_quantity_sum(answer,tools,**kwargs)
    assert answer==before


def test_coherent_control_is_positive_and_input_order_is_irrelevant():
    data,tools,answer,kwargs=coherent(fixture())
    assert repair_quantity_sum(answer,tools,**kwargs)
    assert answer.parameters[0].value=='8920.97'


@pytest.mark.parametrize('left,right',[('А','Б'),('1','2'),('','Усиление')])
def test_single_character_or_empty_nonunit_leaf_is_not_discarded(left,right):
    data=fixture()
    header(data,'cell9')['text']=left
    header(data,'cell10')['text']=right
    data,tools,answer,kwargs=coherent(data)
    assert not repair_quantity_sum(answer,tools,**kwargs)


@pytest.mark.parametrize('left,right',[('А м2','Б м2'),('Участок А м2','Участок Б м2'),('1 м2','2 м2')])
def test_verified_unit_header_cannot_hide_an_additional_partition_label(left,right):
    data=fixture()
    header(data,'cell63')['text']=left
    header(data,'cell69')['text']=right
    data,tools,answer,kwargs=coherent(data)
    assert not repair_quantity_sum(answer,tools,**kwargs)


def test_real_feedback_hook_requests_model_repair_without_replacing_its_choice(monkeypatch):
    data,tools,answer,kwargs=setup()
    answer.parameters=[p for p in answer.parameters if p.parameter=='quantity']
    answer.items=[CheckedItem(topic='Normative applicability',claim_ids=['normative:applicability'],
        state='needs_facts',explanation='Independent normative condition remains unresolved.',
        project_source_ids=[],normative_source_ids=[])]
    before=deepcopy(answer.items)
    original_execute=tools.execute
    calls=[]
    def execute(request):
        calls.append(request.tool)
        assert request.tool=='sum_cells'
        return original_execute(request)
    monkeypatch.setattr(tools,'execute',execute)
    issues=answer_feedback(answer,tools.sources,tools,owner_unit=kwargs['owner_unit'],
        quantity_work_scope=kwargs['owner_work'],work_scope=kwargs['work_scope'],
        section_context=kwargs['section_context'])
    assert issues and not calls
    assert answer.requests==[] and answer.items==before
    assert len(answer.parameters)==2 and all(p.operation=='direct' for p in answer.parameters)
    assert not any(e.get('kind')=='repaired_physical_quantity_pair_v1' for e in tools.events)
