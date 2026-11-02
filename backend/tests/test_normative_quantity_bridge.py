from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_parameters import enrich
from normative_quantity_bridge import OMISSION, SUPERSEDED, TOPIC, quantity_unit_item, record_source_omission
from normative_verdict import publish, readable_review


def fixture():
    position = SimpleNamespace(index=0, number='1', volume='12.50', unit='м2',
        work_name='Устройство верхнего слоя основания из ЩПС', position_type='Работа',
        formula='', calculation={}, position_source=SimpleNamespace(row=18))
    text = 'Верхний слой основания из ЩПС: площадь 12,50 м2'
    project = dict(id='p', kind='project', origin='native', version_id='pdf-v1', page=1,
        label='ПД', text=text, rects=[])
    norm = dict(id='n', kind='normative', origin='native', source_id='norm-pdf', page=30,
        label='ГЭСН 27 · 27-04-001-01', text='Измеритель: 100 м2')
    result = dict(pipeline_version=3, execution_state='complete', sources=[project, norm],
        items=[dict(topic='Применимость', state='confirmed', required=True,
            claim_ids=['normative:applicability'], project_source_ids=['p'], normative_source_ids=['n'],
            explanation='Применимость отдельно сопоставлена с проектом.')], evidence_trace=[],
        parameter_proposals=[dict(parameter='quantity', source_id='p', value='12.50', unit='м2',
            quote=text, scope_quote=text, scope_state='verified', operation='direct', operand_source_ids=[]),
            dict(parameter='unit_scale', source_id='n', value='100', unit='м2', quote=norm['text'],
                scope_quote=norm['text'], scope_state='verified', operation='direct', operand_source_ids=[])])
    return position, result


def item(state='needs_facts', claims=None, project_ids=None, topic='Количество'):
    return dict(topic=topic, state=state, required=True, claim_ids=claims or ['quantity', 'unit'],
        project_source_ids=project_ids or [], normative_source_ids=[], explanation='Количество требует проверки.')


def omission(result, *, explanation='Количество подтверждено по s0.'):
    original = item('confirmed')
    original['explanation'] = explanation
    validated = {**original, 'state': 'needs_facts', 'explanation': explanation+' Не хватает достоверных ссылок.'}
    record_source_omission(original, validated, result['sources'], result['evidence_trace'])
    result['items'].append(validated)
    return validated


def bridge(position, result, relation=None):
    return quantity_unit_item(position, result, {position.index: position}, relation or {})


def published(position, result, extra_claims=()):
    checked = enrich(position, result, {0: position}, {})
    stored = dict(checks=[dict(claim_id=c, state='not_found', required=True)
        for c in ['quantity', 'unit', *extra_claims]])
    return publish({'Статус аудита': 'Требуется ручная проверка'}, stored, 'key',
        {'stage': 'done', 'review': dict(checked, position_key='key')}, enabled=True), checked


def test_missing_numeric_item_can_be_closed_by_fresh_literal_cards_only():
    position, result = fixture()
    original = deepcopy(result)
    proof = bridge(position, result)
    assert proof['claim_ids'] == ['quantity', 'unit']
    assert proof['project_source_ids'] == ['p'] and proof['normative_source_ids'] == ['n']
    assert result == original
    report, checked = published(position, result)
    assert report['Статус аудита'] == 'ОК'
    assert sum(i['topic'] == TOPIC for i in checked['items']) == 1
    assert enrich(position, checked, {0: position}, {}) == checked


def test_mechanical_omission_marker_survives_readable_prose_and_replay():
    position, result = fixture()
    omission(result)
    assert result['evidence_trace'][0]['kind'] == OMISSION
    assert bridge(position, result)['claim_ids'] == ['quantity', 'unit']
    readable = readable_review(result)
    assert 's0' not in readable['items'][-1]['explanation']
    assert bridge(position, readable)
    report, checked = published(position, readable)
    assert report['Статус аудита'] == 'ОК'
    assert not any(i['state'] == 'needs_facts' for i in checked['items'])
    archived = next(e for e in checked['evidence_trace'] if e['kind'] == SUPERSEDED)
    assert archived['original_item'] == readable['items'][-1]
    assert archived['replacement_topic'] == TOPIC
    assert not any(e['kind'] == OMISSION for e in checked['evidence_trace'])
    assert enrich(position, checked, {0: position}, {}) == checked


def test_consumed_marker_cannot_close_even_an_identical_new_uncertain_statement():
    position, result = fixture()
    original = deepcopy(omission(result))
    _, checked = published(position, result)
    assert not any(i['state'] == 'needs_facts' for i in checked['items'])
    checked['items'].append(original)
    fresh = enrich(position, checked, {0: position}, {})
    assert not any(i['topic'] == TOPIC for i in fresh['items'])
    assert original in fresh['items']


def test_archived_omission_is_not_restored_when_numeric_proof_is_revoked():
    position, result = fixture()
    original = deepcopy(omission(result))
    _, checked = published(position, result)
    checked['sources'][0]['text'] = 'Верхний слой основания: площадь 10 м2'
    fresh = enrich(position, checked, {0: position}, {})
    assert original not in fresh['items']
    assert not any(i['topic'] == TOPIC for i in fresh['items'])
    assert fresh['parameter_cards'][0]['state'] != 'confirmed'
    assert any(e['kind'] == SUPERSEDED for e in fresh['evidence_trace'])


def test_unproved_unit_keeps_the_original_mixed_quantity_unit_omission_visible():
    position, result = fixture()
    original = deepcopy(omission(result))
    result['items'] = result['items'][1:]  # No selected norm to establish its scale.
    checked = enrich(position, result, {0: position}, {})
    assert original in checked['items']
    assert next(i for i in checked['items'] if i['topic'] == TOPIC)['claim_ids'] == ['quantity']
    assert not any(e['kind'] == SUPERSEDED for e in checked['evidence_trace'])


def test_live_item_validation_records_only_new_mechanical_omission():
    from normative_position_check import CheckedItem, validate_items
    position, result = fixture()
    original = item('confirmed')
    original['explanation'] = 'Количество и единица совпадают с ПД.'
    checked = validate_items([CheckedItem(**original)], result['sources'], {'quantity', 'unit'},
        evidence_trace=result['evidence_trace'])
    result['items'].extend(checked)
    assert checked[0]['state'] == 'needs_facts'
    assert len(result['evidence_trace']) == 1
    assert bridge(position, result)['claim_ids'] == ['quantity', 'unit']
    replay_trace = []
    validate_items([CheckedItem(**checked[0])], result['sources'], {'quantity', 'unit'},
        evidence_trace=replay_trace)
    assert replay_trace == []


@pytest.mark.parametrize('change', ['statement', 'claim', 'reference', 'state'])
def test_marker_cannot_be_reused_for_a_changed_claim(change):
    position, result = fixture()
    target = omission(result)
    if change == 'statement':
        target['explanation'] += ' Однако участок не установлен.'
    elif change == 'claim':
        target['claim_ids'].append('method')
    elif change == 'reference':
        target['project_source_ids'] = ['p']
    else:
        target['state'] = 'possible_mismatch'
    assert bridge(position, result) is None


@pytest.mark.parametrize('state,claims,refs', [
    ('needs_facts', ['quantity'], []),
    ('needs_facts', ['unit'], ['p']),
    ('possible_mismatch', ['quantity'], []),
    ('needs_facts', ['quantity', 'method'], []),
])
def test_real_uncertainty_contradiction_and_mixed_claims_are_not_bypassed(state, claims, refs):
    position, result = fixture()
    result['items'].append(item(state, claims, refs))
    assert bridge(position, result) is None
    assert published(position, result)[0]['Статус аудита'] != 'ОК'


def test_legacy_warning_text_without_server_marker_does_not_authorize_promotion():
    position, result = fixture()
    omission(result)
    result['evidence_trace'] = []
    assert bridge(position, result) is None


@pytest.mark.parametrize('original_state,claims,norm_ids', [
    ('needs_facts', ['quantity'], []),
    ('confirmed', ['quantity', 'method'], []),
    ('confirmed', ['quantity'], ['missing']),
])
def test_marker_only_records_original_confirmed_numeric_source_omission(original_state, claims, norm_ids):
    _, result = fixture()
    original = item(original_state, claims)
    original['normative_source_ids'] = norm_ids
    record_source_omission(original, {**original, 'state': 'needs_facts'}, result['sources'], result['evidence_trace'])
    assert result['evidence_trace'] == []


def test_absence_statement_cannot_be_mislabeled_as_missing_reference_only():
    _, result = fixture()
    omission(result, explanation='Количество в проекте не найдено.')
    assert result['evidence_trace'] == []


def test_saved_green_cards_and_shortcut_states_are_not_proof():
    position, result = fixture()
    result['parameter_cards'] = [dict(id='quantity', state='confirmed', found_value='12.50', found_unit='м2',
        source_ids=['p'], source_anchors=[dict(source_id='p', quote=result['sources'][0]['text'])])]
    result['local_work_basis'] = dict(quantity='12.50', thickness_cm='12', unit='м2',
        project_source_ids=['p'], normative_source_ids=['n'], norm_scale='100')
    result['parameter_proposals'] = []
    assert bridge(position, result) is None


@pytest.mark.parametrize('defect', ['quote', 'scope', 'dimension', 'ocr', 'version', 'value', 'duplicate_id'])
def test_quantity_requires_current_literal_scoped_readable_evidence(defect):
    position, result = fixture()
    if defect == 'quote':
        result['parameter_proposals'][0]['quote'] = 'Несуществующая площадь 12,50 м2'
    elif defect == 'scope':
        result['parameter_proposals'][0]['scope_state'] = 'unresolved'
    elif defect == 'dimension':
        position.unit = 'м3'
    elif defect == 'ocr':
        result['sources'][0]['origin'] = 'ocr_unverified'
    elif defect == 'version':
        result['sources'][0].pop('version_id')
    elif defect == 'value':
        position.volume = '12.51'
    else:
        result['sources'].append(deepcopy(result['sources'][0]))
    assert bridge(position, result) is None


def test_declared_rounding_is_allowed_but_not_a_real_quantity_difference():
    position, result = fixture()
    result['sources'][0]['text'] = result['sources'][0]['text'].replace('12,50', '12,501')
    result['parameter_proposals'][0].update(value='12.501', quote=result['sources'][0]['text'],
        scope_quote=result['sources'][0]['text'])
    assert bridge(position, result)
    position.volume = '12.49'
    assert bridge(position, result) is None


@pytest.mark.parametrize('defect', ['unselected', 'different_norm', 'different_pdf', 'bad_quote', 'unverified_ocr'])
def test_unit_scale_needs_the_selected_norm_and_its_literal_anchor(defect):
    position, result = fixture()
    if defect == 'unselected':
        result['items'] = []
    elif defect in {'different_norm', 'different_pdf'}:
        unrelated = {**result['sources'][1], 'id': 'other'}
        if defect == 'different_norm':
            unrelated['label'] = 'ГЭСН 27 · 27-04-001-02'
        else:
            unrelated['source_id'] = 'other-pdf'
        result['sources'].append(unrelated)
        result['parameter_proposals'][1]['source_id'] = 'other'
    elif defect == 'bad_quote':
        result['parameter_proposals'][1]['quote'] = 'Измеритель: 1000 м2'
    else:
        result['sources'][1]['origin'] = 'ocr_unreviewed'
    proof = bridge(position, result)
    assert proof['claim_ids'] == ['quantity'] and proof['normative_source_ids'] == []
    assert published(position, result)[0]['Статус аудита'] != 'ОК'


def test_quantity_bridge_never_closes_method_or_applicability():
    position, result = fixture()
    omission(result)
    result['items'].append(item('needs_facts', ['method']))
    assert bridge(position, result)['claim_ids'] == ['quantity', 'unit']
    assert published(position, result, ['method'])[0]['Статус аудита'] != 'ОК'
    result['items'][0]['state'] = 'needs_facts'
    assert bridge(position, result)['claim_ids'] == ['quantity']
    assert published(position, result)[0]['Статус аудита'] != 'ОК'


def test_parent_quantity_cannot_confirm_material_quantity():
    position, result = fixture()
    assert bridge(position, result, {'parent': {'index': 4}}) is None
    position.position_type = 'Материал'
    assert bridge(position, result) is None


def sum_fixture():
    position, result = fixture()
    cells = [dict(id=f'agent:p1:cell:{i}', kind='project', origin='ocr', version_id='pdf-v1', page=1,
        label='ПД', text='Верхний слой основания из ЩПС; площадь м2\n'+value,
        unit_verified=True, unit='м2', numeric_verified=True, rects=[[i/10, 0, (i+1)/10, .1]])
        for i, value in enumerate(('5.25', '7.25'))]
    text = 'Верхний слой основания из ЩПС; площадь м2\n5.25 + 7.25 = 12.50 м2'
    result['sources'][0].update(id='sum', text=text, origin='calculated_from_cells',
        numeric_verified=True, unit_verified=True, unit='м2', rects=[c['rects'][0] for c in cells])
    result['sources'].extend(cells)
    result['items'][0]['project_source_ids'] = ['sum']
    result['parameter_proposals'][0].update(source_id='sum', operation='sum', quote=text,
        scope_quote=text, operand_source_ids=[c['id'] for c in cells])
    return position, result


def test_sum_is_recalculated_from_distinct_physical_qualified_cells():
    position, result = sum_fixture()
    assert bridge(position, result)['claim_ids'] == ['quantity', 'unit']


@pytest.mark.parametrize('defect', ['number', 'unit', 'ocr', 'page', 'duplicate_cell', 'missing_operand'])
def test_saved_sum_flags_cannot_override_bad_operand(defect):
    position, result = sum_fixture()
    cell = result['sources'][2]
    if defect == 'number':
        cell['text'] = cell['text'].replace('5.25', '4.25')
    elif defect == 'unit':
        cell['unit_verified'] = False
    elif defect == 'ocr':
        cell['numeric_verified'] = False
    elif defect == 'page':
        cell['page'] = 2
    elif defect == 'duplicate_cell':
        cell['rects'] = result['sources'][3]['rects']
    else:
        result['sources'].pop()
    assert bridge(position, result) is None


def test_same_topic_without_server_proof_does_not_hide_a_real_dispute():
    position, result = fixture()
    result['items'].append(item(topic=TOPIC))
    assert bridge(position, result) is None
    _, checked = published(position, result)
    assert next(i for i in checked['items'] if i['topic'] == TOPIC)['state'] == 'needs_facts'


def test_replay_revokes_old_synthetic_confirmation_when_source_changes():
    position, result = fixture()
    checked = enrich(position, result, {0: position}, {})
    checked['sources'][0]['text'] = 'Другой проектный фрагмент: площадь 90 м2'
    fresh = enrich(position, checked, {0: position}, {})
    assert not any(i['topic'] == TOPIC for i in fresh['items'])
    assert fresh['parameter_cards'][0]['state'] != 'confirmed'
