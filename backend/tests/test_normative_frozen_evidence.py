"""Saved review refresh can reuse evidence, never invent project quantities."""
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from audit_models import VorPosition, PositionSource
from normative_parameter_anchors import parameter_candidates, resolve_anchors
from normative_position_check import validate_agent_saved
from workspace_models import NormativeParameterProposal, NormativePositionCheckItem


def _sources(unit='м2'):
    return [dict(id='p', kind='project', origin='native', version_id='pdf', page=1,
                 label='ПД', text=f'Площадь участка 10 {unit}.'),
            dict(id='n', kind='normative', origin='native', page=2,
                 label='ГЭСН 27 · 27-01-001-01', text=f'Измеритель: {unit}')]


def _item():
    return dict(topic='Основание', state='confirmed', required=True,
                claim_ids=['quantity', 'normative:applicability'],
                project_source_ids=['p'], normative_source_ids=['n'],
                explanation='Участок и применимая норма установлены.')


def _fixture():
    position = VorPosition(index=0, number='1', volume='10', unit='м2',
        work_name='Площадь участка', position_type='Работа',
        position_source=PositionSource(document_sha256='vor', format='xlsx', sheet='S',
                                       row=10, header_row=1, position_id='0'))
    review = dict(pipeline_version=4, execution_state='complete', state='needs_facts',
        summary='Измеритель не извлечён', sources=_sources(), related_positions=[],
        items=[_item()], parameter_proposals=[dict(parameter='quantity', value='10', unit='м2',
            source_id='p', quote='Площадь участка 10 м2', scope_quote='Площадь участка 10 м2',
            scope_state='verified')])
    row = dict(id='old', kind='check', payload=dict(position_key='key-0', vor_version_id='vor',
        kb={'release':'pinned'}, position_check=review, work_relations={}))
    record = dict(id='run', vor_version_id='vor', config={'pdf_versions':{'ПД':'pdf'}},
        indexed_results=[dict(position_index=0, result=dict(status='ОК'))])
    repository = Mock()
    repository.save_normative_review.side_effect = lambda run, index, kind, data, actor: dict(id='new', payload=data)
    return position, row, record, SimpleNamespace(repository=repository)


def _invoke(position, row, record, context, *, positions=None, links=None):
    with ExitStack() as stack:
        stack.enter_context(patch('workspace_service.inputs_for_run', return_value=positions or [position]))
        stack.enter_context(patch('vor_material_links.material_links', return_value=links or {}))
        stack.enter_context(patch('normative_service.position_key', side_effect=lambda p:f'key-{p.index}'))
        stack.enter_context(patch('normative_service.profile_for', return_value='test'))
        stack.enter_context(patch('normative_service.corpus', return_value=SimpleNamespace(identity={'release':'pinned'})))
        stack.enter_context(patch('audit_review_policy.review_source', side_effect=lambda raw, task:raw))
        stack.enter_context(patch('normative_position_check._ask', side_effect=AssertionError('No LLM')))
        stack.enter_context(patch('pdf_text.PDFTextSource', side_effect=AssertionError('No PDF/OCR')))
        return validate_agent_saved(context, record, position.index, row, 'test', register=False)


def test_implicit_unit_scale_has_literal_quote_and_requires_selected_norm():
    sources = _sources('т')
    candidate = next(p for p in parameter_candidates(sources) if p['parameter']=='unit_scale')
    assert candidate['value']=='1' and candidate['quote']=='Измеритель: т'
    items = [NormativePositionCheckItem.model_validate(_item())]
    assert resolve_anchors([], sources, items, owner_unit='т')[0].value=='1'
    assert resolve_anchors([], sources, [], owner_unit='т')==[]
    assert resolve_anchors([], sources, items, owner_unit='м2')==[]


def test_linear_norm_scale_is_read_as_length_not_lost():
    sources=_sources('м')
    sources[1]['text']='Измеритель: 100 м'
    items=[NormativePositionCheckItem.model_validate(_item())]
    proposals=resolve_anchors([], sources, items, owner_unit='м')
    assert len(proposals)==1 and proposals[0].value=='100'
    assert proposals[0].unit=='м' and proposals[0].quote=='Измеритель: 100 м'


def test_implicit_scale_repairs_same_source_quote_without_changing_unresolved_scope():
    proposal = NormativeParameterProposal(parameter='unit_scale', value='1', unit='т',
        source_id='n', quote='Измеритель: 1 т', scope_state='unresolved')
    resolve_anchors([proposal], _sources('т'), [], owner_unit='т')
    assert proposal.quote=='Измеритель: т' and proposal.scope_state=='unresolved'
    proposal.source_id='invented'
    proposal.quote='Измеритель: 1 т'
    resolve_anchors([proposal], _sources('т'), [], owner_unit='т')
    assert proposal.quote=='Измеритель: 1 т'


def test_native_depth_repairs_units_from_exact_prose_in_the_same_source():
    text='Газон вдоль улицы. Наносится плодородный слой почвы толщиной 0, 20 м и сеются многолетние травы.'
    source=dict(id='lawn', kind='project', origin='native', text=text)
    scope='Наносится плодородный слой почвы толщиной 0, 20 м и сеются многолетние травы.'
    proposal=NormativeParameterProposal(parameter='thickness', value='20', unit='см', source_id='lawn',
        quote='Наносится плодородный слой почвы толщиной 0, 20 м', scope_quote=scope, scope_state='verified')
    resolve_anchors([proposal], [source], [], work_scope='Газон с растительной землей h=0,20 м')
    assert proposal.value=='0.20' and proposal.unit=='м'
    assert proposal.quote=='толщиной 0, 20 м' and proposal.scope_quote==scope
    assert proposal.scope_state=='verified'
    source['origin']='ocr_unverified'
    proposal.value,proposal.unit,proposal.quote='20','см','Несуществующая цитата'
    resolve_anchors([proposal], [source], [])
    assert proposal.quote=='Несуществующая цитата'


def test_frozen_refresh_fills_existing_norm_scale_without_calls_or_source_changes():
    position, row, record, context = _fixture()
    original = deepcopy(row)
    saved = _invoke(position, row, record, context)
    review = saved['payload']['position_check']
    scale = next(c for c in review['parameter_cards'] if c['role']=='unit_scale')
    assert scale['state']=='confirmed' and scale['found_value']=='1'
    assert scale['quote']=='Измеритель: м2'
    assert review['state']=='confirmed' and review['telemetry']['requests']==0
    assert review['sources']==original['payload']['position_check']['sources']
    assert row==original
    repeated = _invoke(position, saved, record, context)['payload']['position_check']
    assert repeated['parameter_proposals']==review['parameter_proposals']
    assert repeated['parameter_cards']==review['parameter_cards']


def test_frozen_refresh_does_not_reapprove_unresolved_quantity():
    position, row, record, context = _fixture()
    review = row['payload']['position_check']
    review['items'][0]['state']='needs_facts'
    review['items'][0]['explanation']='Принадлежность площади этому участку не установлена.'
    review['parameter_proposals'][0]['scope_state']='unresolved'
    saved = _invoke(position, row, record, context)['payload']['position_check']
    assert saved['state']=='needs_facts'
    assert next(c for c in saved['parameter_cards'] if c['id']=='quantity')['state']=='needs_facts'


def test_changed_existing_parent_requires_new_evidence():
    position, row, record, context = _fixture()
    row['payload']['work_relations']={'parent':{'index':5}}
    with pytest.raises(ValueError, match='родительской'):
        _invoke(position, row, record, context)
    context.repository.save_normative_review.assert_not_called()


def test_newly_detected_parent_reuses_only_already_verified_geometry():
    parent, donor, record, context = _fixture()
    donor['payload']['position_check']['state']='confirmed'
    donor['payload']['position_check']['parameter_cards']=[dict(id='quantity', state='confirmed', source_ids=['p'])]
    child = parent.model_copy(update={'index':1, 'number':'2', 'position_type':'Материал',
        'work_name':'Расчётный материал', 'unit':'т', 'volume':'2',
        'position_source':parent.position_source.model_copy(update={'row':11, 'position_id':'1'})})
    child_review = dict(pipeline_version=4, execution_state='complete', state='needs_facts',
        summary='Основание не установлено', sources=[], related_positions=[], parameter_proposals=[],
        items=[dict(topic='Расход', state='needs_facts', required=True, claim_ids=['derived:rate'],
                    project_source_ids=[], normative_source_ids=[], explanation='Норма расхода не установлена.')])
    row = dict(id='child-old', kind='check', payload=dict(position_key='key-1', vor_version_id='vor',
        kb={'release':'pinned'}, position_check=child_review, work_relations={}))
    record['indexed_results'].append(dict(position_index=1, result=dict(status='ОК')))
    context.repository.normative_reviews.return_value=[donor]
    link=dict(index=0, number='1', name=parent.work_name, row=10, sheet='S', expression='10*0.2')
    saved = _invoke(child, row, record, context, positions=[parent, child], links={1:link})['payload']
    assert saved['work_relations']['parent']['index']==0
    review=saved['position_check']
    assert next(c for c in review['parameter_cards'] if c['id']=='quantity')['state']=='confirmed'
    assert any('derived:parent' in i['claim_ids'] and i['state']=='confirmed' for i in review['items'])
    assert next(i for i in review['items'] if 'derived:rate' in i['claim_ids'])['state']=='needs_facts'
    assert review['state']=='needs_facts'


@pytest.mark.parametrize('changed', ['vor', 'pdf', 'kb'])
def test_frozen_refresh_rejects_changed_pins(changed):
    position, row, record, context = _fixture()
    if changed=='vor':
        row['payload']['vor_version_id']='other'
    elif changed=='pdf':
        row['payload']['position_check']['sources'][0]['version_id']='other'
    else:
        row['payload']['kb']={'release':'other'}
    with pytest.raises(ValueError):
        _invoke(position, row, record, context)
    context.repository.save_normative_review.assert_not_called()


def test_comment_summary_closes_only_after_its_required_predicates():
    from normative_verdict import confirmed_comment_aggregate
    checks=[dict(claim_id='geometry:comment:thickness:1', state='not_found', required=True),
            dict(claim_id='basis:comment:consumption:2', state='review_required', required=True),
            dict(claim_id='comment', state='review_required', reasons=['comment_predicates_evaluated'])]
    confirmed={'geometry:comment:thickness:1', 'basis:comment:consumption:2'}
    assert confirmed_comment_aggregate({'checks':checks}, confirmed)
    assert not confirmed_comment_aggregate({'checks':checks}, set())
    for state in ('contradiction', 'source_conflict'):
        changed=deepcopy(checks)
        changed[0]['state']=state
        assert not confirmed_comment_aggregate({'checks':changed}, confirmed)
    changed=deepcopy(checks)
    changed.append(dict(claim_id='attribute:comment:unknown', state='review_required', required=True))
    assert not confirmed_comment_aggregate({'checks':changed}, confirmed)
    changed[-1]['required']=False
    assert confirmed_comment_aggregate({'checks':changed}, confirmed)
    checks[-1]['reasons']=['comment_not_cited']
    assert not confirmed_comment_aggregate({'checks':checks}, confirmed)
