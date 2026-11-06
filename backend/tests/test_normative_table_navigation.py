"""Table continuations preserve source identity and reject substituted addresses."""
from copy import deepcopy

import pytest

from normative_agent import EvidenceRequest, model_result
from test_normative_agent import toolkit


def test_registered_cell_reopens_exact_row_and_returns_structure_without_promoting_binding():
    tools = toolkit()
    first = tools.execute(EvidenceRequest(tool='read_table', table_id='t1', row_ids=[2], column_ids=[1]))
    row = first['rows'][0]
    reply = tools.execute(EvidenceRequest.model_validate(row['read_request']))
    assert [r['source_id'] for r in reply['rows']] == [row['source_id']]
    assert reply['engineering_paths'][0]['row'] == 2
    assert any(gap['link'] == 'fact_to_subject' for gap in reply['engineering_paths'][0]['missing_links'])
    assert 'full_engineering_paths' in reply
    assert 'full_engineering_paths' not in model_result(reply)


@pytest.mark.parametrize('address', [{'page': 2}, {'document_version_id': 'old'}, {'table_id': 'other'}])
def test_registered_cell_cannot_be_redirected(address):
    tools = toolkit()
    first = tools.execute(EvidenceRequest(tool='read_table', table_id='t1', row_ids=[1]))
    source_id = first['rows'][0]['source_id']
    result = tools.execute(EvidenceRequest(tool='read_table', source_id=source_id, **address))
    assert result == {'error': 'table_source_address_conflict'}


def test_sum_requires_all_operand_addresses_from_one_table():
    tools = toolkit()
    first = tools.execute(EvidenceRequest(tool='read_table', table_id='t1', row_ids=[1, 2]))
    ids = [row['source_id'] for row in first['rows']]
    sum_source = dict(id='derived', origin='calculated_from_cells', kind='project',
        page=1, version_id=tools.version)
    tools.sources.append(sum_source)
    tools.quantity_sums = [dict(source_id='derived', operand_source_ids=ids)]
    query = EvidenceRequest(tool='read_table', source_id='derived')
    assert [row['source_id'] for row in tools.execute(query)['rows']] == ids
    altered = deepcopy(next(source for source in tools.sources if source['id'] == ids[0]))
    altered['source_anchor']['table_id'] = 'different'
    tools.sources[tools.sources.index(next(source for source in tools.sources if source['id'] == ids[0]))] = altered
    assert tools.execute(query) == {'error': 'table_source_spans_multiple_tables'}
