"""Scope only locally nonrequired aggregates and trace-proven unclaimed prose."""
from copy import deepcopy

import pytest

from normative_position_check import validate_items
from normative_item_scope import TRACE
from normative_verdict import FIX,REVIEW,publish
from workspace_models import NormativePositionCheckItem

SOURCES=[dict(id='p',kind='project'),dict(id='n',kind='normative')]
CHECKS=[dict(claim_id='comment',required=False,state='review_required',reasons=['comment_predicates_evaluated']),
    dict(claim_id='attribute:comment:condition',required=True,state='not_found')]
KNOWN={'comment','attribute:comment:condition','quantity','normative:applicability','related:34:rate'}


def item(claims=None,state='needs_facts',text='Дочерний расход не найден.'):
    return NormativePositionCheckItem(topic='Аспект',claim_ids=['comment'] if claims is None else claims,state=state,
        required=True,explanation=text,project_source_ids=['p'],normative_source_ids=['n'])


def validate(items,*,checks=None,trace=None,replaying=False):
    return validate_items(items,SOURCES,KNOWN,evidence_trace=trace,
        matching_checks=CHECKS if checks is None else checks,replaying=replaying)


def verdict(items,checks=None,cards=None):
    stored=dict(status='ОК',checks=CHECKS if checks is None else checks)
    return publish({'Статус аудита':'ОК'},stored,'key',dict(stage='done',review=dict(position_key='key',
        pipeline_version=4,items=items,parameter_cards=cards or [],summary='Дочерний расход не найден.')),enabled=True)


def own_confirmed():
    return validate([item(['attribute:comment:condition','normative:applicability'],'confirmed','Собственное условие подтверждено.')])


def test_nonrequired_aggregate_is_not_reintroduced_but_typed_own_condition_remains_required():
    checked=validate([item(),item(['attribute:comment:condition'],text='Свой участок не определён.')])
    assert checked[0]['required'] is False and checked[1]['required'] is True
    result=verdict(checked+validate([item(['normative:applicability'],'confirmed')]))
    assert result['Статус аудита']==REVIEW
    assert 'Свой участок' in result['Что не так'] and 'Дочерний расход' not in result['Что не так']
    assert verdict([checked[0]]+own_confirmed())['Статус аудита']=='ОК'


@pytest.mark.parametrize('change',['generic','required','no_constituents','unknown_constituent','reason','aggregate_conflict','constituent_conflict','duplicate'])
def test_no_advisory_inference_without_exact_local_aggregate_metadata(change):
    checks=deepcopy(CHECKS)
    if change=='generic':
        checks[0].pop('reasons')
    elif change=='required':
        checks[0]['required']=True
    elif change=='no_constituents':
        checks=checks[:1]
    elif change=='unknown_constituent':
        checks[1]['claim_id']='unknown:comment:x'
    elif change=='reason':
        checks[0]['reasons'].append('other')
    elif change=='aggregate_conflict':
        checks[0]['state']='contradiction'
    elif change=='constituent_conflict':
        checks[1]['state']='source_conflict'
    else:
        checks.append(deepcopy(checks[0]))
    assert validate([item()],checks=checks)[0]['required'] is True


@pytest.mark.parametrize('claims,state',[
    (['comment'],'possible_mismatch'),(['attribute:comment:condition'],'needs_facts'),
    (['comment','attribute:comment:condition'],'needs_facts'),(['unknown'],'needs_facts'),
    (['comment','related:34:rate'],'needs_facts'),([], 'possible_mismatch'),
])
def test_conflicts_unknowns_and_mixed_claims_remain_blocking(claims,state):
    rows=validate([item(claims,state,text='ПД требует 10, ВОР задаёт 20.')])
    assert any(r['required'] for r in rows)
    if state=='possible_mismatch' and claims:
        assert verdict(rows+own_confirmed())['Статус аудита']==FIX


def test_original_possible_mismatch_cannot_become_advisory_after_source_downgrade():
    source=item([],state='possible_mismatch')
    trace=[]
    row,=validate([source],trace=trace)
    assert row['state']=='needs_facts' and row['required'] is True
    assert not trace


def test_new_unclaimed_nonconflict_is_advisory_without_satisfying_any_claim():
    trace=[]
    row,=validate([item([])],trace=trace)
    assert row['required'] is False and row['claim_ids']==[] and row['state']=='needs_facts'
    assert len(trace)==1 and trace[0]['kind']==TRACE
    assert verdict([row])['Статус аудита']==REVIEW  # Own obligations are still missing.
    assert verdict([row]+own_confirmed())['Статус аудита']=='ОК'


def test_legacy_blank_and_unknown_stripped_blank_cannot_reuse_new_empty_origin_proof():
    trace=[]
    blank,=validate([item([])],trace=trace)
    assert validate([NormativePositionCheckItem.model_validate(blank)],replaying=True)[0]['required'] is True
    assert validate([NormativePositionCheckItem.model_validate(blank)],trace=trace,replaying=True)[0]['required'] is False
    unknown_trace=[]
    unknown,=validate([item(['unknown'])],trace=unknown_trace)
    assert unknown['claim_ids']==[] and unknown['required'] and not unknown_trace
    assert validate([NormativePositionCheckItem.model_validate(unknown)],trace=unknown_trace,replaying=True)[0]['required']
    assert validate([NormativePositionCheckItem.model_validate(unknown)],trace=trace,replaying=True)[0]['required']
    assert validate([item(['unknown'])],trace=trace)[0]['required']


@pytest.mark.parametrize('change',['explanation','source','original_state','original_claims','plan','item_hash'])
def test_scope_trace_is_bound_to_the_exact_item_and_current_plan(change):
    trace=[]
    row,=validate([item([])],trace=trace)
    if change=='explanation':
        row['explanation']='Другая неопределённость'
    elif change=='source':
        row['project_source_ids']=[]
    elif change=='original_state':
        trace[0]['original_state']='possible_mismatch'
    elif change=='original_claims':
        trace[0]['original_claim_ids']=['unknown']
    elif change=='plan':
        trace[0]['plan_sha256']='other'
    else:
        trace[0]['item_sha256']='other'
    assert validate([NormativePositionCheckItem.model_validate(row)],trace=trace,replaying=True)[0]['required'] is True


def test_cards_still_block_and_legacy_unscoped_warning_stays_in_problem():
    items=own_confirmed()+validate([item()])
    result=verdict(items,cards=[dict(id='factor',label='Коэффициент',state='needs_facts',reason='Нет основания')])
    assert result['Статус аудита']==REVIEW and 'Коэффициент' in result['Что не так']
    legacy=validate([item([],text='Неизвестный аспект')],replaying=True)
    result=verdict(items+legacy)
    assert result['Статус аудита']==REVIEW and 'Неизвестный аспект' in result['Что не так']


def test_no_uncertain_summary_fallback_reintroduces_advisory_as_manual_todo():
    result=verdict(validate([item()]))
    assert result['Статус аудита']==REVIEW and 'Дочерний расход' not in result['Что не так']
