"""Preserve structural provenance and require independent native scope evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import EvidenceTools
from normative_engineering_evidence import (
    compact_evidence_path,
    engineering_path_links,
    resolve_native_geometry,
    table_evidence_path,
)
from normative_source_addresses import cell_source_id


VERSION = 'document-version'


def table_and_cell():
    header = dict(cell_id='t:h', row=0, column=1, colspan=2, text='Конструкция II', role='header')
    divider = dict(cell_id='t:d', row=1, column=0, text='Правая сторона', role='group')
    label = dict(cell_id='t:l', row=2, column=0, rowspan=2, text='ПК 2+50', role='cell')
    cell = dict(cell_id='t:c', row=3, column=2, text='12', header_ids=['t:h'],
                bbox=[3, 4, 5, 6], row_scope=dict(state='literal', section_id='t:d'),
                numeric_verified=True, role='cell')
    table = dict(table_id='t', cells=[header, divider, label, cell],
                 row_sections=[dict(cell_id='t:d', text=divider['text'], start_row=2, end_row=4)])
    return table, cell


def evidence_path(table, cell, **changes):
    anchor = dict(version_id=VERSION, page=4, table_id='t', cell_id='t:c',
                  state='located', bbox=cell['bbox'])
    kwargs = dict(version=VERSION, page=4, source_id=cell_source_id(VERSION, 4, 't', 't:c'),
                  source_anchor=anchor, unit='м2')
    return table_evidence_path(table, cell, **(kwargs | changes))


def test_path_contains_header_section_and_merged_label_without_engineering_promotion():
    table, cell = table_and_cell()
    path = evidence_path(table, cell)
    assert path is not None
    nodes = {node['kind']: node for node in path['nodes']}
    assert nodes['cell']['header_ids'] == [nodes['header']['id']]
    assert nodes['row']['parent_id'] == nodes['row_section']['id']
    assert nodes['row_label']['text'] == 'ПК 2+50'
    assert nodes['fact']['state'] == 'literal_verified'
    assert {edge['link'] for edge in path['missing_links']} == {
        'fact_to_subject', 'subject_to_vor_position',
    }
    assert compact_evidence_path(path)['navigation_only'] is True
    assert evidence_path(table, cell, source_id=cell_source_id('other', 4, 't', 't:c')) is None


def test_wrong_header_column_and_invalid_section_interval_remain_missing():
    table, cell = table_and_cell()
    table['cells'][0]['colspan'] = 1
    table['row_sections'][0]['end_row'] = 3
    path = evidence_path(table, cell, unit='')
    assert path is not None
    assert {'invalid_header_ancestry', 'invalid_section_interval', 'no_verified_unit'} <= {
        item['reason'] for item in path['missing_links']
    }
    assert path['nodes'][-1]['state'] == 'unresolved'


def test_exact_ledger_route_is_projected_only_for_its_vor_position():
    graph = dict(
        entities=[dict(id='e', kind='Evidence', source_id='physical'),
                  dict(id='f', kind='Fact', fact={'parameter': 'quantity'}),
                  dict(id='s', kind='Work'), dict(id='w', kind='Work', position_index=3)],
        relations=[dict(id='sum', kind='DERIVED_FROM', source='f', target='e', state='verified'),
                   dict(id='scope', kind='HAS_QUANTITY', source='s', target='f', state='verified'),
                   dict(id='vor', kind='CORRESPONDS_TO', source='w', target='s', state='verified')],
    )
    assert engineering_path_links(graph, 'physical', 3)[0]['evidence_role'] == 'operand'
    assert engineering_path_links(graph, 'physical', 4) == []
    graph['relations'][-1]['state'] = 'candidate'
    assert engineering_path_links(graph, 'physical', 3) == []


def native_tools(texts, *, origin='native'):
    tools = EvidenceTools.__new__(EvidenceTools)
    tools._reset([], None, {})
    tools.version = VERSION
    tools.index = SimpleNamespace(texts=texts, tables=[[]] * len(texts),
                                  quality={i: dict(origin=origin) for i in range(len(texts))},
                                  locators=None)
    return tools


def native_work(title, details):
    return SimpleNamespace(index=12, position_type='Работа', work_name=f'Устройство {title} {details}',
                           position_source={'sections': [{'text': f'Раздел 3. Устройство {title}.'}]})


@pytest.mark.parametrize(('title', 'body', 'details'), [
    ('газонов', 'Газоны устраиваются рядом с дорогой. Наносится плодородный слой почвы\n'
                'толщиной 0, 20 м и сеются многолетние травы.',
     'с внесением земли h = 25 см и посевом многолетних трав'),
    ('пандусов', 'Пандусы предназначены для аварийной эвакуации. Защитный слой покрытия '
                 'толщиной 20 см устраивается по проекту.',
     'для аварийной эвакуации, h = 25 см'),
])
def test_native_named_section_reads_actual_thickness_even_if_declared_value_differs(title, body, details):
    work = native_work(title, details)
    heading = 'Газоны' if title == 'газонов' else 'Пандусы'
    tools = native_tools([f'_{heading}_\n{body}\n_Другой раздел_\nТекст.'])
    result = dict(sources=[], parameter_proposals=[])
    assert resolve_native_geometry(work, {work.index: work}, tools, result)
    proof = result['parameter_proposals'][0]
    assert proof['value'] in {'0.20', '20'}
    assert proof['scope_state'] == 'verified'
    assert result['evidence_trace'][-1]['expected_value_used_for_selection'] is False


@pytest.mark.parametrize('reason', ['ocr', 'competing_scope', 'competing_work', 'range', 'same_number_only',
                                   'alternative_layer', 'qualified_work', 'material_conflict'])
def test_native_binding_rejects_wrong_or_ambiguous_scope(reason):
    body = '_Газоны_\nГазоны создаются. Наносится слой почвы толщиной 20 см, сеются многолетние травы.\n_Дороги_\nТекст.'
    work = native_work('газонов', 'с внесением почвы h = 20 см и посевом многолетних трав')
    positions = {work.index: work}
    texts = [body]
    if reason == 'competing_scope':
        texts.append(body.replace('_Газоны_', '_Газоны второй очереди_'))
    if reason == 'competing_work':
        sibling = deepcopy(work)
        sibling.index += 1
        positions[sibling.index] = sibling
    if reason == 'range':
        texts[0] = body.replace('20 см', '20 см или 25 см')
    if reason == 'same_number_only':
        texts[0] = body.replace('многолетние травы', 'однолетние растения')
    if reason == 'alternative_layer':
        texts[0] = body.replace('_Дороги_', 'Другой слой hmin=30 см.\n_Дороги_')
    if reason == 'qualified_work':
        work.work_name += ' только на первом этапе'
    if reason == 'material_conflict':
        work.work_name = work.work_name.replace('с внесением почвы', 'с материалом «песок»')
        texts[0] = body.replace('слой почвы', 'слой материала «щебень»')
    tools = native_tools(texts, origin='ocr' if reason == 'ocr' else 'native')
    result = dict(sources=[], parameter_proposals=[])
    assert not resolve_native_geometry(work, positions, tools, result)
    assert result['parameter_proposals'] == []
