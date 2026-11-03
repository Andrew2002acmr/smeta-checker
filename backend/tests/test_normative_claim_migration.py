from copy import deepcopy

import pytest

from audit_models import VorPosition
from audit_predicates import technical_specs,comment_metadata
from human_basis import position_key
from normative_claim_migration import normalize_matching,apply_review_migration,migrated_matching,VERSION
from normative_verdict import publish


def lawn():
    return VorPosition(index=45,work_name='Устройство газонов с планировкой площади, с внесением растительной земли '
        'h = 0,20 м и засевом смесью из многолетних трав (механизировано)',volume='1931',unit='м2',
        comment='Агрегат для травосеяния СЗМ Ника-4.\nАвтоматизированная система расчёта программным продуктом "Indor CAD".',
        position_type='Работа')


def specs(position):
    return technical_specs(position.work_name,position.comment,position.position_type,[],position.formula)


def test_lawn_keeps_explicit_thickness_and_equipment_method_but_not_software_or_brand_literal():
    parsed=specs(lawn())
    assert not any(s.property_label=='area' for s in parsed)
    thickness=next(s for s in parsed if s.property_label=='thickness')
    assert thickness.expected_text=='0,20 м' and thickness.required
    metadata=next(s for s in parsed if s.claim_id=='attribute:comment:condition')
    assert metadata.predicate=='advisory' and not metadata.required
    method=next(s for s in parsed if s.kind=='method')
    assert method.required and method.expected_text=='Агрегат для травосеяния'


@pytest.mark.parametrize('comment',[
    'Расчёт выполнен программным продуктом Indor CAD.',
    'Расчёт выполнен программным продуктом Indor CAD 2022.',
    'Проектирование выполнено программным комплексом CAD-4.',
])
def test_software_version_is_not_a_physical_requirement(comment):
    assert comment_metadata(comment)==[]
    assert all(not s.required for s in technical_specs('',comment,'Работа',[]))


@pytest.mark.parametrize('comment',[
    'Бульдозер мощностью 79 кВт. Каток Hamm HD130.',
    'Укладка а/укладчиком Vögele SUPER 1600 1 типоразмера',
    'Температура смеси не менее 140 градусов',
    'Расчёт программным продуктом CAD 2022, расход 2 кг/м2',
    'Агрегат для травосеяния СЗМ Ника-4 при скорости 8 км/ч',
    'Агрегат для травосеяния СЗМ Ника-4 выполнять посев дважды',
    'Только программным продуктом CAD 2022 выполнять расчёт',
    'Расчёт выполнен программным продуктом CAD 2022 с учетом ветрового давления',
    'Агрегат для травосеяния СЗМ Ника-4.\nНеизвестное инженерное условие 8',
])
def test_machines_metrics_mandatory_and_unknown_conditions_remain_required(comment):
    assert comment_metadata(comment) is None
    assert any(s.required for s in technical_specs('',comment,'Работа',[]))


def test_true_area_and_unknown_wrong_dimension_are_not_silently_dropped():
    area=technical_specs('Площадь 100 м2','','Работа',[])
    assert area[0].property_label=='area' and area[0].expected_text=='100 м2'
    wrong=technical_specs('Площадь 100 м','','Работа',[])
    assert wrong[0].required and wrong[0].predicate=='unsupported'


def frozen():
    return dict(status='Неполная проверка',checks=[
        dict(claim_id='geometry:name:area:1',kind='geometry',state='review_required',required=True,facts=[]),
        dict(claim_id='attribute:comment:condition',kind='attribute',state='not_found',required=True,facts=[]),
        dict(claim_id='method',kind='method',state='not_found',required=True,facts=[])])


def test_frozen_matching_is_immutable_and_replacements_are_required_not_preconfirmed():
    position=lawn()
    original=frozen()
    snapshot=deepcopy(original)
    normalized=normalize_matching(position,original)
    assert original==snapshot
    checks={c['claim_id']:c for c in normalized['checks']}
    assert checks['geometry:name:area:1']['state']=='not_applicable'
    assert checks['attribute:comment:condition']['required'] is False
    for key in ('geometry:name:thickness:1','method:comment:equipment:1'):
        assert checks[key]['required'] and checks[key]['state']=='not_found'
    assert checks['method']==original['checks'][2]


def test_local_review_archives_old_claims_and_never_disables_unknown_or_technical_contradictions():
    position=lawn()
    old=dict(topic='Wrong area',state='possible_mismatch',required=True,
        claim_ids=['geometry:name:area:1','method'],project_source_ids=['p'],normative_source_ids=['n'],explanation='old')
    result=dict(items=[old])
    apply_review_migration(position,result)
    assert result['items'][0]['claim_ids']==['method'] and result['items'][0]['state']=='possible_mismatch'
    assert result['items'][1]['required'] is False and result['items'][1]['state']=='needs_facts'
    event=next(e for e in result['evidence_trace'] if e['kind']==VERSION)
    assert event['original_items']==[old]
    again=deepcopy(result)
    apply_review_migration(position,result)
    assert result==again
    assert migrated_matching(frozen(),result,position_key(position))==normalize_matching(position,frozen())
    assert migrated_matching(frozen(),result,'different-position')==frozen()
    result['evidence_trace'][0]['retired']['method']='fake'
    assert migrated_matching(frozen(),result,position_key(position))==frozen()


def test_metadata_alone_does_not_block_but_new_method_and_thickness_must_have_proof():
    position=lawn()
    original=frozen()
    claims=['method','method:comment:equipment:1','geometry:name:thickness:1','normative:applicability']
    result=dict(pipeline_version=4,execution_state='complete',items=[
        dict(topic='Supported',state='confirmed',claim_ids=claims,project_source_ids=['p'],normative_source_ids=['n'])],
        parameter_cards=[dict(id='thickness',state='confirmed',required=True)],sources=[])
    apply_review_migration(position,result)
    key=position_key(position)
    report=publish({'Статус аудита':'Требуется ручная проверка'},original,key,
        {'stage':'done','review':dict(result,position_key=key)},enabled=True)
    assert report['Статус аудита']=='ОК'
    result['items'][0]['claim_ids'].remove('method:comment:equipment:1')
    report=publish({'Статус аудита':'Требуется ручная проверка'},original,key,
        {'stage':'done','review':dict(result,position_key=key)},enabled=True)
    assert report['Статус аудита']=='Требуется ручная проверка'


@pytest.mark.parametrize('comment,method',[
    ('Уплотнение виброкатком Hamm HD130','Уплотнение виброкатком'),
    ('Уплотнение основания производится тандемным виброкатком Hamm HD130.',
        'Уплотнение основания производится тандемным виброкатком'),
    ('Укладка а/укладчиком Vögele SUPER 1600','Укладка а/укладчиком'),
    ('Уплотнение катком Bomag BW-161','Уплотнение катком'),
])
def test_whole_action_machine_name_preserves_required_function(comment,method):
    parsed=technical_specs('',comment,'Работа',[])
    assert comment_metadata(comment)==[method]
    required=[s for s in parsed if s.required]
    assert len(required)==1 and required[0].kind=='method'
    assert required[0].expected_text==method
    assert next(s for s in parsed if s.claim_id=='attribute:comment:condition').predicate=='advisory'


@pytest.mark.parametrize('tail',[
    ' массой 13 т',' шириной 2 м',' при температуре 20 градусов',' мощностью 79 кВт',
    ' с вибрацией',' без вибрации',' 3 типоразмера',' выполнять проходы дважды',
    '; Неизвестное инженерное условие 8',
])
def test_machine_name_never_hides_metrics_conditions_or_unknown_tail(tail):
    comment='Уплотнение виброкатком Hamm HD130'+tail
    assert comment_metadata(comment) is None
    parsed=technical_specs('',comment,'Работа',[])
    assert any(s.required and not s.claim_id.startswith('method:comment:equipment:') for s in parsed)


def machine_with_power():
    return VorPosition(index=33,work_name='Устройство основания бульдозером мощностью 79 кВт',
        volume='2843',unit='м2',position_type='Работа',comment=
        'Устройство основания производится бульдозером мощностью 79 кВт\n'
        'Уплотнение основания производится тандемным виброкатком Hamm HD130.')


def test_mixed_comment_keeps_power_and_unknown_requirements_and_separate_function():
    position=machine_with_power()
    parsed=specs(position)
    condition=next(s for s in parsed if s.claim_id=='attribute:comment:condition')
    assert condition.required and '79 кВт' in condition.expected_text
    assert 'Hamm' not in condition.expected_text
    method=next(s for s in parsed if s.claim_id=='method:comment:equipment:1')
    assert method.required and method.expected_text.endswith('тандемным виброкатком')
    assert not next(s for s in parsed if s.claim_id=='attribute:comment:equipment_names').required
    position.comment+='\nНеизвестное инженерное условие 8'
    condition=next(s for s in specs(position) if s.claim_id=='attribute:comment:condition')
    assert condition.required and 'Неизвестное инженерное условие 8' in condition.expected_text


def test_frozen_mixed_claim_is_never_retired_or_promoted_by_machine_name_cleanup():
    position=machine_with_power()
    old=dict(checks=[dict(claim_id='attribute:comment:condition',kind='attribute',
        state='review_required',required=True,facts=[])])
    normalized=normalize_matching(position,old)
    assert normalized['checks'][0]==old['checks'][0]
    method=normalized['checks'][1]
    assert method['claim_id']=='method:comment:equipment:1'
    assert method['required'] and method['state']=='not_found'
    item=dict(topic='Machine condition',state='possible_mismatch',required=True,
        claim_ids=['attribute:comment:condition'],project_source_ids=['p'],normative_source_ids=['n'])
    review=dict(items=[deepcopy(item)])
    apply_review_migration(position,review)
    assert review['items']==[item]
    trace=review['evidence_trace'][0]
    assert 'attribute:comment:condition' not in trace['retired'] and trace['inputs']['comment']==position.comment
    assert migrated_matching(old,review,position_key(position))==normalized
    saved=deepcopy(review)
    apply_review_migration(position,review)
    assert saved==review
