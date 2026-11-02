"""Known arithmetic is delivered before the model; construction scope is not."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import compact, model_source_aliases
from normative_parameters import build_cards
from test_normative_agent import toolkit
from test_normative_table_candidates import table


def prepared():
    tools = toolkit()
    value = table()
    tools.index.tables = ([value], [], [])
    cells = {cell['cell_id']:cell for cell in value['cells']}
    cells['v0']['text'] = '345,77'
    cells['v2']['text'] = '85,841'
    return tools, cells


def test_exact_group_gets_real_sum_source_but_no_automatic_scope_confirmation():
    tools, cells = prepared()
    original = deepcopy(cells)
    candidate, = tools.quantity_candidates('Материал Б',unit='м2',height_cm='18')
    assert candidate['calculated_sum'] == '431.611'
    assert candidate['equation'] == '345.77 + 85.841 = 431.611 м2.'
    assert candidate['scope_verified'] is False
    source = next(s for s in tools.sources if s['id']==candidate['source_id'])
    assert source['origin']=='calculated_from_cells' and source['numeric_verified']
    assert candidate['equation'] in source['text']
    assert len(candidate['operand_source_ids'])==2
    assert cells == original  # No expected VOR quantity was used to select/read cells.
    assert tools.events[-1]['kind']=='prepared_quantity_sum'
    supplied, aliases = model_source_aliases(compact({'quantity_candidates':[candidate]},tools.sources,tools.preferred),tools.sources)
    public = supplied['quantity_candidates'][0]
    assert aliases[public['source_id']] == source['id']
    assert [aliases[sid] for sid in public['operand_source_ids']] == candidate['operand_source_ids']
    assert any(s['id']==public['source_id'] and candidate['equation'] in s['text'] for s in supplied['sources'])
    position = SimpleNamespace(index=0,work_name='Материал Б',volume='431.61',unit='м2',
        position_source=None,formula='',calculation={},number='1')
    proposal = dict(parameter='quantity',operation='sum',value=candidate['calculated_sum'],unit='м2',
        source_id=source['id'],operand_source_ids=candidate['operand_source_ids'],
        quote=candidate['equation'],scope_quote=source['text'],scope_state='unresolved')
    result = dict(pipeline_version=4,sources=tools.sources,items=[],parameter_proposals=[proposal])
    quantity = next(c for c in build_cards(position,result,{0:position},{}) if c['id']=='quantity')
    assert quantity['state']=='needs_facts'
    assert 'Принадлежность' in quantity['reason']


@pytest.mark.parametrize('change',[
    lambda cells: cells['v0'].update(numeric_verified=False),
    lambda cells: cells['v2'].update(origin='ocr',quality='uncertain'),
    lambda cells: cells['u0'].update(text='Площадь'),
    lambda cells: cells['m2'].update(text='Материал Б h=0,20 м'),
    lambda cells: cells['m2'].update(text='Материал Б'),
    lambda cells: cells['m2'].update(text='Материал В h=0,18 м'),
])
def test_uncertain_units_numbers_or_different_scope_never_get_a_prepared_sum(change):
    tools, cells = prepared()
    change(cells)
    candidates = tools.quantity_candidates('Материал Б',unit='м2',height_cm='18')
    assert all('source_id' not in c for c in candidates)
    assert not any(s.get('origin')=='calculated_from_cells' for s in tools.sources)


def test_vor_section_prefers_corresponding_literal_table_context_without_quantity_matching():
    tools,cells=prepared()
    street=tools.index.tables[0][0]
    street['cells'].append(dict(cell_id='street-title',role='header',row=0,column=0,colspan=4,text='Проезжая часть. Улица'))
    sidewalk=deepcopy(street)
    sidewalk['table_id']='sidewalk'
    sidewalk['cells'][-1]['text']='Тротуары, тех.тротуары и лестничные марши'
    for cell in sidewalk['cells']:
        if cell['cell_id']=='v0':
            cell['text']='2'
        if cell['cell_id']=='v2':
            cell['text']='3'
    tools.index.tables=([street],[sidewalk],[])
    candidates=tools.quantity_candidates('Материал Б',unit='м2',height_cm='18',
        section_context='Раздел 2. Устройство пешеходных и технических тротуаров и лестничных маршей')
    assert candidates[0]['page']==2 and candidates[0]['table_id']=='sidewalk'
    assert candidates[0]['calculated_sum']=='5'  # Ranking does not use either total.
    assert candidates[0]['section_match_terms'] and not candidates[0]['scope_verified']


def test_generic_street_caption_cannot_outrank_exact_material_and_thickness():
    tools,cells=prepared()
    exact=tools.index.tables[0][0]
    unrelated=deepcopy(exact)
    unrelated['table_id']='lawn'
    unrelated['cells'].append(dict(cell_id='title',role='header',row=0,column=0,colspan=4,
        text='Газон. Улица'))
    for cell in unrelated['cells']:
        if cell['cell_id'] in {'m0','m2'}:
            cell['text']='Газон'
        if cell.get('header_ids'):
            cell['header_ids']=['title',*cell['header_ids']]
        if cell['cell_id']=='v0':
            cell['text']='1000'
    tools.index.tables=([unrelated],[exact],[])
    before=deepcopy(tools.index.tables)
    candidates=tools.quantity_candidates('Материал Б',unit='м2',height_cm='18',section_context='Улица')
    assert candidates[0]['table_id']=='t' and candidates[0]['page']==2
    assert candidates[0]['calculated_sum']=='431.611'
    assert candidates[0]['section_match_terms']==[]
    assert any(c['table_id']=='lawn' and c['section_match_terms'] for c in candidates)
    assert all(c['scope_verified'] is False for c in candidates)
    assert tools.index.tables==before
