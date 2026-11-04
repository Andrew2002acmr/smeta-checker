"""Addressed table readings remain available after a later tool batch."""
from copy import deepcopy
from unittest.mock import Mock

from normative_agent import EvidenceRequest, cell_source_id, run
from normative_context_packing import completed_read_ids, pack_sources
from normative_position_check import AgentAnswer
from test_normative_agent import toolkit


def test_table_operands_survive_a_later_page_read_in_the_actual_final_wire():
    tools = toolkit()
    tools.sources = [dict(id=f'old-{i}', kind='normative', origin='native',
        text='Unselected earlier candidate. ' * 100) for i in range(20)]
    tools.preferred = [s['id'] for s in tools.sources]
    before = deepcopy(tools.sources)
    ask = Mock(side_effect=[
        AgentAnswer(summary='Read the exact total cells', items=[], requests=[
            EvidenceRequest(tool='read_table', page=1, table_id='t1', row_ids=[1, 2], column_ids=[1])]),
        AgentAnswer(summary='Read its construction', items=[], requests=[EvidenceRequest(tool='read_page', page=2)]),
        AgentAnswer(summary='Partial evidence only', items=[]),
    ])
    answer = run(ask, None, None, '', dict(protected_metadata='m' * 48000), AgentAnswer, None, {'queries': []}, tools)
    wire = ask.call_args.args[3]
    # The final batch contains only read_page. Its predecessor's precise table
    # evidence must remain delivered too, rather than just archived by handle.
    assert [r['request']['tool'] for r in wire['tool_results']] == ['read_page']
    canonical_to_alias = {s['id']: f's{i}' for i, s in enumerate(tools.sources)}
    delivered = {s['id']: s for s in wire['sources']}
    for cell in ['c1', 'c2']:
        sid = cell_source_id(tools.version, 1, 't1', cell)
        original = next(s for s in tools.sources if s['id'] == sid)
        sent = delivered[canonical_to_alias[sid]]
        assert sent['text'] == original['text']
        assert sent['numeric_verified'] == original['numeric_verified']
        assert not sent['excerpt_truncated']
    assert tools.sources[:20] == before
    assert not answer.items


def test_table_and_sum_receipts_are_navigation_and_unknown_handles_are_not_created():
    events = [dict(request=dict(tool='read_table'), result=dict(rows=[dict(source_id='cell')])),
        dict(request=dict(tool='sum_cells'), result=dict(source_id='sum')),
        dict(request=dict(tool='search_tables'), result=dict(source_id='search-only')),
        dict(request=dict(tool='read_source'), result=dict(error='unknown_source'))]
    assert completed_read_ids(events) == ['sum', 'cell']
    sources = [dict(id='cell', kind='project', text='Unverified column\n12', numeric_verified=False)]
    packed = pack_sources({}, sources, limit=1000, read_source_ids=[*completed_read_ids(events), 'missing'])
    assert [s['id'] for s in packed['sources']] == ['cell']
    assert packed['sources'][0]['numeric_verified'] is False
    assert not packed['archived_source_ids']
