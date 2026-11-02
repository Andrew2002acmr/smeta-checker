from copy import deepcopy

import pytest

from normative_agent import EvidenceRequest, EvidenceTools, cell_source_id, model_source_aliases
from normative_quantity_anchors import quantity_parameter_candidates
from workspace_models import NormativeParameterProposal


def toolkit(values=('345,77','85,841'), *, origin='ocr'):
    tools=EvidenceTools.__new__(EvidenceTools)
    tools.sources=[]
    tools.cells={}
    tools.quantity_sums=[]
    tools.version='pd-version'
    tools.filename='Проект.pdf'
    ids=[]
    for column,value in enumerate(values,2):
        source_id=cell_source_id(tools.version,89,'t1',f'c{column}')
        header=f'ЩПС, нижний слой h=0,24 м | сторона {column}\nЕдиница физического столбца: м2'
        text=header+'\nВсего: | '+value+'\n'+value
        cell=dict(cell_id=f'c{column}',text=value,row=15,column=column,bbox=[column*30,200,column*30+25,220],
            role='cell',quality='usable',origin=origin,numeric_verified=origin=='ocr')
        tools.cells[source_id]=dict(cell=cell,headers=text,header_text=header,page=89,unit='м2',
            version_id=tools.version,table_id='t1')
        tools.sources.append(dict(id=source_id,kind='project',version_id=tools.version,page=89,label=tools.filename,
            text=text,origin=origin,numeric_verified=origin=='ocr',unit_verified=True,unit='м2'))
        ids.append(source_id)
    return tools,ids


def candidates(tools):
    return quantity_parameter_candidates(tools.sources,tools.cells,tools.quantity_sums)


def test_verified_physical_cell_offers_literal_template_without_engineering_approval():
    tools,ids=toolkit(('2843,00',))
    result=candidates(tools)
    assert len(result)==1
    proposal=NormativeParameterProposal(**result[0]['proposal'])
    assert proposal.value=='2843.00' and proposal.unit=='м2' and proposal.source_id==ids[0]
    assert proposal.scope_state=='unresolved' and result[0]['scope_verified'] is False
    assert proposal.quote=='Единица физического столбца: м2\nВсего: | 2843,00\n2843,00'
    assert proposal.scope_quote==tools.sources[0]['text']
    assert result[0]['provenance']['bbox']==[60,200,85,220]
    assert result[0]['provenance']['cell_id']=='c2'


def test_native_literal_does_not_require_an_ocr_verification_flag():
    tools,_=toolkit(('25',),origin='native')
    assert len(candidates(tools))==1


def test_sum_registry_produces_exact_unrounded_template_with_actual_operands():
    tools,ids=toolkit()
    computed=tools._sum(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit='м2'))
    result=candidates(tools)
    assert result[0]['proposal']['source_id']==computed['source_id']
    proposal=NormativeParameterProposal(**result[0]['proposal'])
    assert proposal.operation=='sum' and proposal.operand_source_ids==ids
    assert proposal.value=='431.611' and proposal.quote=='345.77 + 85.841 = 431.611 м2'
    assert proposal.scope_state=='unresolved' and result[0]['scope_verified'] is False
    assert 'h=0,24 м' in proposal.scope_quote and proposal.quote in proposal.scope_quote
    assert 'Ячейки:' not in proposal.scope_quote
    assert [p['value'] for p in result[0]['provenance']['operands']]==['345.77','85.841']
    aliased,_=model_source_aliases({'quantity_parameter_candidates':result},tools.sources)
    assert aliased['quantity_parameter_candidates'][0]['proposal']['operand_source_ids']==['s0','s1']


@pytest.mark.parametrize('mutation',[
    lambda t,i:t.sources[0].update(numeric_verified=False),
    lambda t,i:t.cells[i[0]]['cell'].update(numeric_verified=False),
    lambda t,i:t.sources[0].update(unit_verified=False),
    lambda t,i:t.cells[i[0]]['cell'].update(quality='uncertain'),
    lambda t,i:t.cells[i[0]]['cell'].update(role='header'),
    lambda t,i:t.cells[i[0]]['cell'].update(bbox=[60,200,59,220]),
    lambda t,i:t.cells[i[0]].update(page=90),
    lambda t,i:t.sources[0].update(version_id=''),
    lambda t,i:t.sources[0].update(text=t.sources[0]['text'].replace('h=0,24','h=0,10')),
    lambda t,i:t.cells[i[0]]['cell'].update(text='2843,00'),
    lambda t,i:t.cells[i[0]]['cell'].update(cell_id='neighbor-cell'),
    lambda t,i:t.cells[i[0]].update(table_id='another-table'),
    lambda t,i:t.sources.append(deepcopy(t.sources[0])),
])
def test_unverified_or_inconsistent_physical_source_has_no_ready_template(mutation):
    tools,ids=toolkit(('25',))
    mutation(tools,ids)
    assert candidates(tools)==[]


@pytest.mark.parametrize('mutation',[
    lambda t,i:t.quantity_sums[0].update(value='431.61'),
    lambda t,i:t.quantity_sums[0].update(operand_source_ids=[i[0],i[0]]),
    lambda t,i:t.quantity_sums[0].update(operand_source_ids=[i[0],'unknown']),
    lambda t,i:t.sources[-1].update(text=t.sources[-1]['text'].replace('0,24','0,10')),
    lambda t,i:t.sources[0].update(version_id='another-pdf'),
    lambda t,i:t.sources[0].update(page=90),
    lambda t,i:t.sources[-1].update(numeric_verified=False),
    lambda t,i:t.quantity_sums[0].update(unit='м3'),
])
def test_sum_refuses_rounding_foreign_operands_or_fabricated_scope(mutation):
    tools,ids=toolkit()
    tools._sum(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit='м2'))
    mutation(tools,ids)
    assert all(c['proposal']['operation']!='sum' for c in candidates(tools))


def test_long_semantic_context_is_not_trimmed_into_a_ready_template():
    tools,ids=toolkit(('25',))
    prefix='Другая конструкция. '*70
    tools.sources[0]['text']=prefix+tools.sources[0]['text']
    tools.cells[ids[0]]['header_text']=prefix+tools.cells[ids[0]]['header_text']
    tools.cells[ids[0]]['headers']=tools.sources[0]['text']
    assert candidates(tools)==[]


def test_same_number_in_different_cells_is_not_merged_or_selected_by_amount():
    tools,ids=toolkit(('25','25'))
    result=candidates(tools)
    assert {c['proposal']['source_id'] for c in result}==set(ids)
    assert len({c['proposal']['scope_quote'] for c in result})==2
    assert all(c['proposal']['scope_state']=='unresolved' for c in result)
    assert quantity_parameter_candidates(tools.sources,tools.cells,max_chars=10)==[]


def test_different_source_ids_cannot_sum_the_same_physical_cell_twice():
    tools,ids=toolkit(('25','25'))
    tools.cells[ids[1]]['cell']['bbox']=deepcopy(tools.cells[ids[0]]['cell']['bbox'])
    tools._sum(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit='м2'))
    assert all(c['proposal']['operation']=='direct' for c in candidates(tools))
