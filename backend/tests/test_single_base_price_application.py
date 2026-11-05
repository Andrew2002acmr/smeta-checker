from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from audit_models import PositionSource, VorPosition
from normative_conditional_selection import selection_findings
from normative_layer_assembly import single_base_price_application, single_base_price_replay_supported
from normative_parameters import build_cards
from normative_thickness_adjustment import (AdjustmentProposal, findings,
    proposal_events)
from test_normative_conditional_selection import CLAUSE


def fixture():
    position = VorPosition(index=8, position_type='Работа', unit='м2', volume='80',
        work_name='Устройство слоя основания, h=20 см бульдозером с уплотнением',
        position_source=PositionSource.model_validate(dict(document_sha256='a'*64, format='xlsx', sheet='S',
            row=20, header_row=1, position_id='base',
            sections=[dict(address='A2', text='Раздел. Тротуары')])))
    header = 'Устройство тротуаров. Улица | Основание - ЩПС 0/31,5, м² (20см)'
    sources = [dict(id='head', kind='project', origin='ocr_verified_header', version_id='pd',
        page=90, text=header, numeric_verified=True,
        verified_parameters=[dict(parameter='thickness', value='20', unit='см', quote='(20см)')]),
        dict(id='area', kind='project', origin='ocr', version_id='pd', page=90,
            text=header+'\nВсего: 80 м2', numeric_verified=True, unit_verified=True, unit='м2'),
        dict(id='native', kind='project', origin='native', version_id='pd', page=50,
            text='Тип V — Тротуар\n- Покрытие — А5Вл Асфальтобетон, h=5 см;\n'
                '- Основание — Щебёночно-песчаная смесь для оснований фр. 0/31,5, h=20 см;\n'
                'Конструкция трамвайного полотна\nДругая конструкция.'),
        dict(id='clause', kind='normative', origin='extracted_native_unreviewed',
            source_id='norm-pdf', label='ГЭСН 27 · 1.27.18', page=2, text=CLAUSE)]
    adjustments = []
    for code, role in [('06','нижний'), ('07','верхний')]:
        sources.append(dict(id='base'+code, kind='normative', origin='extracted_native_unreviewed',
            source_id='norm-pdf', label='ГЭСН 27 · 27-04-003-'+code, page=25,
            text='Таблица ГЭСН 27-04-003 Устройство оснований из щебеночно-песчаных смесей\n'
                'Измеритель: 1000 м2\n27-04-003-'+code+
                ' непрерывной гранулометрии С-4 и С-6, двухслойных '+role+' слой толщиной 15 см'))
        adjustments.append(AdjustmentProposal(base_source_id='base'+code, adjustment_source_id='step',
            signed_steps=-5, base_quote='толщиной 15 см', adjustment_quote=
                '27-04-003-08 На каждый 1 см изменения толщины слоя добавлять или исключать к нормам с 27-04-003-05 по 27-04-003-07'))
    sources.append(dict(id='step', kind='normative', origin='extracted_native_unreviewed', source_id='norm-pdf',
        label='ГЭСН 27 · 27-04-003-08', page=25, text='Измеритель: 1000 м2\n'+adjustments[0].adjustment_quote))
    result: dict[str, Any] = dict(pipeline_version=24, execution_state='complete', state='confirmed', sources=sources,
        items=[dict(topic='Применимость', state='confirmed', claim_ids=['normative:applicability'],
            project_source_ids=['area','head'], normative_source_ids=['clause','base06','base07','step'])],
        parameter_proposals=[dict(parameter='quantity', value='80', unit='м2', source_id='area',
            scope_state='verified', quote=header+'\nВсего: 80 м2', scope_quote=header+'\nВсего: 80 м2'),
            dict(parameter='thickness', value='20', unit='см', source_id='head', scope_state='verified',
                quote='(20см)', scope_quote=header),
            dict(parameter='unit_scale', value='1000', unit='м2', source_id='base06', scope_state='verified',
                quote='Измеритель: 1000 м2', scope_quote='Измеритель: 1000 м2')],
        evidence_trace=proposal_events(adjustments))
    return position, result, {position.index: position}


def test_one_native_base_can_have_two_explicit_priced_courses_without_new_physical_layers():
    position, result, positions = fixture()
    before = deepcopy(result)
    proof = single_base_price_application(position, result, positions, {})
    assert proof and Decimal(proof['total_m']) == Decimal('.20')
    assert [Decimal(c['effective_m']) for c in proof['courses']] == [Decimal('.10')]*2
    assert proof['price_application_only'] and proof['physical_layer_count'] == 1
    assert proof['project_construction']['base_quote'] in result['sources'][2]['text']
    assert not selection_findings(position, result, positions, {})
    assert not findings(position, result, positions, {})
    assert result == before and len(positions) == 1
    cards = build_cards(position, result, positions, {})
    assert {c['id'] for c in cards if c['state']=='confirmed'} >= {'quantity','thickness'}


def source(result, sid):
    return next(s for s in result['sources'] if s['id']==sid)


@pytest.mark.parametrize('change',[
    'native_missing','other_site','ambiguous_block','two_bases','native_pin','area_pin',
    'quoted_norm','unequal_measure','wrong_steps','negative_course','missing_course',
    'duplicate_course','false_native_family','false_norm_family','norm_pin','open_block',
    'other_work','threshold_not_exceeded','scope_mismatch',
    'source_hash','condition_pin','native_truncated',
])
def test_incomplete_or_conflicting_literal_proof_is_rejected(change):
    position, result, positions = fixture()
    native = source(result,'native')
    if change=='native_missing':
        result['sources'].remove(native)
        result['evidence_trace'].append(dict(kind='single_base_price_application_v1', verified=True))
    elif change=='other_site':
        native['text']=native['text'].replace('Тротуар','Съезды')
    elif change=='ambiguous_block':
        native['text']+='\n'+native['text']
    elif change=='two_bases':
        native['text']=native['text'].replace('Конструкция трамвайного',
            '- Основание — Щебёночно-песчаная смесь фр. 0/63, h=10 см;\nКонструкция трамвайного')
    elif change=='native_pin':
        native['version_id']='another-pdf'
    elif change=='area_pin':
        source(result,'area')['version_id']='another-pdf'
    elif change=='quoted_norm':
        result['evidence_trace'][0]['adjustment_quote']='На каждый 2 см добавлять к норме 27-04-003-06'
    elif change=='unequal_measure':
        source(result,'base07')['text']=source(result,'base07')['text'].replace('1000 м2','100 м2')
    elif change=='wrong_steps':
        result['evidence_trace'][0]['signed_steps']=-4
    elif change=='negative_course':
        result['evidence_trace'][0]['signed_steps']=-15
    elif change=='missing_course':
        result['evidence_trace'].pop()
    elif change=='duplicate_course':
        result['evidence_trace'][1]=deepcopy(result['evidence_trace'][0])
    elif change=='false_native_family':
        native['text']=native['text'].replace('Щебёночно-песчаная смесь','Песок')
    elif change=='false_norm_family':
        source(result,'base06')['text']=source(result,'base06')['text'].replace('щебеночно-песчаных','бетонных')
    elif change=='norm_pin':
        source(result,'step')['source_id']='another-norm-pdf'
    elif change=='open_block':
        native['text']=native['text'].split('Конструкция трамвайного')[0]
    elif change=='other_work':
        other=position.model_copy(deep=True)
        other.index=9
        assert other.position_source is not None
        other.position_source.row=21
        positions[9]=other
    elif change=='threshold_not_exceeded':
        source(result,'clause')['text']=source(result,'clause')['text'].replace('18 см','22 см')
    elif change=='scope_mismatch':
        source(result,'head')['text']=source(result,'head')['text'].replace('Тротуар','Съезд').replace('тротуар','съезд')
    elif change=='source_hash':
        changed='base06:full:'+'f'*20
        source(result,'base06')['id']=changed
        result['evidence_trace'][0]['base_source_id']=changed
        result['parameter_proposals'][2]['source_id']=changed
        result['items'][0]['normative_source_ids'][1]=changed
    elif change=='condition_pin':
        source(result,'clause')['version_id']='another-norm-version'
    elif change=='native_truncated':
        native['excerpt_truncated']=True
    assert single_base_price_application(position,result,positions,{}) is None


def test_native_absence_keeps_current_negative_guards_and_positive_proof_is_audited():
    from normative_conditional_selection import apply_guard
    position, result, positions = fixture()
    missing = deepcopy(result)
    missing['sources'].remove(source(missing,'native'))
    assert selection_findings(position,missing,positions,{})
    assert findings(position,missing,positions,{})
    # The located typed OCR header is an independent numeric premise; its
    # origin tag alone cannot substitute for the actual verified parameter.
    for change in ('unverified_number','missing_typed_fact','wrong_typed_value','nonliteral_typed_quote'):
        invalid=deepcopy(missing)
        head=source(invalid,'head')
        if change=='unverified_number':
            head['numeric_verified']=False
        elif change=='missing_typed_fact':
            head['verified_parameters']=[]
        elif change=='wrong_typed_value':
            head['verified_parameters'][0]['value']='21'
        else:
            head['verified_parameters'][0]['quote']='В другой конструкции 20 см'
        assert findings(position,invalid,positions,{})==[]
    assert not apply_guard(position,result,positions,{})
    assert result['items'][0]['state']=='confirmed'
    traces=[e for e in result['evidence_trace'] if e['kind']=='single_base_price_application_v1']
    assert len(traces)==1 and len(traces[0]['courses'])==2


def stored_fixture():
    from normative_conditional_selection import apply_guard
    position,result,positions=fixture()
    result['parameter_cards']=build_cards(position,result,positions,{})
    assert not apply_guard(position,result,positions,{},cards=result['parameter_cards'])
    assert single_base_price_replay_supported(position,result)
    return position,result


def test_replay_gate_reconstructs_current_sources_proposals_cards_and_position_bank():
    position,result=stored_fixture()
    before=deepcopy(result)
    assert single_base_price_replay_supported(position,result)
    assert result==before
    assert single_base_price_replay_supported(position,dict(evidence_trace=[]))


@pytest.mark.parametrize('change',[
    'native_removed','native_mutated','clause_removed','clause_mutated','source_quality',
    'proposal_mutated','proposal_removed','extra_proposal','card_mutated','card_removed',
    'current_position','position_bank','position_bank_hash','proof_hash','duplicate_proof',
])
def test_publisher_replay_fails_closed_after_any_bound_input_changes(change):
    position,result=stored_fixture()
    if change=='native_removed':
        result['sources'].remove(source(result,'native'))
    elif change=='native_mutated':
        source(result,'native')['text']=source(result,'native')['text'].replace('h=20 см','h=22 см')
    elif change=='clause_removed':
        result['sources'].remove(source(result,'clause'))
    elif change=='clause_mutated':
        source(result,'clause')['text']=source(result,'clause')['text'].replace('18 см','19 см')
    elif change=='source_quality':
        source(result,'area')['numeric_verified']=False
    elif change=='proposal_mutated':
        result['evidence_trace'][0]['signed_steps']=-4
    elif change=='proposal_removed':
        result['evidence_trace'].pop(0)
    elif change=='extra_proposal':
        result['evidence_trace'].append(deepcopy(result['evidence_trace'][0]))
    elif change=='card_mutated':
        result['parameter_cards'][0]['found_value']='999'
    elif change=='card_removed':
        result['parameter_cards']=[]
    elif change=='current_position':
        position.volume='81'
    else:
        proof=next(e for e in result['evidence_trace'] if e['kind']=='single_base_price_application_v1')
        if change=='position_bank':
            proof['rebuild_positions'][0]['volume']='81'
        elif change=='position_bank_hash':
            proof['workbook_sha256']='f'*64
        elif change=='proof_hash':
            proof['sha256']='f'*64
        elif change=='duplicate_proof':
            result['evidence_trace'].append(deepcopy(proof))
    assert not single_base_price_replay_supported(position,result)
