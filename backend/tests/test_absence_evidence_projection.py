from copy import deepcopy

from normative_verdict import absence_claim, readable_review


def test_method_not_confirmed_in_project_is_not_a_proven_vor_error():
    item=dict(topic='Способ работы',state='possible_mismatch',required=True,
        claim_ids=['method','attribute:comment:condition'],project_source_ids=['p'],normative_source_ids=['n'],
        explanation='Ресурсы нормы содержат автогрейдер и катки. Ручной способ и пневмотрамбовки '
            'нормой не предусмотрены; подтверждения ручной технологии в ПД нет.')
    review=dict(state='possible_mismatch',summary='Сверка',items=[item],sources=[])
    original=deepcopy(review)
    assert absence_claim(item)
    corrected=readable_review(review)
    assert corrected['state']=='needs_facts'
    assert corrected['items'][0]['state']=='needs_facts'
    assert corrected['items'][0]['required'] is True
    assert corrected['items'][0]['explanation']==item['explanation']
    assert corrected['evidence_trace'][0]['original_item']==item
    assert readable_review(corrected)==corrected
    assert review==original


def test_positive_project_counterfact_and_explicit_normative_ban_remain_mismatch():
    item=dict(state='possible_mismatch',claim_ids=['method'],
        explanation='Для данного участка ПД прямо требует механизированную укладку. '
            'ВОР назначает ручную укладку; процитированный пункт запрещает её в этих условиях.')
    assert not absence_claim(item)
    assert readable_review(dict(state='possible_mismatch',items=[item],sources=[]))['state']=='possible_mismatch'
