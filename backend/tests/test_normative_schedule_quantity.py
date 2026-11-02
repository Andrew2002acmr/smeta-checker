from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from normative_agent import EvidenceTools
from normative_parameters import build_cards
from normative_schedule_quantity import identity, project, select, table_scope


def fixture(index=1):
    data = json.loads((Path(__file__).parent / 'fixtures/schedule-layer-v29.json').read_text(encoding='utf-8'))[index]
    position = VorPosition.model_validate(data['position'])
    tools = EvidenceTools.__new__(EvidenceTools)
    for name in ('sources', 'events', 'quantity_sums', 'preferred'):
        setattr(tools, name, [])
    tools.cells = {}
    tools.coverage = {}
    tools.snapshot_refs = []
    tools.version = data['version_id']
    tools.filename = 'project.pdf'
    tables = [[] for _ in range(99)]
    tables[data['page']-1] = [data['table']]
    tools.index = SimpleNamespace(tables=tables, texts=['']*99, metadata={})
    return position, data['result'], tools, data['materials']


def test_full_exit_total_is_bound_by_mix_height_and_row_scope():
    position, result, tools, materials = fixture()
    assert project(position, result, tools, materials)
    q = next(c for c in build_cards(position, result, {position.index: position}, {}) if c['id'] == 'quantity')
    assert q['state'] == 'confirmed' and q['found_value'] == '106.00'
    assert next(i for i in result['items'] if i['claim_ids'] == ['method'])['state'] == 'needs_facts'
    assert result['evidence_trace'][-1]['model_requests'] == 0


def test_selection_never_uses_expected_vor_amount():
    position, result, tools, materials = fixture()
    key = identity(position, materials)
    table = tools.index.tables[89][0]
    first = select([(90, table)], key)
    position.volume = '99999'
    second = select([(90, table)], identity(position, materials))
    assert first == second and first is not None
    assert project(position, result, tools, materials)
    event = result['evidence_trace'][-1]
    assert event['kind'] == 'located_schedule_area_v1' and event['value'] == '106.00'
    assert not any(p['parameter'] == 'quantity' for p in result['parameter_proposals'])
    assert not any(i['state'] == 'possible_mismatch' for i in result['items'])


@pytest.mark.parametrize('change', ['wrong_section', 'operation_stage', 'unverified_number', 'ambiguous', 'wrong_unit', 'scoped_conflict', 'wrong_material'])
def test_unknown_or_conflicting_scope_cannot_confirm(change):
    position, result, tools, materials = fixture()
    if change == 'wrong_section':
        position.position_source.sections[0].text = 'Автобусные остановки'
    elif change == 'operation_stage':
        position.work_name += ' (двухслойного основания - слой1)'
    elif change == 'unverified_number':
        for c in tools.index.tables[89][0]['cells']:
            if c['row'] == 14 and c['column'] == 12:
                c['numeric_verified'] = False
    elif change == 'ambiguous':
        tools.index.tables[88] = deepcopy(tools.index.tables[89])
    elif change == 'wrong_unit':
        table = tools.index.tables[89][0]
        table['column_units'] = {}
        for c in table['cells']:
            if c.get('role') == 'header':
                c['text'] = c['text'].replace('м2', 'м3').replace('м²', 'м³').replace('Площадь', 'Объем')
    elif change == 'scoped_conflict':
        result['parameter_proposals'].append(dict(parameter='quantity', scope_state='verified', source_id='other', value='1'))
    else:
        materials = ['Асфальтобетонная смесь А22Нл']
    before = deepcopy(result)
    assert not project(position, result, tools, materials)
    assert result == before


def test_minimum_layer_selects_full_symmetric_area_and_not_other_height():
    position, result, tools, materials = fixture(0)
    key = identity(position, materials)
    selection = select([(89, tools.index.tables[88][0])], key)
    assert selection is not None
    assert [c['column'] for c in selection[2]] == [6, 12]
    assert [c['text'] for c in selection[2]] == ['2582,95', '2719,86']
    assert project(position, result, tools, materials)
    event = result['evidence_trace'][-1]
    assert event['kind'] == 'located_schedule_area_v1' and event['value'] == '5302.81'
    assert not any(i['state'] == 'possible_mismatch' for i in result['items'])


@pytest.mark.parametrize('change', ['version', 'duplicate', 'minimum'])
def test_street_native_layer_identity_is_not_inferred_from_matching_numbers(change):
    position, result, tools, materials = fixture(0)
    if change == 'version':
        result['sources'][0]['version_id'] = 'another-pdf'
    elif change == 'duplicate':
        result['parameter_proposals'].append(deepcopy(result['parameter_proposals'][0]))
    else:
        position.work_name = position.work_name.replace('hmin', 'h')
    before = deepcopy(result)
    assert not project(position, result, tools, materials)
    assert result == before


def test_ambiguous_literal_section_is_not_replaced_by_guessed_row_scope():
    _, _, tools, _ = fixture()
    table = tools.index.tables[89][0]
    table['cells'].append(dict(cell_id='mixed-section', role='header', row=-1, column=0,
        colspan=table['col_count'], text='Съезды и автобусные остановки'))
    assert table_scope(table) is None
