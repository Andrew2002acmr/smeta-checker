from unittest.mock import patch

from normative_preflight import preflight_candidates


def item(identity,kind='norm'):
    return dict(id=identity,entity_kind=kind)


def test_work_definition_survives_many_material_queries_and_adjustment_gets_room():
    work=[item('work'),item('work-alternative'),item('work-clause','clause')]
    material=[item(f'material-{i}') for i in range(20)]
    with patch('normative_preflight.work_variants',return_value=work),\
            patch('normative_preflight.search_evidence',return_value=material),\
            patch('normative_preflight.same_table_adjustments',return_value=[item('step')]):
        result=preflight_candidates(None,['material','owning-work'],{},definition_queries=['owning-work'],limit=6)
    assert result[0]['id']=='work'
    assert len(result)==6 and any(i['id']=='step' for i in result)


def test_adjustment_already_retrieved_but_outside_initial_budget_is_retained():
    work=[item('work'),item('work-alternative')]
    material=[item(f'material-{i}') for i in range(10)]+[item('step')]
    with patch('normative_preflight.work_variants',return_value=work),\
            patch('normative_preflight.search_evidence',return_value=material),\
            patch('normative_preflight.same_table_adjustments',return_value=[item('step')]):
        result=preflight_candidates(None,['material','owning-work'],{},definition_queries=['owning-work'],limit=6)
    assert len(result)==6 and sum(i['id']=='step' for i in result)==1
