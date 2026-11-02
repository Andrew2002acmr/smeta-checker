from normative_parameter_anchors import parameter_candidates,resolve_anchors
from workspace_models import NormativeParameterProposal
from normative_position_check import AgentAnswer,validate_items


def sources():
    return [dict(id='p',kind='project',origin='native',text='Средний слой основания — Смесь 0/63 h=20 см.\nНижний слой основания — Смесь 0/31,5 h=24 см.'),
        dict(id='n',kind='normative',origin='native',text='Работа. Измеритель: 1000 м2')]


def test_thickness_adjustment_uses_literal_base_step_and_requested_thickness():
    from normative_parameter_anchors import thickness_adjustments
    items=[dict(id='base',kind='normative',origin='native',source_id='pdf',label='ГЭСН 01-02-003-01',
        text='Измеритель: 1000 м2\n01-02-003-01 Основание толщиной 16 см.'),
        dict(id='step',kind='normative',origin='native',source_id='pdf',label='ГЭСН 01-02-003-02',
        text='Измеритель: 1000 м2\n01-02-003-02 На каждый 1 см изменения толщины слоя добавлять или исключать к норме 01-02-003-01.')]
    result=thickness_adjustments('Устройство слоя h=11 см',items)
    assert result[0]['signed_steps']=='-5'
    assert result[0]['base_source_id']=='base' and not result[0]['scope_verified']
    items[1]['label']='99-99-999-01'
    assert thickness_adjustments('Устройство слоя h=11 см',items)==[]


def test_native_layer_anchors_preserve_exact_text_and_layer_identity():
    candidates=parameter_candidates(sources())
    lower=next(c for c in candidates if c['parameter']=='thickness' and c['value']=='24')
    assert lower['quote']=='h=24 см'
    assert lower['scope_quote']=='Нижний слой основания — Смесь 0/31,5 h=24 см'
    assert lower['scope_quote'] in sources()[0]['text']


def test_quote_can_be_restored_only_from_a_unique_anchor_in_the_same_source():
    proposal=NormativeParameterProposal(parameter='thickness',value='24',unit='см',source_id='p',
        quote='Нижний слой ... 24 см',scope_quote='Смесь 0/31,5 ... 24 см',scope_state='verified')
    resolve_anchors([proposal],sources(),[],work_scope='Смесь 0/31,5',height_cm='24')
    assert proposal.quote=='h=24 см'
    assert proposal.scope_quote in sources()[0]['text']
    unresolved=proposal.model_copy(update={'quote':'...','scope_state':'unresolved'})
    resolve_anchors([unresolved],sources(),[],work_scope='Смесь 0/31,5',height_cm='24')
    assert unresolved.scope_state=='unresolved'


def test_other_layer_or_source_cannot_repair_a_fabricated_quote():
    proposal=NormativeParameterProposal(parameter='thickness',value='20',unit='см',source_id='p',
        quote='bad',scope_state='verified')
    resolve_anchors([proposal],sources(),[],work_scope='Смесь 0/31,5',height_cm='24')
    assert proposal.quote=='bad'
    proposal.source_id='invented'
    resolve_anchors([proposal],sources(),[],work_scope='Смесь 0/31,5',height_cm='24')
    assert proposal.quote=='bad'


def test_unit_scale_comes_only_from_selected_normative_sources_and_compatible_units():
    items=AgentAnswer(summary='Норма',items=[dict(topic='Норма',state='confirmed',explanation='Норма выбрана',
        claim_ids=['normative:applicability'],project_source_ids=['p'],normative_source_ids=['n'])]).items
    proposals=resolve_anchors([],sources(),items,owner_unit='м2')
    assert len(proposals)==1 and proposals[0].value=='1000'
    assert proposals[0].source_id=='n'
    assert resolve_anchors([],sources(),[],owner_unit='м2')==[]
    assert resolve_anchors([],sources(),items,owner_unit='м3')==[]


def test_related_material_rate_is_informational_for_the_parent_work_only():
    answer=AgentAnswer(summary='Коэффициент материала',items=[dict(topic='Расход',state='needs_facts',explanation='Нет коэффициента',
        claim_ids=['related:1:rate'],project_source_ids=[],normative_source_ids=[])])
    result=validate_items(answer.items,[],{'related:1:rate'})
    assert result[0]['required'] is False
    result=validate_items(answer.items,[],set())
    assert result[0]['required'] is True
