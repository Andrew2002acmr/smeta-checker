"""Numeric grouping does not need a normative citation, but does need calculation."""
from copy import deepcopy

import pytest

from audit_models import VorPosition,PositionSource
from normative_parameters import enrich
from normative_position_check import validate_items
from normative_verdict import publish,source_supported
from normative_workbook_arithmetic import PROOF,arithmetic_claims
from workspace_models import NormativePositionCheckItem


def fixture(formula='108+53',volume='161'):
    position=VorPosition(index=94,number='95',work_name='Установка бортовых камней',position_type='Работа',
        volume=volume,unit='м',formula=formula,position_source=PositionSource(document_sha256='vor',format='xlsx',
            sheet='S',row=114,header_row=1,position_id='94'))
    text='Бортовые камни остановок. Всего 161 м'
    sources=[dict(id='p',kind='project',origin='native',version_id='pdf',page=92,label='ПД',text=text),
        dict(id='n',kind='normative',origin='native',source_id='norm-pdf',page=20,
            label='ГЭСН27 · 27-02-010-01',text='Измеритель: 100 м')]
    items=[dict(topic='Количество и арифметика',state='confirmed',required=True,
        claim_ids=['presence','quantity','unit','workbook:formula'],project_source_ids=['p'],normative_source_ids=[],
        explanation='Количество161 м соответствует ПД, 108+53=161.'),
        dict(topic='Применимость',state='confirmed',required=True,claim_ids=['normative:applicability'],
            project_source_ids=['p'],normative_source_ids=['n'],explanation='Норма применима.')]
    result=dict(pipeline_version=4,execution_state='complete',state='needs_facts',sources=sources,summary='Проверено',
        items=items,parameter_proposals=[dict(parameter='quantity',operation='direct',source_id='p',value='161',unit='м',
            quote=text,scope_quote=text,scope_state='verified'),
            dict(parameter='unit_scale',source_id='n',value='100',unit='м',quote='Измеритель: 100 м',scope_state='verified')])
    return position,result


def checked(position,result):
    result=deepcopy(result)
    result['items']=validate_items([NormativePositionCheckItem.model_validate(i) for i in result['items']],result['sources'])
    return enrich(position,result,{position.index:position},{})


def status(review):
    return publish({'Статус аудита':'Требуется ручная проверка'},
        dict(checks=[dict(claim_id=c,state='not_found',required=True) for c in ('quantity','workbook:formula')]),'key',
        dict(stage='done',review=dict(review,position_key='key')),enabled=True)['Статус аудита']


def test_actual_positive_addition_combined_with_quantity_needs_no_normative_arithmetic_citation():
    position,result=fixture()
    original=deepcopy(result)
    review=checked(position,result)
    assert status(review)=='ОК'
    assert arithmetic_claims(review)=={'workbook:formula'}
    assert all(c['state']=='confirmed' for c in review['parameter_cards'])
    assert any(i['claim_ids']==['workbook:formula'] and i['state']=='needs_facts' for i in review['items'])
    assert any('quantity' in i['claim_ids'] and i['state']=='confirmed' for i in review['items'])
    assert not source_supported(result['items'][0])  # No blanket source exception was introduced.
    assert result==original
    assert checked(position,review)==review


@pytest.mark.parametrize('change',['quantity_scope','quantity_unit','model_uncertain','coefficient','method','named_subsite'])
def test_arithmetic_proof_does_not_close_other_engineering_obligations(change):
    position,result=fixture()
    if change=='quantity_scope':
        result['parameter_proposals'][0]['scope_state']='unresolved'
    elif change=='quantity_unit':
        result['parameter_proposals'][0]['unit']='м2'
    elif change=='model_uncertain':
        result['items'][0]['state']='needs_facts'
    elif change=='coefficient':
        position.comment='Коэффициент 108'
    else:
        result['items'].append(dict(topic='Отдельный аспект',state='needs_facts',required=True,
            claim_ids=['method' if change=='method' else 'geometry:left'],project_source_ids=[],normative_source_ids=[],
            explanation='Нужно отдельное основание.'))
    review=checked(position,result)
    assert arithmetic_claims(review)=={'workbook:formula'}
    assert status(review)=='Требуется ручная проверка'


@pytest.mark.parametrize('formula', ['108+54','A1+53','UNKNOWN(161)','108/0'])
def test_wrong_or_unavailable_formula_cannot_gain_proof_from_model_or_saved_confirmed_card(formula):
    position,result=fixture(formula)
    result['items'][0]['normative_source_ids']=['n']
    review=checked(position,result)
    assert not arithmetic_claims(review)
    for card in review['parameter_cards']:
        if card['id']=='calculation:94':
            card['state']='confirmed'
    assert not arithmetic_claims(review) and status(review)!='ОК'


def test_saved_arithmetic_is_recalculated_and_only_matches_its_own_card():
    position,result=fixture()
    review=checked(position,result)
    for change in ('expression','declared_value','own_card','dependencies'):
        tampered=deepcopy(review)
        proof=next(e for e in tampered['evidence_trace'] if e['kind']==PROOF)
        if change=='expression':
            proof['expression']='108+54'
        elif change=='declared_value':
            proof['declared_value']='162'
        elif change=='own_card':
            proof['position_index']=1
        else:
            proof['dependencies']=[dict(cell='XFD1048576',value='161')]
        assert not arithmetic_claims(tampered)
    # A different row's valid arithmetic cannot satisfy this row implicitly.
    review['parameter_cards']=[c for c in review['parameter_cards'] if c['id']!='calculation:94']
    assert not arithmetic_claims(review)


def test_excel_dependencies_and_cache_conflict_are_checked_programmatically():
    position,result=fixture('A1+53')
    position.calculation=dict(expression='A1+53',cell='E114',dependencies=[dict(cell='A1',value='108')])
    review=checked(position,result)
    assert arithmetic_claims(review)=={'workbook:formula'}
    position.calculation['cached_mismatch']=True
    assert not arithmetic_claims(checked(position,result))


def test_parsed_literal_dependencies_are_rederived_from_actual_expression():
    position,result=fixture()
    position.calculation=dict(expression='=108+53',cell='D117',dependencies=[
        dict(cell=None,origin='formula_literal',value='108'),dict(cell=None,origin='formula_literal',value='53')])
    review=checked(position,result)
    assert arithmetic_claims(review)=={'workbook:formula'} and status(review)=='ОК'
    position.calculation['expression']='=108+54'
    assert not arithmetic_claims(checked(position,result))


def test_frozen_revalidation_splits_combined_claim_and_keeps_matching_immutable():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from test_normative_frozen_evidence import _invoke
    position,result=fixture()
    row=dict(id='old',kind='check',payload=dict(position_key='key-94',vor_version_id='vor',kb={'release':'pinned'},
        position_check=result,work_relations={}))
    record=dict(id='run',vor_version_id='vor',config={'pdf_versions':{'ПД':'pdf'}},
        indexed_results=[dict(position_index=94,result=dict(status='ОК'))])
    original=deepcopy((row,record))
    repository=Mock()
    repository.save_normative_review.side_effect=lambda run,index,kind,payload,actor:dict(id='new',payload=payload)
    saved=_invoke(position,row,record,SimpleNamespace(repository=repository))['payload']['position_check']
    assert saved['state']=='confirmed' and saved['telemetry']['requests']==0
    assert (row,record)==original
