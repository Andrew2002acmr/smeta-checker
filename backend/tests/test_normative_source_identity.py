from copy import deepcopy

import pytest

from analysis_replay import digest
from normative_source_identity import canonical_source_id, shared_source
from normative_paired_quantities import verified_components
from normative_paired_scope import validated_parent
from normative_quantity_anchors import quantity_parameter_candidates


def test_single_hop_and_repeated_alias_preserve_both_parent_and_pair():
    from test_normative_paired_scope import fixture
    _, _, _, original = fixture()
    before = deepcopy(original)
    first = shared_source(original, 23)
    assert canonical_source_id(first) == original['id']
    assert verified_components(first) == verified_components(original)
    assert validated_parent(first) == validated_parent(original)
    assert len(quantity_parameter_candidates([first], {})) == 2
    second = shared_source(first, 24)
    assert canonical_source_id(second) == original['id']
    assert second['physical_source_id'] == original['id']
    assert verified_components(second) == verified_components(original)
    assert original == before
    assert shared_source(original, -1) is None


def test_legacy_single_hop_alias_is_content_checked_without_metadata():
    from test_normative_paired_scope import fixture
    _, _, _, original = fixture()
    alias = dict(deepcopy(original), id='shared:23:' + digest(original)[:24],
                 physical_source_id=original['id'])
    assert canonical_source_id(alias) == original['id']
    assert verified_components(alias) == verified_components(original)


@pytest.mark.parametrize('namespace', ['geometry', 'basis', 'work'])
def test_parent_alias_has_content_attestation_and_single_canonical_target(namespace):
    from test_normative_paired_scope import fixture
    _, _, _, original = fixture()
    alias = shared_source(original, 23, namespace=namespace)
    assert alias['id'].startswith('work-23:'if namespace=='work'else f'parent-23:{namespace}:')
    assert canonical_source_id(alias) == original['id']
    assert verified_components(alias) == verified_components(original)
    assert canonical_source_id(shared_source(alias, 24)) == original['id']
    alias.pop('source_alias')
    assert canonical_source_id(alias) is None


def test_legacy_unattested_parent_cannot_be_promoted_through_a_new_alias():
    original = dict(id='parent-2:geometry:'+'0'*24, physical_source_id='agent:p1:cell:'+'0'*24,
                    text='25', kind='project')
    assert canonical_source_id(original) is None
    assert shared_source(original, 24) is None


@pytest.mark.parametrize('mutation', ['wrong_target', 'changed_text', 'changed_version',
    'changed_bbox', 'changed_digest', 'changed_metadata', 'arbitrary_alias', 'alias_chain',
    'wrong_ocr_history'])
def test_damaged_alias_cannot_turn_a_claimed_physical_id_into_valid_proof(mutation):
    from test_normative_paired_scope import fixture
    _, _, _, original = fixture()
    alias = shared_source(original, 23)
    if mutation == 'wrong_target':
        alias['physical_source_id'] = 'agent:p1:pairrow:' + '0' * 24
    elif mutation == 'changed_text':
        alias['text'] = 'Other work 114'
    elif mutation == 'changed_version':
        alias['version_id'] = 'other'
    elif mutation == 'changed_bbox':
        alias['paired_measurement_context']['row_cells'][0]['bbox'] = [0, 0, 5, 5]
    elif mutation == 'changed_digest':
        alias['id'] = 'shared:23:' + '0' * 24
    elif mutation == 'changed_metadata':
        alias['source_alias']['donor_position'] = 22
    elif mutation == 'arbitrary_alias':
        alias['id'] = 'shared:23:logical'
    elif mutation == 'alias_chain':
        alias['physical_source_id'] = 'shared:22:' + '0' * 24
    elif mutation == 'wrong_ocr_history':
        alias['paired_measurement_context']['row_cells'][-1]['paired_ocr'] = {'fake': True}
    assert canonical_source_id(alias) is None
    assert not verified_components(alias)
    assert not quantity_parameter_candidates([alias], {})
    assert shared_source(alias, 24) is None


def test_alias_does_not_relax_physical_checks_even_after_rebinding_digest():
    from test_normative_paired_scope import fixture
    _, _, _, original = fixture()
    original['paired_measurement_context']['source_anchors'][0]['bbox'] = [0, 0, 5, 5]
    alias = shared_source(original, 23)
    assert canonical_source_id(alias) == original['id']
    assert not verified_components(alias)
    assert not quantity_parameter_candidates([alias], {})


def test_cell_alias_reuses_canonical_registry_without_claiming_scope():
    from test_normative_quantity_anchors import toolkit
    tools, ids = toolkit(('25',))
    alias = shared_source(tools.sources[0], 23)
    result = quantity_parameter_candidates([alias], tools.cells)
    assert len(result) == 1
    assert result[0]['proposal']['source_id'] == alias['id']
    assert result[0]['proposal']['scope_state'] == 'unresolved'
    tools.cells[ids[0]]['cell']['bbox'] = [0, 0, 0, 0]
    assert not quantity_parameter_candidates([alias], tools.cells)


def test_alias_and_original_do_not_spend_proposal_budget_twice():
    from test_normative_paired_scope import fixture
    _, _, _, source = fixture()
    alias=shared_source(source,23)
    assert len(quantity_parameter_candidates([source,alias],{}))==2
    assert len(quantity_parameter_candidates([alias,source],{}))==2


def test_conflicting_canonical_envelopes_cannot_silently_win_by_order():
    from test_normative_paired_scope import fixture
    _, _, _, source = fixture()
    other=deepcopy(source)
    other['label']='Different saved envelope'
    alias=shared_source(other,23)
    assert canonical_source_id(alias)==source['id']
    assert not quantity_parameter_candidates([source,alias],{})
    assert not quantity_parameter_candidates([alias,source],{})


def test_two_aliases_do_not_manufacture_two_independent_sum_operands():
    from normative_parent_basis import _proposal_dependencies
    source=dict(id='original',kind='project',origin='native',version_id='v',page=1,
                text='Quantity\n10',numeric_verified=True,unit_verified=True,unit='шт')
    first,second=shared_source(source,1),shared_source(source,2)
    total=dict(id='sum',kind='project',origin='calculated_from_cells',version_id='v',page=1,
               text='10 + 10 = 20 шт',numeric_verified=True,unit_verified=True,unit='шт')
    proposal=dict(source_id='sum',operation='sum',operand_source_ids=[first['id'],second['id']],value='20',unit='шт')
    assert not _proposal_dependencies(proposal,{s['id']:s for s in (first,second,total)})
