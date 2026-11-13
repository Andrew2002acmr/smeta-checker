"""Body OCR agreement cannot certify a separate unverified unit header."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_agent import EvidenceTools, EvidenceRequest
from normative_parameter_feedback import literal_parameter_issues
from normative_parameters import build_cards
from normative_parent_basis import _proposal_dependencies
from normative_fact_ledger import build
from normative_confirmed_units import confirm, attach
from normative_source_identity import shared_source
from normative_source_roles import ocr_cell_quantity_unit_supported
from workspace_models import NormativeParameterProposal
from test_normative_parameters import position
from test_confirmed_source_units import Repository


def fixture(unit='м2'):
    cells = []
    for row, texts in enumerate((('Наименование работ', 'Количество, ' + unit), ('Устройство основания', '100'))):
        for column, text in enumerate(texts):
            cells.append(dict(cell_id=f'c{row}-{column}', role='header' if row == 0 else 'cell',
                row=row, column=column, colspan=1, rowspan=1, text=text,
                bbox=[column*100,row*20,column*100+100,row*20+20], origin='ocr', quality='usable',
                numeric_verified=row == 1 and column == 1, header_ids=[] if row == 0 else [f'c0-{column}']))
    table = dict(table_id='t1', origin='ocr', row_count=2, col_count=2, cells=cells)
    index = SimpleNamespace(texts=('Prepared OCR',), tables=([table],), pending_ocr=[], errors={}, quality={},
        metadata=dict(document_version_id='pdf', preparation_complete=True,
            page_geometry={'0':dict(width=200,height=40,matrix=[1,0,0,1,0,0])}))
    tools = EvidenceTools.from_prepared(index, 'pdf')
    tools.execute(EvidenceRequest(tool='read_table',page=1,table_id='t1',row_ids=[1],column_ids=[1]))
    source = next(s for s in tools.sources if ':cell:' in s['id'])
    assert source['source_anchor']['state'] == 'located'
    assert source['numeric_verified'] is True and source['unit_verified'] is False
    proposal = NormativeParameterProposal(parameter='quantity',value='100',unit=unit,source_id=source['id'],
        quote=source['text'],scope_quote=source['text'],scope_state='verified')
    p = position(volume='100',unit=unit)
    p.work_name = 'Устройство основания'
    return p, source, proposal


def checked(p, source, proposal):
    result = dict(pipeline_version=27,sources=[source],parameter_proposals=[proposal.model_dump()],items=[])
    result['parameter_cards'] = build_cards(p,result,{0:p},{})
    return result['parameter_cards'][0], build(p,result,{0:p},{})['facts'][0]


def test_real_read_table_shape_keeps_raw_number_without_confirming_ocr_header_unit():
    p, source, proposal = fixture()
    frozen = deepcopy(source)
    assert literal_parameter_issues(proposal,source)
    card, fact = checked(p,source,proposal)
    assert card['state'] == 'needs_facts' and card['found_value'] == '100'
    assert fact['value'] == '100' and fact['unit'] == 'м2'
    assert fact['literal_state'] == fact['subject_binding_state'] == 'unresolved'
    assert fact['comparison_state'] == 'needs_facts'
    assert not _proposal_dependencies(proposal.model_dump(),{source['id']:source})
    assert source == frozen


@pytest.mark.parametrize('physical_unit', ['см', 'мм', 'км'])
def test_same_dimension_with_different_scale_is_not_an_acceptable_unit_proof(physical_unit):
    p, source, proposal = fixture('м')
    source.update(unit_verified=True,unit=physical_unit)
    assert literal_parameter_issues(proposal,source)
    card, fact = checked(p,source,proposal)
    assert card['state'] == 'needs_facts'
    assert fact['comparison_state'] == 'needs_facts'
    assert not _proposal_dependencies(proposal.model_dump(),{source['id']:source})


def test_exact_verified_column_unit_supports_literal_binding_and_comparison():
    p, source, proposal = fixture()
    source.update(unit_verified=True,unit='м2')
    assert not literal_parameter_issues(proposal,source)
    card, fact = checked(p,source,proposal)
    assert card['state'] == 'confirmed'
    assert fact['literal_state'] == fact['subject_binding_state'] == 'verified'
    assert _proposal_dependencies(proposal.model_dump(),{source['id']:source}) == [source['id']]


def expert(source):
    context = SimpleNamespace(repository=Repository())
    record = dict(project_id='project',config=dict(pdf_versions={'PD':'pdf'}))
    confirm(context,record,source,'м2','synthetic-unit-reviewer','Explicit unit confirmation in the isolated contract test')
    return attach(context,record,dict(sources=[source]))['sources'][0]


@pytest.mark.parametrize('alias', [False, True])
def test_existing_strict_expert_unit_context_is_a_valid_alternative_to_column_proof(alias):
    p, source, proposal = fixture()
    source = expert(source)
    if alias:
        source = shared_source(source, 7)
        proposal.source_id = source['id']
    assert source['unit_verified'] is False
    assert not literal_parameter_issues(proposal,source)
    card, fact = checked(p,source,proposal)
    assert card['state'] == 'confirmed'
    assert fact['literal_state'] == fact['subject_binding_state'] == 'verified'


def test_mutated_expert_context_cannot_override_the_unverified_column():
    p, source, proposal = fixture()
    source = expert(source)
    source['confirmed_unit_context']['binding']['text'] = 'another physical cell'
    assert literal_parameter_issues(proposal,source)
    card, fact = checked(p,source,proposal)
    assert card['state'] == 'needs_facts'
    assert fact['literal_state'] == fact['subject_binding_state'] == 'unresolved'


@pytest.mark.parametrize('case', ['paragraph', 'native'])
def test_gate_leaves_ocr_paragraphs_and_native_cells_under_their_existing_contracts(case):
    p, source, proposal = fixture()
    if case == 'paragraph':
        source['id'] = 'prepared:ocr:paragraph'
        proposal.source_id = source['id']
    else:
        source['origin'] = 'native'
    assert ocr_cell_quantity_unit_supported(proposal.model_dump(),source)
    assert not literal_parameter_issues(proposal,source)
    assert checked(p,source,proposal)[0]['state'] == 'confirmed'


@pytest.mark.parametrize('namespace', ['shared', 'work'])
def test_unit_gate_uses_validated_canonical_cell_id_through_transport_wrappers(namespace):
    p, original, proposal = fixture()
    source = shared_source(original, 3, namespace=namespace)
    proposal.source_id = source['id']
    assert ':cell:' not in source['id']
    assert literal_parameter_issues(proposal,source)
    assert checked(p,source,proposal)[0]['state'] == 'needs_facts'
    assert not _proposal_dependencies(proposal.model_dump(),{source['id']:source})
    verified = deepcopy(original)
    verified.update(unit_verified=True,unit='м2')
    verified = shared_source(verified, 3, namespace=namespace)
    proposal.source_id = verified['id']
    assert not literal_parameter_issues(proposal,verified)
    assert checked(p,verified,proposal)[0]['state'] == 'confirmed'


def test_lost_physical_alias_metadata_cannot_reclassify_cell_as_an_ocr_paragraph():
    p, original, proposal = fixture()
    source = shared_source(original, 3)
    source.pop('physical_source_id')
    proposal.source_id = source['id']
    assert literal_parameter_issues(proposal,source)
    assert checked(p,source,proposal)[0]['state'] == 'needs_facts'


def test_default_direct_dependency_rejects_missing_ocr_unit_confirmation():
    _, source, proposal = fixture()
    raw = proposal.model_dump()
    raw.pop('operation')
    assert not _proposal_dependencies(raw,{source['id']:source})


def test_existing_sum_tool_and_saved_sum_dependencies_reject_unverified_ocr_units():
    from test_normative_quantity_anchors import toolkit

    tools, ids = toolkit(('100','200'))
    for source in tools.sources:
        source.update(unit_verified=False,unit='')
    for cell in tools.cells.values():
        cell['unit'] = ''
    result = tools._sum(EvidenceRequest(tool='sum_cells',operand_ids=ids,unit='м2'))
    assert 'error' in result
    assert not tools.quantity_sums
    # Even an invalid old-looking aggregate envelope is not transferable proof.
    total = dict(id='sum',kind='project',origin='calculated_from_cells',numeric_verified=True,
        unit_verified=True,unit='м2',version_id=tools.version,page=89,text='100 + 200 = 300 м2')
    proposal = dict(parameter='quantity',source_id='sum',operation='sum',operand_source_ids=ids,value='300',unit='м2')
    available = {s['id']:s for s in [*tools.sources,total]}
    assert not _proposal_dependencies(proposal,available)
