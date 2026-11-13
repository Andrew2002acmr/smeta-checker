from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import EvidenceTools,EvidenceRequest,model_result
from normative_paired_quantities import verified_components
from normative_quantity_anchors import quantity_parameter_candidates
from normative_parameter_feedback import literal_parameter_issues
from normative_parameters import build_cards
from workspace_models import NormativeParameterProposal


def toolkit():
    from test_pdf_row_measurements import table
    grid=table()
    grid.update(row_count=2,col_count=4)
    for cell in grid['cells']:
        cell.update(bbox=[cell['column']*10,cell['row']*10,cell['column']*10+10,cell['row']*10+10],origin='native')
    grid['cells'].append(dict(cell_id='work',role='cell',row=1,column=1,text='Шпала ШТУ 03.02',
        bbox=[0,10,20,20],origin='native',header_ids=[]))
    index=SimpleNamespace(texts=('PDF',),tables=([grid],),metadata=dict(document_version_id='v',
        preparation_complete=True,page_geometry={'0':dict(width=40,height=20,matrix=[1,0,0,1,0,0])}),
        pending_ocr=[],errors={},quality={})
    tools=EvidenceTools.from_prepared(index,'v')
    reply=tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='t',row_ids=[1],column_ids=[2,3]))
    pair=reply['row_measurements'][0]
    source=next(s for s in tools.sources if s['id']==pair['source_id'])
    return tools,reply,source


def test_literal_components_and_two_source_regions_reach_the_agent():
    tools,reply,source=toolkit()
    assert source['text']=='Шпала ШТУ 03.02 | шт./т | 114/23,53'
    assert len(source['rects'])==2
    assert verified_components(source)==[dict(value='114',unit='шт'),dict(value='23.53',unit='т')]
    candidates=quantity_parameter_candidates(tools.sources,tools.cells)
    assert {(c['proposal']['value'],c['proposal']['unit'])for c in candidates}=={('114','шт'),('23.53','т')}
    assert all(not c['scope_verified']and c['proposal']['scope_state']=='unresolved'for c in candidates)
    assert model_result(reply,4000)['row_measurements'][0]['source_id']==source['id']
    for candidate in candidates:
        assert not literal_parameter_issues(NormativeParameterProposal(**candidate['proposal']),source)


@pytest.mark.parametrize('mutation',['swap_unit','wrong_page','wrong_version','wrong_literal',
    'wrong_cell','ocr','missing_anchor','invalid_rect','different_valid_bbox'])
def test_invalid_envelope_never_creates_literal_templates(mutation):
    tools,_,source=toolkit()
    source=deepcopy(source)
    if mutation=='swap_unit':
        source['paired_measurement_context']['row_cells'][0]['text']='т/шт.'
    if mutation=='wrong_page':
        source['page']=2
    if mutation=='wrong_version':
        source['version_id']='other'
    if mutation=='wrong_literal':
        source['text']=source['text'].replace('114','115')
    if mutation=='wrong_cell':
        source['paired_measurement_context']['source_anchors'][1]['cell_id']='neighbor'
    if mutation=='ocr':
        source['origin']='ocr'
    if mutation=='missing_anchor':
        source['paired_measurement_context']['source_anchors'].pop()
    if mutation=='invalid_rect':
        source['paired_measurement_context']['source_anchors'][1]['rects']=[[0,0,2,1]]
    if mutation=='different_valid_bbox':
        source['paired_measurement_context']['source_anchors'][1]['bbox']=[0,0,5,5]
    assert not verified_components(source)
    assert not quantity_parameter_candidates([source],tools.cells)


def test_wrong_number_unit_pair_is_rejected_by_feedback_and_result_cards():
    _,_,source=toolkit()
    proposal=dict(parameter='quantity',value='114',unit='т',source_id=source['id'],
        quote=source['text'],scope_quote=source['text'],scope_state='verified')
    assert 'не образуют одну пару' in literal_parameter_issues(NormativeParameterProposal(**proposal),source)[0]
    from test_normative_parameters import position
    owner=position(volume='114',unit='шт')
    owner.work_name='Шпала ШТУ 03.02'
    card=build_cards(owner,dict(sources=[source],parameter_proposals=[proposal]),{0:owner},{})[0]
    assert card['state']=='needs_facts'and 'не образуют одну пару'in card['reason']
