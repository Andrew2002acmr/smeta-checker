from copy import deepcopy
import pytest
from normative_verdict import publish, required_claims, PENDING, FIX, REVIEW


def report(status=FIX):
    return {'Статус аудита': status, 'Что не так': 'Raw mismatch'}


def item(state='confirmed', claims=None):
    return dict(state=state, claim_ids=claims or ['quantity', 'normative:applicability'],
        explanation='Scoped comparison with cited clause.', project_source_ids=['p1'], normative_source_ids=['n1'])


def validation(items):
    return dict(stage='done', review=dict(position_key='same', summary='Validated', items=items))


SOURCE = dict(checks=[dict(claim_id='quantity', state='contradiction', required=True)])


def test_saved_agent_state_agrees_with_guarded_row_verdict():
    from normative_verdict import finalize_state
    review=validation([item()])['review']
    assert finalize_state(review,SOURCE,'same')['state']=='confirmed'
    review['items'][0]['normative_source_ids']=[]
    assert finalize_state(review,SOURCE,'same')['state']=='needs_facts'


def test_absent_designation_is_not_a_proved_incompatibility():
    weak=item('possible_mismatch',['attribute:grade'])
    weak['explanation']='В ВОР марка А, в проекте марка Б. Обозначение А в ПД не найдено.'
    checked=validation([item(),weak])
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']==REVIEW
    weak['explanation']='Одно и то же свойство: проект требует класс Б; применён класс А, запрещённый приведённым нормативным пунктом.'
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']==FIX


def test_model_handles_are_readable_but_literal_source_quotes_stay_immutable():
    from normative_verdict import readable_review
    raw=dict(summary='Сумма s0+s1',items=[],sources=[dict(id='canonical-left',kind='project',page=7,text='s0 literal'),
        dict(id='canonical-right',kind='project',page=8,text='unchanged')])
    result=readable_review(raw)
    assert result['summary']=='Сумма ПД, стр. 7+ПД, стр. 8'
    assert raw['summary']=='Сумма s0+s1' and result['sources'][0]['text']=='s0 literal'


@pytest.mark.parametrize('stage', [None, 'pending', 'dispatched', 'running'])
def test_matching_mismatch_is_not_published_before_normative_validation(stage):
    raw = report()
    assert publish(raw, SOURCE, 'same', {'stage': stage}, enabled=True)['Статус аудита'] == PENDING
    assert raw == report()


@pytest.mark.parametrize('stage', ['failed', 'cancelled', 'done'])
def test_missing_or_failed_normative_layer_is_uncertainty_not_discrepancy(stage):
    assert publish(report(), SOURCE, 'same', {'stage': stage}, enabled=True)['Статус аудита'] == REVIEW


def test_requires_scoped_both_sided_evidence_and_complete_coverage():
    assert publish(report(), SOURCE, 'same', validation([item()]), enabled=True)['Статус аудита'] == 'ОК'
    assert publish(report(), SOURCE, 'same', validation([item(claims=['quantity'])]), enabled=True)['Статус аудита'] == REVIEW
    weak = item('possible_mismatch')
    weak['normative_source_ids'] = []
    assert publish(report(), SOURCE, 'same', validation([weak]), enabled=True)['Статус аудита'] == REVIEW
    assert publish(report(), SOURCE, 'same', validation([item('possible_mismatch')]), enabled=True)['Статус аудита'] == FIX


def test_quantity_may_use_pd_only_while_applicability_still_requires_normative_proof():
    quantity=item(claims=['quantity'])
    quantity['normative_source_ids']=[]
    checked=validation([quantity,item(claims=['normative:applicability'])])
    checked['review']['pipeline_version']=3
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']=='ОК'
    checked['review']['items']=checked['review']['items'][:1]
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']==REVIEW


def test_related_material_requires_parent_and_rate_not_just_matching_arithmetic():
    assert {'derived:parent', 'derived:rate'} <= required_claims(SOURCE, FIX, True)
    checked = validation([item()])
    assert publish(report(), SOURCE, 'same', checked, enabled=True, derived=True)['Статус аудита'] == REVIEW
    checked['review']['items'].append(item(claims=['derived:parent', 'derived:rate']))
    assert publish(report(), SOURCE, 'same', checked, enabled=True, derived=True)['Статус аудита'] == 'ОК'


def test_stale_review_and_legacy_result_are_not_silently_confirmed():
    stale = validation([item()])
    assert publish(report(), SOURCE, 'changed', stale, enabled=True)['Статус аудита'] == REVIEW
    original = deepcopy(report())
    assert publish(original, SOURCE, 'same', None) == original


def test_normative_model_cannot_override_locally_proved_formula_error():
    source = {**SOURCE, 'workbook_notes': [{'kind': 'formula-mismatch', 'message': '2*2 != 5'}]}
    assert publish(report(), source, 'same', validation([item()]), enabled=True)['Статус аудита'] == FIX


def test_material_waits_for_parent_and_cannot_inherit_unconfirmed_basis():
    from types import SimpleNamespace
    from normative_verdict import apply_parent_verdicts
    parent = SimpleNamespace(position_index=0, related_work=None, queue='pending')
    child = SimpleNamespace(position_index=1, related_work={'index': 0, 'number': '1'}, queue='ok', report_status='ОК', problem='')
    apply_parent_verdicts([parent, child], enabled=True, running=True)
    assert child.queue == 'pending'
    parent.queue, child.queue = 'review', 'ok'
    apply_parent_verdicts([parent, child], enabled=True, running=False)
    assert child.queue == 'review'
    parent.queue, child.queue = 'review', 'fix'
    apply_parent_verdicts([parent, child], enabled=True, running=False)
    assert child.queue == 'fix'  # Keep an independently proved child error.


def test_generic_model_confirmation_cannot_override_unproven_parameter_card():
    checked=validation([item()])
    checked['review']['parameter_cards']=[dict(label='Коэффициент 1,26',state='needs_facts',reason='Нет условий применения',required=True)]
    result=publish(report(),SOURCE,'same',checked,enabled=True)
    assert result['Статус аудита']==REVIEW and 'Коэффициент 1,26' in result['Что не так']


def test_child_rate_is_informational_for_work_but_unresolved_own_claim_still_blocks():
    checked = validation([item()])
    checked['review']['parameter_cards'] = [dict(label='Расход материала',state='needs_facts',reason='Нет источника',required=False)]
    assert publish(report(), SOURCE, 'same', checked, enabled=True)['Статус аудита'] == 'ОК'
    checked['review']['items'].append(item('needs_facts', ['method']))
    assert publish(report(), SOURCE, 'same', checked, enabled=True)['Статус аудита'] == REVIEW
    checked['review']['items'][-1]['state'] = 'possible_mismatch'
    assert publish(report(), SOURCE, 'same', checked, enabled=True)['Статус аудита'] == FIX


def test_unscoped_warning_cannot_silently_disappear_without_local_resolution():
    checked = validation([item()])
    checked['review']['items'].append(dict(state='needs_facts',explanation='Нужна проверка участка'))
    assert publish(report(), SOURCE, 'same', checked, enabled=True)['Статус аудита'] == REVIEW


def test_confirmed_table_quantity_is_visible_only_for_its_own_work_and_unit():
    from normative_verdict import verified_quantity
    review=dict(position_key='same',parameter_cards=[dict(id='quantity',state='confirmed',
        found_value='431.61',found_unit='м2',source_ids=['p89'])])
    assert verified_quantity(review,'same','м²') == ('431.61','м2')
    assert verified_quantity(review,'other','м2') is None
    assert verified_quantity(review,'same','м3') is None
    assert verified_quantity(review,'same','м2',derived=True) is None
    review['parameter_cards'][0]['state']='needs_facts'
    assert verified_quantity(review,'same','м2') is None


def test_old_review_shows_automatic_refresh_instead_of_stale_manual_verdict():
    checked=validation([item()])
    checked['stage']='pending'
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']==PENDING
    checked['review']['local_rules_version']=1
    assert publish(report(),SOURCE,'same',checked,enabled=True)['Статус аудита']=='ОК'
