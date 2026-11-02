"""Complete embedded quotes retain conditions while freeing source space."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from normative_agent import MAX_CONTEXT, compact, model_source_aliases, run
from normative_embedded_context import literal_paths, packing_record
from normative_context_packing import serialize_context
from normative_position_check import AgentAnswer
from test_normative_agent import toolkit


def fixture():
    pin=dict(profile='roads',release_id='pinned',sha256='a'*64)
    quote='Буквальное условие. '*100+'Не применять при толщине свыше 8 см.'
    source=dict(id='exact',kind='normative',label='Reviewed clause',page=3,
        origin='native',text=quote,kb_identity=pin)
    inputs=dict(collection_context=dict(kb=deepcopy(pin),literal_chunks=[dict(source_id='exact',quote=quote)]))
    return inputs,[source]


def test_full_same_pin_literal_is_kept_once_without_mutating_its_registry():
    inputs,sources=fixture()
    original=deepcopy((inputs,sources))
    result=compact(inputs,sources,['exact'])
    brief=result['sources'][0]
    assert brief['text_embedded_at']=='collection_context.literal_chunks[0].quote'
    assert brief['embedded_text_complete'] is True and brief['next_offset'] is None
    assert len(brief['text'])==160 and brief['excerpt_truncated'] is True
    assert result['collection_context']['literal_chunks'][0]['quote']==sources[0]['text']
    assert result['collection_context']['literal_chunks'][0]['quote'].endswith('свыше 8 см.')
    assert (inputs,sources)==original
    assert json.dumps(result,ensure_ascii=False).count(sources[0]['text'])==1
    aliased,_=model_source_aliases(result,sources)
    assert aliased['sources'][0]['id']==aliased['collection_context']['literal_chunks'][0]['source_id']=='s0'
    assert aliased['sources'][0]['text_embedded_at']==brief['text_embedded_at']


@pytest.mark.parametrize('change',['partial','paraphrase','wrong_id','wrong_pin','missing_pin',
    'project_source','unknown_context','conflicting_quote','duplicate_source_id','nonstring_quote'])
def test_no_deduplication_for_partial_ambiguous_or_different_sources(change):
    inputs,sources=fixture()
    chunk=inputs['collection_context']['literal_chunks'][0]
    if change=='partial':
        chunk['quote']=chunk['quote'][:-20]
    elif change=='paraphrase':
        chunk['quote']=chunk['quote'].replace('8 см','80 мм')
    elif change=='wrong_id':
        chunk['source_id']='other'
    elif change=='wrong_pin':
        inputs['collection_context']['kb']['release_id']='other'
    elif change=='missing_pin':
        inputs['collection_context'].pop('kb')
    elif change=='project_source':
        sources[0]['kind']='project'
    elif change=='unknown_context':
        inputs['untrusted_summary']=inputs.pop('collection_context')
    elif change=='conflicting_quote':
        inputs['collection_context']['literal_chunks'].append(dict(source_id='exact',quote='Different condition'))
    elif change=='duplicate_source_id':
        sources.append({**sources[0],'text':'A different source sharing an ID'})
    elif change=='nonstring_quote':
        chunk['quote']=['not literal text']
    assert literal_paths(inputs,sources)=={}
    if change=='duplicate_source_id':
        with pytest.raises(ValueError,match='duplicate identities'):
            compact(inputs,sources,['exact'])
        return
    assert all('embedded_text_complete' not in source for source in compact(inputs,sources,['exact'])['sources'])


def test_deduplication_keeps_work_norms_and_resources_in_a_crowded_input():
    pin=dict(profile='roads',release_id='pinned')
    inputs=dict(position_notes='x'*17000)
    sources=[]
    for context_name,count in [('methodology_context',3),('manufacturer_context',2)]:
        chunks=[]
        for i in range(count):
            sid=f'{context_name}:{i}'
            text=sid+' '+('Буквальный текст с условиями. '*90)
            sources.append(dict(id=sid,kind='normative',label=sid,page=i+1,origin='native',text=text,kb_identity=pin))
            chunks.append(dict(source_id=sid,quote=text))
        inputs[context_name]=dict(kb=pin,literal_clauses=chunks)
    for sid in ['work-base','work-resources','technical-condition']:
        sources.append(dict(id=sid,kind='normative',label=sid,page=20,origin='native',text=(sid+' ')*25))
    preferred=[source['id'] for source in sources[:5]]
    result=compact(inputs,sources,preferred)
    assert len(json.dumps(result,ensure_ascii=False))<=MAX_CONTEXT
    assert {'work-base','work-resources','technical-condition'}<={source['id'] for source in result['sources']}
    assert len(literal_paths(result,sources))==5
    for source in result['sources']:
        if source['id'].startswith(('work-','technical-')):
            original=next(s for s in sources if s['id']==source['id'])
            assert source['text']==original['text']


def test_packing_diagnostics_distinguish_full_embedded_sources_from_omissions():
    inputs,sources=fixture()
    sources.append(dict(id='omitted',kind='normative',text='Not sent'))
    report=packing_record({**inputs,'sources':[]},sources,'check')
    assert report['embedded_full_source_ids']==['exact']
    assert report['omitted_normative_source_ids']==['omitted']


def test_packing_diagnostics_are_saved_but_never_fed_back_into_model_context():
    tools=toolkit()
    ask=Mock(side_effect=[AgentAnswer(summary='First',items=[]),AgentAnswer(summary='Second',items=[])])
    verify=Mock(side_effect=[['Needs one correction'],[]])
    run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,verify_answer=verify)
    packing=tools.coverage['model_context_packing']
    assert len(packing)==2
    for call,record in zip(ask.call_args_list,packing,strict=True):
        supplied=call.args[3]
        assert 'model_context_packing' not in supplied['coverage']
        assert record['input_json_characters']==len(serialize_context(supplied))
        assert record['input_json_characters']<=MAX_CONTEXT
