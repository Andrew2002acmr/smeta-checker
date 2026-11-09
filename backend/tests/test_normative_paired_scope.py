from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import EvidenceTools,EvidenceRequest,model_result,model_source_aliases
from normative_paired_quantities import verified_components,source_id
from normative_paired_scope import observe,validated_parent,preview,scope_issue
from normative_quantity_anchors import quantity_parameter_candidates
from normative_context_packing import pack_sources,serialize_context


def fixture():
    cells=[]
    for row,texts in enumerate((('№ п/п','Наименование работ','Единица измерения','Количество'),
            ('10','Укладка пути в кривых участках','м','62,31'),
            ('10.2','Шпала ШТУ 03.02','шт./т','114/23,53'))):
        for column,text in enumerate(texts):
            cells.append(dict(cell_id=f'c{row}-{column}',role='header'if row==0 else'cell',
                row=row,column=column,colspan=1,text=text,bbox=[column*10,row*10,column*10+10,row*10+10],
                origin='native',header_ids=[]if row==0 else[f'c0-{column}']))
    table=dict(table_id='table',origin='native',row_count=3,col_count=4,cells=cells)
    index=SimpleNamespace(texts=('PDF',),tables=([table],),metadata=dict(document_version_id='v',
        preparation_complete=True,page_geometry={'0':dict(width=40,height=30,matrix=[1,0,0,1,0,0])}),
        pending_ocr=[],errors={},quality={})
    tools=EvidenceTools.from_prepared(index,'v')
    reply=tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='table',row_ids=[2],column_ids=[2,3]))
    pair=reply['row_measurements'][0]
    source=next(s for s in tools.sources if s['id']==pair['source_id'])
    return tools,table,reply,source


def test_parent_is_addressed_navigation_and_survives_model_transport():
    tools,table,reply,source=fixture()
    before=deepcopy(source)
    parent=validated_parent(source)
    assert parent and parent['description']['cell']['text']=='Укладка пути в кривых участках'
    assert verified_components(source)==[dict(value='114',unit='шт'),dict(value='23.53',unit='т')]
    view=reply['row_measurements'][0]['parent_observation']
    assert view['parent_position']=='10'and view['child_position']=='10.2'
    assert view['scope_verified']is False
    request=EvidenceRequest(**view['read_request'])
    result=tools.execute(request)
    assert any(r['text']=='Укладка пути в кривых участках'for r in result['rows'])
    candidates=quantity_parameter_candidates([source],{})
    packed=pack_sources(dict(quantity_parameter_candidates=candidates),[source],limit=12000)
    wire,aliases=model_source_aliases(packed,[source])
    assert len(serialize_context(wire))<=12000
    assert wire['sources'][0]['paired_parent_observation']==view
    assert aliases['s0']==source['id']
    assert model_result(reply,4000)['row_measurements'][0]['parent_observation']==view
    assert source==before
    # Reopening the same source never changes its immutable meaning.
    tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='table',row_ids=[2],column_ids=[2,3]))
    assert len([s for s in tools.sources if s['id']==source['id']])==1


@pytest.mark.parametrize('mutation',['foreign_page','foreign_version','wrong_child','wrong_parent',
    'wrong_parent_row','wrong_bbox','wrong_anchor','invalid_rect','promoted_scope','wrong_binding'])
def test_parent_damage_is_rejected_even_with_a_recomputed_handle(mutation):
    _,_,_,source=fixture()
    source=deepcopy(source)
    context=source['paired_parent_context']
    if mutation=='foreign_page':
        context['parent']['anchor']['page']=2
    if mutation=='foreign_version':
        context['description']['anchor']['version_id']='other'
    if mutation=='wrong_child':
        context['child']['cell']['cell_id']='neighbor'
    if mutation=='wrong_parent':
        context['parent']['cell']['text']='11'
    if mutation=='wrong_parent_row':
        context['parent']['cell']['row']=2
    if mutation=='wrong_bbox':
        context['parent']['cell']['bbox']=[0,0,0,0]
    if mutation=='wrong_anchor':
        context['description']['anchor']['literal_text']='Another work'
    if mutation=='invalid_rect':
        context['child']['anchor']['rects']=[[0,0,2,1]]
    if mutation=='promoted_scope':
        context['scope_verified']=True
    if mutation=='wrong_binding':
        context['binding']['value_cell_id']='neighbor'
    pair=source['paired_measurement_context']
    source['id']=source_id('v',1,'table',pair['unit_cell_id'],pair['value_cell_id'],parent_context=context)
    assert validated_parent(source)is None
    assert not verified_components(source)
    assert not quantity_parameter_candidates([source],{})


@pytest.mark.parametrize('mutation',['duplicate_parent','wrong_description_header','intervening_work','no_parent'])
def test_parent_is_never_guessed_across_ambiguous_or_missing_rows(mutation):
    tools,table,reply,_=fixture()
    table=deepcopy(table)
    if mutation=='duplicate_parent':
        table['cells'].append(deepcopy(table['cells'][4]))
    if mutation=='wrong_description_header':
        table['cells'][5]['header_ids']=[]
    if mutation=='intervening_work':
        table['cells'][8]['row']=3
        table['cells'].append(dict(table['cells'][4],cell_id='intervening',row=2,text='11'))
        for c in table['cells'][9:12]:
            c['row']=3
        reply['row_measurements'][0]['row']=3
    if mutation=='no_parent':
        table['cells'][4]['text']='Unknown'
    assert observe(tools.index,'v',1,table,reply['row_measurements'][0])is None


def test_large_server_proofs_do_not_consume_literal_proposal_budget():
    _,_,_,source=fixture()
    source['paired_measurement_context']['row_cells'][-1]['paired_ocr']={'history':'x'*50000}
    before=deepcopy(source)
    candidates=quantity_parameter_candidates([source],{})
    assert len(candidates)==2
    assert len(serialize_context(candidates))<4000
    assert all(not c['scope_verified']and c['proposal']['scope_state']=='unresolved'for c in candidates)
    assert 'history'not in serialize_context(candidates)
    assert source==before


def test_duplicate_pair_handles_do_not_offer_ambiguous_literal_candidates():
    _,_,_,source=fixture()
    assert not quantity_parameter_candidates([source,deepcopy(source)],{})


def test_legacy_pair_envelope_remains_readable_without_invented_parent_context():
    _,_,_,source=fixture()
    old=deepcopy(source)
    old.pop('paired_parent_context')
    context=old['paired_measurement_context']
    for cell in context['row_cells']:
        cell.pop('colspan',None)
    old['id']=source_id('v',1,'table',context['unit_cell_id'],context['value_cell_id'])
    assert old['id']!=source['id']
    assert verified_components(old)==verified_components(source)
    assert preview(old)is None
    assert len(quantity_parameter_candidates([old],{}))==2


@pytest.mark.parametrize('work,problem',[
    ('Шпала ШТУ 03.01','Обозначение ШТУ'),
    ('Шпала ШТУ 03.02 для прямых участков','другой родительской работе'),
    ('Демонтаж шпал ШТУ 03.02','другой операции'),
    ('Шпала ШТУ 03.02 для кривых участков',None)])
def test_explicit_subject_conflicts_block_even_a_verified_model_claim(work,problem):
    from normative_parameters import build_cards
    from normative_position_check import AgentAnswer,answer_feedback
    from test_normative_parameters import position
    _,_,_,source=fixture()
    candidate=next(c['proposal']for c in quantity_parameter_candidates([source],{})if c['proposal']['unit']=='шт')
    candidate['scope_state']='verified'
    owner=position(volume='114',unit='шт')
    owner.work_name=work
    result=dict(pipeline_version=27,sources=[source],parameter_proposals=[candidate],items=[])
    card=build_cards(owner,result,{owner.index:owner},{})[0]
    tools=SimpleNamespace(parameter_evidence={},events=[])
    answer=AgentAnswer(summary='',items=[],parameters=[candidate])
    feedback=answer_feedback(answer,[source],tools,work_scope=work)
    if problem:
        assert card['state']=='needs_facts'and problem in card['reason']
        assert any(problem in line for line in feedback)
        assert 'quantity'in tools.invalid_project_parameter_kinds
    else:
        assert scope_issue(source,work)is None
        assert card['state']=='confirmed'and not feedback


def test_parent_preview_truncation_has_an_exact_read_request():
    _,_,_,source=fixture()
    source=deepcopy(source)
    description=source['paired_parent_context']['description']
    description['cell']['text']='Название родительской работы. '*40
    description['anchor']['literal_text']=description['cell']['text']
    pair=source['paired_measurement_context']
    source['id']=source_id('v',1,'table',pair['unit_cell_id'],pair['value_cell_id'],
        parent_context=source['paired_parent_context'])
    view=preview(source)
    assert view['excerpt_truncated']and len(view['text'])==450
    assert view['full_length']>450 and view['read_request']['row_ids']==[1]


@pytest.mark.parametrize('parent_text,child_text,work,problem',[
    ('Разборка пути РТ62 эпюра 1680', 'Шпалы железобетонные', 'Шпалы для пути Р65', 'другому обозначению'),
    ('Разборка пути Р65 эпюра 1440', 'Шпалы железобетонные', 'Шпалы для пути РТ62', 'другому обозначению'),
    ('Разборка пути РТ62 эпюра 1680', 'Скрепления для рельсов Р65', 'Скрепления Р65', 'противоречат'),
    ('Разборка пути РТ62 эпюра 1680', 'Шпалы железобетонные', 'Шпалы железобетонные', None),
    ('Разборка пути Р65 эпюра 1440', 'Скрепления для рельсов P65', 'Скрепления Р65', None),
])
def test_rail_designations_preserve_source_contradiction_as_unresolved_scope(parent_text,child_text,work,problem):
    _,_,_,source=fixture()
    context=source['paired_parent_context']
    context['description']['cell']['text']=parent_text
    context['description']['anchor']['literal_text']=parent_text
    pair=source['paired_measurement_context']
    next(c for c in pair['row_cells']if c['column']==1)['text']=child_text
    source['text']=source['literal_text']=' | '.join(c['text']for c in sorted(pair['row_cells'],key=lambda c:c['column']))
    source['id']=source_id('v',1,'table',pair['unit_cell_id'],pair['value_cell_id'],parent_context=context)
    assert len(verified_components(source))==2
    issue=scope_issue(source,work)
    if problem:
        assert problem in issue
    else:
        assert issue is None  # Absence of a conflict is not scope verification.
