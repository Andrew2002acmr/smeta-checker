"""Agent regression tests: generic tables, bounded prompts and technical failures."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from normative_agent import EvidenceRequest, EvidenceTools, compact, run, cell_source_id
from normative_position_check import AgentAnswer
from normative_verdict import publish


def test_large_search_and_table_replies_stay_bounded_without_changing_evidence():
    from normative_agent import model_result, MAX_CONTEXT
    tools=toolkit()
    tools.coverage['available_tables']=[dict(page=i,table_id='drawing') for i in range(80)]
    result=dict(rows=[dict(row=i,column=7,text=str(i),source_id=f'exact:{i}',
        bbox=[0,1,2,3],header_ids=['header']*50) for i in range(90)],
        columns=[dict(column=i,headers=['Header '*100]*10) for i in range(26)],coverage=tools.coverage)
    small=model_result(result,4000)
    assert len(json.dumps(small,ensure_ascii=False))<=4000
    assert small['rows'][0]['source_id']=='exact:0'
    assert len(result['rows'])==90 and 'bbox' in result['rows'][0]
    tools.execute=Mock(return_value=result)
    answer=AgentAnswer(summary='Read',items=[],requests=[EvidenceRequest(tool='read_table')]*4)
    ask=Mock(side_effect=[answer,AgentAnswer(summary='Partial',items=[])])
    run(ask,None,None,'',dict(prepared_quantity_tables=result),AgentAnswer,None,{'queries':[]},tools)
    assert len(json.dumps(ask.call_args.args[3],ensure_ascii=False))<MAX_CONTEXT
    assert len(tools.events)==4 and len(tools.events[0]['result']['rows'])==90


def test_unique_public_source_alias_resolves_but_ambiguous_alias_does_not():
    from normative_position_check import validate_items
    item=dict(topic='Norm',state='confirmed',explanation='Source selected',claim_ids=['normative:applicability'],
        project_source_ids=['p'],normative_source_ids=['public-pdf'])
    answer=AgentAnswer(summary='Check',items=[item])
    sources=[dict(id='p',kind='project'),dict(id='n1',kind='normative',source_id='public-pdf')]
    assert validate_items(answer.items,sources)[0]['normative_source_ids']==['n1']
    sources.append(dict(id='n2',kind='normative',source_id='public-pdf'))
    assert validate_items(answer.items,sources)[0]['state']=='needs_facts'


def test_normative_resources_retain_their_own_page_and_unknown_values():
    from normative_agent import resource_sources
    item=dict(designation='Test norm',code='01',unit_raw='100 м2',extraction_status='extracted',resources=[
        dict(code='M',name='Machine',unit='маш.-ч',raw_quantity='П',quantity=None,
            citations=[dict(page=19,source_id='pdf')])])
    result=resource_sources(item,'n1')
    assert result[0]['page']==19 and result[0]['source_id']=='pdf'
    assert 'Machine' in result[0]['text'] and 'П' in result[0]['text']


def test_short_source_handles_roundtrip_to_exact_cells_not_neighboring_values():
    from normative_agent import model_source_aliases,restore_source_ids
    sources=[dict(id='hashed-left-cell',kind='project'),dict(id='hashed-right-cell',kind='project')]
    sent,aliases=model_source_aliases(dict(sources=sources,quantity_candidates=[dict(operand_source_ids=['hashed-left-cell','hashed-right-cell'])]),sources)
    assert sent['sources'][0]['id']=='s0'
    assert sent['quantity_candidates'][0]['operand_source_ids']==['s0','s1']
    answer=AgentAnswer(summary='Sum',items=[],parameters=[dict(parameter='quantity',value='22',unit='м2',
        operation='sum',operand_source_ids=['s0','s1'],source_id='s0',quote='22')])
    restored=restore_source_ids(answer,aliases)
    assert restored.parameters[0].operand_source_ids==['hashed-left-cell','hashed-right-cell']
    assert sources[0]['id']=='hashed-left-cell'


def test_mechanical_validation_feedback_gets_one_bounded_correction_round():
    tools=toolkit()
    answers=[AgentAnswer(summary='First',items=[]),AgentAnswer(summary='Corrected',items=[])]
    ask=Mock(side_effect=answers)
    verify=Mock(side_effect=[['Unknown source; use exact source handle'],[]])
    result=run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},tools,verify_answer=verify)
    assert result.summary=='Corrected'
    assert 'answer_validation' in ask.call_args.args[3]
    assert ask.call_count==2


def test_transient_connection_failure_uses_remaining_round_not_new_ocr():
    import httpx
    import openai
    error=openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
    tools=toolkit()
    ask=Mock(side_effect=[error,AgentAnswer(summary='Recovered',items=[])])
    stats={'queries':[]}
    assert run(ask,None,None,'',{},AgentAnswer,None,stats,tools).summary=='Recovered'
    assert ask.call_count==2 and stats['warnings']
    ask=Mock(side_effect=error)
    with pytest.raises(openai.APIConnectionError):
        run(ask,None,None,'',{},AgentAnswer,None,{'queries':[]},toolkit())
    assert ask.call_count==3


def test_read_sum_survives_unrelated_answer_repair_but_not_withdrawn_scope():
    from normative_position_check import answer_feedback,retain_literal_parameters
    tools=toolkit()
    tools.execute(EvidenceRequest(tool='read_table',table_id='t1'))
    summed=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=[sid('c1'),sid('c2')],unit='м2'))
    first=AgentAnswer(summary='Read',items=[],parameters=[dict(parameter='quantity',operation='sum',
        operand_source_ids=[sid('c1'),sid('c2')],scope_state='verified',value='69.12',unit='м2',
        source_id=summed['source_id'],quote=summed['equation'],scope_quote=summed['equation'])])
    assert answer_feedback(first,tools.sources,tools)==[]
    final=AgentAnswer(summary='Correct another aspect',items=[],parameters=[dict(parameter='quantity',
        value='69.12',unit='м2',source_id=sid('c1'),quote='invented sum',scope_state='verified')])
    assert retain_literal_parameters(final,tools.sources,tools).parameters[0].operation=='sum'
    source_count=len(tools.sources)
    tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=[sid('c1'),sid('c2')],unit='м2'))
    assert len(tools.sources)==source_count  # One immutable sum, not repeated copies.
    final.parameters[0].scope_state='unresolved'
    final.parameters[0].operation='direct'
    assert retain_literal_parameters(final,tools.sources,tools).parameters[0].operation=='direct'


def test_saved_agent_revalidation_is_local_pinned_and_keeps_original_immutable():
    from copy import deepcopy
    from normative_position_check import validate_agent_saved
    from test_normative_parameters import position,source,proposal
    p=position(volume='10')
    p.work_name='Основание'
    p.legacy_task=lambda: {}
    review=dict(pipeline_version=4,execution_state='complete',state='confirmed',summary='Read',related_positions=[],
        sources=[dict(**source(text='Площадь 10 м2'),version_id='version'),source('n','normative','Норма')],
        parameter_proposals=[dict(**proposal('quantity','10','м2','Площадь 10 м2'),scope_state='verified',scope_quote='Площадь 10 м2')],
        items=[dict(topic='Check',state='confirmed',claim_ids=['quantity','normative:applicability'],
            explanation='Literal',project_source_ids=['p1'],normative_source_ids=['n'])])
    row=dict(id='root',payload=dict(position_key='key',vor_version_id='vor',kb={'release':'pinned'},position_check=review))
    original=deepcopy(row)
    record=dict(id='run',project_id='test',vor_version_id='vor',config={'pdf_versions':{'test.pdf':'version'}},indexed_results=[dict(position_index=0,result={'status':'ОК'})])
    context=SimpleNamespace(repository=Mock())
    context.repository.save_normative_review.side_effect=lambda *args: args[3]
    with patch('workspace_service.inputs_for_run',return_value=[p]),patch('normative_service.position_key',return_value='key'),\
            patch('normative_service.corpus',return_value=SimpleNamespace(identity={'release':'pinned'})),\
            patch('audit_review_policy.review_source',side_effect=lambda raw,task:raw),\
            patch('normative_context._ask',side_effect=AssertionError('No model call')),\
            patch('pdf_text.PDFTextSource',side_effect=AssertionError('No PDF/OCR')):
        output=validate_agent_saved(context,record,0,row,'test')
    assert output['position_check']['state']=='confirmed'
    assert output['position_check']['telemetry']['requests']==0
    assert row==original


def toolkit():
    tools = EvidenceTools.__new__(EvidenceTools)
    tools.sources, tools.cells = [], {}
    tools.events = []
    tools.preferred = []
    tools.table_search_done = False
    tools.snapshot_refs = []
    tools.version, tools.filename = 'pinned-version', 'copy.pdf'
    tools.coverage = dict(state='prepared', pending_ocr_pages=[3], error_pages=[])
    cells = [dict(cell_id='h', row=0, column=1, text='Площадь, м2', role='header', header_ids=[])]
    cells += [dict(cell_id=f'c{i}', row=i, column=1, text=text, role='cell', header_ids=['h'], origin='native')
        for i, text in enumerate(('12,34', '56,78', '90,12'), 1)]
    tools.index = SimpleNamespace(texts=('Описание бетона и таблица площади 9999', 'Кабельная линия 75', ''),
        quality={},
        tables=([dict(table_id='t1', cells=cells, row_count=4, col_count=2)], [], []))
    return tools


def sid(cell):
    return cell_source_id('pinned-version', 1, 't1', cell)


def test_quantity_search_reaches_late_table_beyond_bounded_model_catalogue():
    from test_normative_table_candidates import table
    tools = toolkit()
    frames = [dict(table_id=f'frame{i}',row_count=1,col_count=1,cells=[])
              for i in range(80)]
    late = table()
    tools.index.tables = (frames, [late], [])
    tools.coverage['available_tables'] = [dict(page=1,table_id=t['table_id']) for t in frames]
    candidates = tools.quantity_candidates('Материал Б',unit='м2',height_cm='18')
    assert len(candidates)==1
    assert candidates[0]['page']==2
    assert candidates[0]['calculated_sum']=='22'
    assert candidates[0]['operand_source_ids']


def test_fact_graph_cannot_overflow_model_prompt():
    inputs = dict(matching_checks=[dict(claim_id='quantity', state='review_required', facts=[{'raw': 'x'*1000000}])])
    result = compact(inputs, [dict(id='p1', kind='project', text='x'*20000)])
    assert len(json.dumps(result)) < 10000
    assert result['matching_checks'] == [dict(claim_id='quantity', state='review_required')]
    assert result['sources'][0]['excerpt_truncated']


def test_search_does_not_prioritize_expected_quantity_and_reports_incomplete_ocr():
    tools = toolkit()
    result = tools.execute(EvidenceRequest(tool='search_project', query='кабельная линия 9999'))
    assert result['pages'][0]['page'] == 2
    assert result['coverage']['pending_ocr_pages'] == [3]


def test_generic_table_cells_preserve_headers_and_numeric_provenance():
    tools = toolkit()
    result = tools.execute(EvidenceRequest(tool='read_table', page=1, table_id='t1'))
    assert result['row_count'] == 4
    assert 'Площадь, м2' in tools.sources[1]['text']
    result = tools.execute(EvidenceRequest(tool='sum_cells', operand_ids=[sid('c1'), sid('c2')], unit='м2'))
    assert '69.12 м2' in result['equation']
    assert result['scope_verified'] is False  # Arithmetic is not semantic applicability.
    assert tools.sources[-1]['version_id'] == 'pinned-version'


@pytest.mark.parametrize('ids,unit', [(['invented', sid('c1')], 'м2'),
    ([sid('c1'), sid('c1')], 'м2'), ([sid('c1'), sid('c2')], 'т')])
def test_sum_rejects_fabricated_duplicate_or_incompatible_cells(ids, unit):
    tools = toolkit()
    tools.execute(EvidenceRequest(tool='read_table', table_id='t1'))
    assert 'error' in tools.execute(EvidenceRequest(tool='sum_cells', operand_ids=ids, unit=unit))


def test_agent_acquires_missing_evidence_before_verdict():
    tools = toolkit()
    answers = [AgentAnswer(summary='Нужна таблица', items=[], requests=[EvidenceRequest(tool='read_table', table_id='t1')]),
        AgentAnswer(summary='Недостаточно данных о применимости', items=[])]
    ask = Mock(side_effect=answers)
    stats = {'queries': []}
    result = run(ask, None, None, '', {}, AgentAnswer, None, stats, tools)
    assert result.summary == answers[1].summary
    supplied = ask.call_args.args[3]
    assert supplied['tool_results'][0]['result']['rows'][1]['text'] == '12,34'
    assert len(tools.sources) == 4
    assert ask.call_count == 2


def test_agent_cannot_turn_exhausted_tool_budget_into_a_verdict():
    ask = Mock(return_value=AgentAnswer(summary='Еще поиск', items=[], requests=[EvidenceRequest(tool='read_page')]))
    with pytest.raises(TimeoutError):
        run(ask, None, None, '', {}, AgentAnswer, None, {'queries': []}, toolkit())
    assert ask.call_count == 3


def test_final_open_search_preserves_partial_evidence_without_confirming_verdict():
    item=dict(topic='Площадь',state='confirmed',explanation='Нужен ещё источник единицы.',
        project_source_ids=[],normative_source_ids=[],claim_ids=['quantity'])
    ask=Mock(return_value=AgentAnswer(summary='Частичный результат',items=[item],
        requests=[EvidenceRequest(tool='read_page')]))
    stats={'queries':[]}
    answer=run(ask,None,None,'',{},AgentAnswer,None,stats,toolkit())
    assert ask.call_count==3
    assert answer.items[0].state=='needs_facts'
    assert answer.requests==[]
    assert 'read_page' in stats['missing_facts'][0]


def test_one_remaining_call_uses_prepared_evidence_instead_of_planning_more_reads():
    ask=Mock(return_value=AgentAnswer(summary='Недостаточно подтверждённых данных',items=[]))
    with patch('normative_agent.MAX_ROUNDS',1):
        run(ask,None,None,'Проверить факты.',{},AgentAnswer,None,{'queries':[]},toolkit())
    assert 'Проверить факты.' in ask.call_args.args[2]
    assert 'requests=[] обязательно' in ask.call_args.args[2]


def test_generic_composite_quantity_can_clear_verdict_without_road_repair_rules():
    from normative_parameters import enrich
    from normative_position_check import validate_items
    tools = toolkit()
    tools.sources.append(dict(id='n1', kind='normative', label='Норма', page=1, text='Измеритель м2', origin='native'))
    def answer(*args):
        supplied = args[3]
        step = 2-supplied['remaining_steps']
        if step == 0:
            return AgentAnswer(summary='Читаю исходные площади', items=[], requests=[EvidenceRequest(tool='read_table', table_id='t1')])
        if step == 1:
            return AgentAnswer(summary='Проверяю сумму', items=[], requests=[EvidenceRequest(tool='sum_cells',
                operand_ids=[sid('c1'), sid('c2')], unit='м2')])
        source = next(s for s in supplied['sources'] if s['origin'] == 'calculated_from_cells')
        return AgentAnswer(summary='Площадь рассчитана из двух участков', items=[dict(topic='Площадь', state='confirmed',
            explanation='Сумма двух площадей по таблице.', claim_ids=['quantity', 'normative:applicability'],
            project_source_ids=[source['id']], normative_source_ids=['n1'])], parameters=[dict(parameter='quantity',
                value='69.12', unit='м2', source_id=source['id'], quote='12.34 + 56.78 = 69.12 м2',
                scope_state='verified', scope_quote='12.34 + 56.78 = 69.12 м2')])
    result = run(answer, None, None, '', {}, AgentAnswer, None, {'queries': []}, tools)
    p = SimpleNamespace(index=0, number='1', work_name='Покрытие', volume='69.12', unit='м2', formula='', calculation={}, position_source=None)
    review = enrich(p, dict(pipeline_version=3, position_key='k', sources=tools.sources, summary=result.summary,
        items=validate_items(result.items, tools.sources, {'quantity', 'normative:applicability'}),
        parameter_proposals=[v.model_dump() for v in result.parameters]), {0:p}, {})
    report = publish({'Статус аудита':'Требуется ручная проверка'},
        {'checks':[dict(claim_id='quantity', state='review_required')]}, 'k', {'review':review}, enabled=True)
    assert report['Статус аудита'] == 'ОК'


def test_technical_failure_is_not_engineering_discrepancy_or_manual_ambiguity():
    report = {'Статус аудита': 'Обнаружено расхождение', 'Что не так': 'old'}
    review = dict(position_key='key', execution_state='technical_failure', summary='Истекло время запроса')
    result = publish(report, {}, 'key', dict(review=review, stage='done'), enabled=True)
    assert result['Статус аудита'] == 'Проверка не выполнена'
    assert result['Что не так'] == 'Истекло время запроса'


def test_project_quantity_does_not_require_a_normative_citation_for_the_number_itself():
    from normative_position_check import validate_items
    answer=AgentAnswer(summary='Количество',items=[dict(topic='Количество',state='confirmed',explanation='Число из таблицы ПД.',
        claim_ids=['quantity'],project_source_ids=['p'],normative_source_ids=[])])
    result=validate_items(answer.items,[dict(id='p',kind='project')],{'quantity'})
    assert result[0]['state']=='confirmed'


def test_compact_keeps_independent_ocr_and_physical_unit_verification_flags():
    source=dict(id='cell',kind='project',text='12,34',origin='ocr',numeric_verified=True,unit_verified=True,unit='м2')
    sent=compact({},[source])['sources'][0]
    assert sent['numeric_verified'] and sent['unit_verified'] and sent['unit']=='м2'


def test_early_position_can_read_table_prepared_for_a_sibling_without_reextracting_pdf():
    tools = toolkit()
    tools.blob_store = object()
    tools.snapshot_refs = [dict(snapshot_storage='pages-v1', snapshot_key='manifest', snapshot_sha256='sha')]
    table = dict(table_id='later-table', row_count=1, col_count=1, cells=[])
    manifest = dict(format='page-snapshot-v1', pages=[{'key':'p1'}, {'key':'p2'}])
    with patch('audit_snapshot.load_json', side_effect=[manifest, {'tables':[table]}]) as read:
        result = tools.execute(EvidenceRequest(tool='read_table', page=2, table_id='later-table'))
    assert result['row_count'] == 1
    assert read.call_count == 2
    assert read.call_args.args[1] == {'key':'p2'}
