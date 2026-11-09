import json
from copy import deepcopy
from unittest.mock import Mock

from normative_agent import EvidenceRequest,run,MAX_CONTEXT,model_result
from normative_position_check import AgentAnswer
from test_normative_agent import toolkit


def tools_with_readings():
    tools=toolkit()
    header=tools.index.tables[0][0]['cells'][0]
    header.update(text='(5см)',bbox=[0,0,10,10],header_line_observation=dict(
        header_id='h',bbox=[0,0,10,10],state='observed',lines=[dict(order=i,
            rapid_words=[dict(text=f'Покрытие А5Вл строка {i}',confidence=.97,raw_protocol='x'*100000)],
            tesseract_words=[dict(text='Покрытие',conf=90)])for i in range(4)]))
    return tools


def test_observations_reach_actual_model_context_without_raw_protocol_or_rewriting_quote():
    tools=tools_with_readings()
    before=deepcopy(tools.index.tables)
    ask=Mock(side_effect=[AgentAnswer(summary='Read header context',items=[],requests=[
        EvidenceRequest(tool='read_table',table_id='t1',row_ids=[1],column_ids=[1])]),
        AgentAnswer(summary='Uncertain header remains explicit',items=[])])
    run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools)
    supplied=ask.call_args.args[3]
    observation=supplied['tool_results'][0]['result']['header_observations'][0]
    assert observation['lines'][0]['readings'][0]['text']=='Покрытие А5Вл строка 0'
    assert not observation['recognition_verified']
    assert len(json.dumps(supplied,ensure_ascii=False))<MAX_CONTEXT
    assert 'raw_protocol' not in json.dumps(supplied)
    assert tools.index.tables==before
    assert all('А5Вл' not in source['text'] for source in tools.sources)
    assert supplied['sources'][0]['header_observation_addresses']==[dict(header_id='h',row=0,column=1)]


def test_observation_pagination_preserves_exact_address_and_original_source_envelope():
    tools=tools_with_readings()
    first=tools.execute(EvidenceRequest(tool='read_table',table_id='t1',row_ids=[0],column_ids=[1]))
    observation=first['header_observations'][0]
    assert observation['next_line_offset']==2
    sources=deepcopy(tools.sources)
    second=tools.execute(EvidenceRequest(**observation['read_request']))
    assert [line['line']for line in second['header_observations'][0]['lines']]==[2,3]
    assert second['header_observations'][0]['next_line_offset'] is None
    assert tools.sources==sources


def test_discovery_warning_survives_bounded_candidate_preview():
    result=model_result(dict(candidates=[dict(discovery_only=True,scope_verified=False,
        parameter_mismatches=[dict(parameter='thickness',requested_cm='9',observed_cm=['18'],
            requires_normative_transform_check=True)],headers=['x'*10000],values=['10','12'])]),4000)
    candidate=result['candidates'][0]
    assert candidate['discovery_only'] and candidate['scope_verified'] is False
    assert candidate['parameter_mismatches'][0]['requires_normative_transform_check']


def test_leaf_header_observations_precede_long_shared_titles_in_bounded_delivery():
    from normative_header_observations import project
    tools=tools_with_readings()
    table=tools.index.tables[0][0]
    parent=deepcopy(table['cells'][0])
    parent.update(cell_id='caption',row=-1)
    parent['header_line_observation'].update(header_id='caption')
    parent['header_line_observation']['lines'][0]['rapid_words'][0]['text']='Общее название '*100
    table['cells'].insert(0,parent)
    table['cells'][2]['header_ids'].append('caption')
    projected=project(table,[table['cells'][2]],page=1)
    assert [o['header_id']for o in projected]==['h','caption']
    bounded=model_result(dict(header_observations=projected),2200)
    assert bounded['header_observations'][0]['header_id']=='h'
    assert bounded['header_observations'][0]['lines'][0]['readings'][0]['text']=='Покрытие А5Вл строка 0'
