"""A candidate is navigation, never approval of a position's scope."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from normative_agent import EvidenceRequest
from normative_quantity_anchors import quantity_parameter_candidates
from normative_quantity_sum_repair import prepare_quantity_pair, repair_quantity_sum
from test_normative_quantity_sum_repair import setup, fixture, coherent, header
from workspace_models import NormativeParameterProposal


def extra106(tools):
    data=json.loads((Path(__file__).parent/'fixtures/quantity-pair26-extra106.json').read_text())
    source=data['source']
    tools.sources.append(deepcopy(source))
    tools.cells[source['id']]=deepcopy(data['entry'])
    return source['id']


def test_actual26_candidate_is_unresolved_and_third_table_is_not_removed():
    data,tools,answer,kwargs=setup()
    other=extra106(tools)
    original=(deepcopy(answer),deepcopy(tools.sources),deepcopy(tools.cells),deepcopy(tools.index.tables))
    result=prepare_quantity_pair(tools,**kwargs)
    assert result is not None
    assert result['proposal']['value']=='8920.97'
    assert result['proposal']['operation']=='sum'
    assert result['proposal']['scope_state']=='unresolved' and result['scope_verified'] is False
    candidates=quantity_parameter_candidates(tools.sources,tools.cells,tools.quantity_sums)
    assert candidates[0]==result
    assert next(c for c in candidates if c['proposal']['source_id']==other)['proposal']['value']=='106.00'
    assert all(c['proposal']['scope_state']=='unresolved' for c in candidates)
    assert answer==original[0] and tools.sources[:len(original[1])]==original[1]
    assert tools.cells==original[2] and tools.index.tables==original[3]
    assert tools.events[-1]['scope_verified'] is False
    assert tools.events[-1]['physical_group']['row']==41
    frozen=(deepcopy(tools.sources),deepcopy(tools.events),deepcopy(tools.quantity_sums))
    assert prepare_quantity_pair(tools,**kwargs)==result
    assert (tools.sources,tools.events,tools.quantity_sums)==frozen


def test_adding_candidate_does_not_relax_three_proposal_answer_guard():
    _,tools,answer,kwargs=setup()
    other=extra106(tools)
    assert prepare_quantity_pair(tools,**kwargs)
    direct=next(c for c in quantity_parameter_candidates(tools.sources,tools.cells) if c['proposal']['source_id']==other)
    answer.parameters.append(NormativeParameterProposal(**{**direct['proposal'],'scope_state':'verified'}))
    before=deepcopy(answer)
    assert not repair_quantity_sum(answer,tools,**kwargs)
    assert answer==before


def test_actual_agent_receives_unresolved_sum_and_separate106_without_model_call():
    from normative_agent import run
    from normative_position_check import AgentAnswer
    _,tools,_,kwargs=setup()
    extra106(tools)
    assert prepare_quantity_pair(tools,**kwargs)
    tools.coverage={}
    calls=[]

    class Captured(BaseException):
        pass

    def capture(_client,_credentials,_instructions,supplied,_schema,_trace,_stage,_stats):
        candidates=supplied['quantity_parameter_candidates']
        assert candidates[0]['proposal']['operation']=='sum'
        assert candidates[0]['proposal']['value']=='8920.97'
        assert candidates[0]['proposal']['scope_state']=='unresolved'
        assert candidates[0]['scope_verified'] is False
        assert len(candidates[0]['proposal']['operand_source_ids'])==2
        assert any(c['proposal']['value']=='106.00' and c['proposal']['operation']=='direct' for c in candidates)
        calls.append(supplied)
        raise Captured()

    with pytest.raises(Captured):
        run(capture,None,None,'',dict(position={'work':kwargs['owner_work']}),AgentAnswer,None,{},tools)
    assert len(calls)==1


@pytest.mark.parametrize('volume',['9999','0','8921.0','not a quantity'])
def test_declared_quantity_is_not_an_input_to_candidate_selection(volume):
    data=fixture()
    data['position']['volume']=volume
    _,tools,_,kwargs=setup(data)
    assert prepare_quantity_pair(tools,**kwargs)['proposal']['value']=='8920.97'


@pytest.mark.parametrize('mutation',[
    lambda t,k:t.sources[0].update(numeric_verified=False),
    lambda t,k:t.sources[0].update(unit_verified=False),
    lambda t,k:t.sources[0].update(version_id='foreign'),
    lambda t,k:t.sources.append(deepcopy(t.sources[0])),
    lambda t,k:t.sources[0].update(rects=t.sources[1]['rects']),
    lambda t,k:t.cells.pop(t.sources[0]['id']),
    lambda t,k:t.cells[t.sources[0]['id']]['cell'].update(numeric_verified=False),
    lambda t,k:t.cells[t.sources[0]['id']]['cell'].update(quality='uncertain'),
    lambda t,k:t.cells[t.sources[0]['id']].update(version_id='foreign'),
    lambda t,k:t.cells[t.sources[0]['id']].update(table_id='foreign'),
    lambda t,k:t.cells[t.sources[0]['id']].update(page=90),
    lambda t,k:k.update(work_scope='ЩМА-20'),
    lambda t,k:k.update(work_scope='ЩМА-16 ЩМА-20'),
    lambda t,k:k.update(owner_unit='м3'),
    lambda t,k:k.update(owner_work='Устройство покрытия без толщины'),
    lambda t,k:k.update(owner_work=k['owner_work']+' слева'),
    lambda t,k:k.update(owner_work=k['owner_work']+' ПК5+00'),
    lambda t,k:k.update(owner_work=k['owner_work'].replace('h = 5 см','h = 7 см'),height_cm='5'),
    lambda t,k:k.update(section_context='Съезды'),
    lambda t,k:k.update(section_context=''),
])
def test_unverified_or_wrong_owner_evidence_yields_no_candidate(mutation):
    _,tools,_,kwargs=setup()
    mutation(tools,kwargs)
    before=(deepcopy(tools.sources),deepcopy(tools.quantity_sums),deepcopy(tools.events))
    assert prepare_quantity_pair(tools,**kwargs) is None
    assert (tools.sources,tools.quantity_sums,tools.events)==before


@pytest.mark.parametrize('mutation',[
    lambda d:header(d,'cell6').update(text='Слева'),
    lambda d:header(d,'cell6').update(text='Справа ось2'),
    lambda d:header(d,'cell6').update(column=9),
    lambda d:header(d,'cell6').update(colspan=5),
    lambda d:header(d,'cell18').update(text='ЩМА-20 на ПБВ60'),
    lambda d:header(d,'cell30').update(text='h=0,07m'),
    lambda d:header(d,'cell45').update(text='Объем'),
    lambda d:header(d,'cell910').update(bbox=deepcopy(header(d,'cell904')['bbox'])),
    lambda d:header(d,'cell910').update(row_scope={'state':'unresolved','section_id':'other'}),
    lambda d:header(d,'cell9').update(text='А'),
    lambda d:header(d,'cell10').update(text='Б'),
    lambda d:header(d,'cell63').update(text='Участок А м2'),
    lambda d:header(d,'cell69').update(text='Участок Б м2'),
])
def test_coherent_changed_physical_headers_still_fail_closed(mutation):
    data=fixture()
    mutation(data)
    _,tools,_,kwargs=coherent(data)
    assert prepare_quantity_pair(tools,**kwargs) is None


def test_second_complete_physical_pair_is_ambiguous_even_with_identical_values():
    data,tools,_,kwargs=setup()
    table=deepcopy(data['table'])
    table['table_id']='second-physical-table'
    tools.index.tables[data['page']-1].append(table)
    tools.execute(EvidenceRequest(tool='read_table',page=data['page'],table_id=table['table_id'],row_ids=[41],column_ids=[2,8]))
    for source in tools.sources[2:]:
        original=next(s for s in tools.sources[:2] if s['text']==source['text'])
        source['rects']=deepcopy(original['rects'])
    assert len(quantity_parameter_candidates(tools.sources,tools.cells))==4
    assert prepare_quantity_pair(tools,**kwargs) is None
    assert not tools.quantity_sums


def test_no_physical_cells_does_not_derive_a_sum_from_declared_total():
    _,tools,_,kwargs=setup()
    tools.cells={}
    assert prepare_quantity_pair(tools,**kwargs) is None


def test_candidate_is_the_same_when_observed_source_order_is_reversed():
    _,tools,_,kwargs=setup()
    normal=prepare_quantity_pair(tools,**kwargs)
    _,tools,_,kwargs=setup()
    tools.sources.reverse()
    tools.cells=dict(reversed(list(tools.cells.items())))
    reversed_result=prepare_quantity_pair(tools,**kwargs)
    assert reversed_result==normal
