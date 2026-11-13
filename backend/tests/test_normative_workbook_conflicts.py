"""Only current, independently recomputed XLSX contradictions become fixes."""
from copy import deepcopy
import re

import pytest

from audit_models import PositionSource,VorPosition
from human_basis import position_key
from normative_verdict import FIX,REVIEW,finalize_state,publish
from normative_workbook_conflicts import PROOF,conflicts


def fixture(actual='=5231.08*0.4/1000',display='5303,08*0,4/1000',volume='2.092432'):
    return VorPosition(index=18,work_name='Розлив битумной эмульсии',volume=volume,unit='т',
        formula=display,formula_refs={'display_cell':'E36'},comment='Расход 0,4 л/м2',
        calculation=dict(cell='D36',expression=actual,status='calculated',value=volume,cached_value=volume,
            cached_mismatch=False,dependencies=[dict(cell=None,origin='formula_literal',value=n)
                for n in re.findall(r'\d+(?:\.\d+)?',actual)]),
        position_source=PositionSource(format='xlsx',document_sha256='a'*64,sheet='Дорожная одежда',
            row=36,header_row=15,position_id='row36',cells={'D36':volume,'E36':display}))


def review():
    return dict(pipeline_version=4,state='needs_facts',execution_state='complete',sources=[],
        evidence_trace=[],items=[dict(claim_ids=['normative:applicability'],state='needs_facts',
            required=True,explanation='Нужно основание нормы.',project_source_ids=[],normative_source_ids=[])])


def publish_row(position,result=None,**kwargs):
    key=position_key(position)
    return publish({'Статус аудита':REVIEW},{},key,
        {'stage':'done','review':dict(result or review(),position_key=key)},enabled=True,position=position,**kwargs)


def test_exact_row18_conflict_is_local_and_does_not_choose_the_correct_quantity():
    position=fixture()
    original=position.model_dump()
    proof,=conflicts(position,position_key(position))
    assert proof['kind']==PROOF and proof['claim_id']=='workbook:E36:display-operand-mismatch'
    assert proof['inputs']['actual_value']=='2.092432'
    assert proof['inputs']['display_value']=='2.121232'
    assert 'Правильное количество по ПД этим сравнением не определяется' in proof['message']
    assert position.model_dump()==original
    assert publish_row(position)['Статус аудита']==FIX


def test_row32_real_conflict_survives_its_unrelated_binary_float_tail():
    position=fixture('=(105.84)*1.26','(357,13*0,2)*1,26','133.35840000000002')
    position.calculation['value']='133.3584'
    proof,=conflicts(position,position_key(position))
    assert proof['inputs']['actual_value']=='133.3584'
    assert proof['inputs']['display_value']=='89.99676'


@pytest.mark.parametrize('actual,display,volume',[
    ('=105.84*1.26','105,84*1,26','133.35840000000002'),
    ('=10*2','5+15','20'),  # Different arithmetic, same result.
    ('=431.611*0.12','431,61*0,12','51.79332'),
    ('=431.615*0.12','431,62*0,12','51.7938'),
    ('=133.35840000000002','133,3584','133.35840000000002'),
    ('=431.611*0.12','51,79','51.79332'),
    ('=431.611*0.12','52','51.79332'),
    ('=5231.08*0.4/1000','5231,08','2.092432'),  # A bare operand is not a displayed calculation.
    ('=8921*0.4/1000','8921*0,4/1000','3.5684'),
])
def test_rounding_equivalence_and_untyped_comment_rate_are_not_errors(actual,display,volume):
    position=fixture(actual,display,volume)
    position.comment='Расход 0,6 л/м2'
    assert conflicts(position,position_key(position))==[]
    assert publish_row(position)['Статус аудита']==REVIEW


@pytest.mark.parametrize('change',[
    'source_missing','document_missing','source_format','source_row','source_sheet','source_d','source_e',
    'same_cell','other_row','unknown_cell','not_calculated','cache_mismatch','cache_value','calc_value',
    'declared_value','actual_expression','unsupported_expression','missing_expression','unknown_display',
])
def test_incomplete_or_changed_provenance_cannot_use_a_saved_finding(change):
    position=fixture()
    assert position.position_source is not None
    source=position.position_source
    if change=='source_missing':
        position.position_source=None
    elif change=='document_missing':
        source.document_sha256=''
    elif change=='source_format':
        source.format='pdf'
    elif change=='source_row':
        source.row=37
    elif change=='source_sheet':
        source.sheet=''
    elif change=='source_d':
        source.cells['D36']='999'
    elif change=='source_e':
        source.cells['E36']='5231,08*0,4/1000'
    elif change=='same_cell':
        position.formula_refs['display_cell']='D36'
    elif change=='other_row':
        position.formula_refs['display_cell']='E37'
    elif change=='unknown_cell':
        position.calculation['cell']='XFD36'
    elif change=='not_calculated':
        position.calculation['status']='unknown'
    elif change=='cache_mismatch':
        position.calculation['cached_mismatch']=True
    elif change=='cache_value':
        position.calculation['cached_value']='999'
    elif change=='calc_value':
        position.calculation['value']='999'
    elif change=='declared_value':
        position.volume='999'
    elif change=='actual_expression':
        position.calculation['expression']='=1+2'
    elif change=='unsupported_expression':
        position.calculation['expression']='=A1*0.4/1000'
    elif change=='missing_expression':
        position.calculation['expression']=''
    elif change=='unknown_display':
        position.formula=source.cells['E36']='SUM(1,2)'
    assert not conflicts(position,position_key(position))
    result=review()
    result['evidence_trace']=[dict(kind=PROOF,claim_id='workbook:E36:display-operand-mismatch',message='Fake proof')]
    assert publish_row(position,result)['Статус аудита']==REVIEW


@pytest.mark.parametrize('display',['1/0','2**100','1e100','1%','2//1','9'*41,'1+__import__("os")'])
def test_unsupported_or_unbounded_arithmetic_fails_closed(display):
    assert not conflicts(fixture(display=display),position_key(fixture(display=display)))


def test_current_key_and_current_inputs_are_mandatory_and_proof_is_bound_to_them():
    position=fixture()
    assert not conflicts(None,'key')
    assert not conflicts(position,'old-key')
    before,=conflicts(position,position_key(position))
    assert position.position_source is not None
    position.position_source.document_sha256='b'*64
    after,=conflicts(position,position_key(position))
    assert before['input_sha256']!=after['input_sha256']
    # publish never trusts a serialized proof without the current row.
    result=review()
    result['evidence_trace']=[before]
    assert publish({'Статус аудита':REVIEW},{},'key',
        {'stage':'done','review':dict(result,position_key='key')},enabled=True)['Статус аудита']==REVIEW


def test_incomplete_legacy_position_fails_closed_without_attempting_a_key():
    from types import SimpleNamespace
    assert not conflicts(SimpleNamespace(index=18,position_source={}), 'key')


def test_fresh_typed_display_check_is_required(monkeypatch):
    import vor_consistency
    position=fixture()
    monkeypatch.setattr(vor_consistency,'check_display_consistency',lambda *args:[])
    assert not conflicts(position,position_key(position))


def test_technical_failure_and_pending_stale_results_do_not_become_completed_fixes():
    position=fixture()
    result=review()
    result['execution_state']='technical_failure'
    assert publish_row(position,result)['Статус аудита']=='Проверка не выполнена'
    assert publish({'Статус аудита':REVIEW},{},position_key(position),{'stage':'running'},
        enabled=True,position=position)['Статус аудита']=='Проверяется по нормативам'


def test_finalize_is_idempotent_preserves_originals_and_leaves_engineering_gaps_open():
    position=fixture()
    result=review()
    stored=dict(status=REVIEW,checks=[dict(claim_id='quantity',state='not_found',required=True)])
    before=deepcopy((result,stored))
    saved=finalize_state(result,stored,position_key(position),position=position)
    assert saved['state']=='possible_mismatch' and saved['requires_engineer_review']
    assert saved['items']==result['items']
    assert len([e for e in saved['evidence_trace'] if e['kind']=='workbook_display_conflict_v1'])==1
    assert len([e for e in saved['evidence_trace'] if e['kind']=='evidence_work_plan_v2'])==1
    assert (result,stored)==before
    assert finalize_state(saved,stored,position_key(position),position=position)==saved
    assert position.position_source is not None
    position.formula=position.position_source.cells['E36']='5231,08*0,4/1000'
    rechecked=finalize_state(saved,stored,position_key(position),position=position)
    assert rechecked['state']=='needs_facts'
    assert not any(e['kind']=='workbook_display_conflict_v1' for e in rechecked['evidence_trace'])
