"""Explicit joined operations cannot be lost behind a long work description."""
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from normative_preflight import compound_operation_queries, preflight_candidates

LAWN='Устройство газонов с планировкой площади, с внесением растительной земли h = 0,20 м и засевом смесью из многолетних трав (механизировано)'


def test_actual_lawn_retains_literal_object_and_normalizes_only_search_operation():
    assert compound_operation_queries(LAWN)==['посев газонов']


@pytest.mark.parametrize('query',[
    'Семена газонных трав (смесь) '+LAWN,
    'Смесь песка и щебня с поливом',
    'Устройство газонов без посева и с поливом',
    'Устройство газонов с посевом или укладкой рулонов',
    'Устройство газонов с посевом при необходимости',
    'Устройство газонов с посевом, кроме луговых',
    'Устройство покрытия h=5 см с уплотнением',
    'Устройство основания из ЩПС с уплотнением',
    'Устройство газонов с растительной землей и смесью трав',
    '27-04-003-06',
])
def test_material_descriptors_negations_alternatives_and_unparsed_work_stay_unchanged(query):
    assert compound_operation_queries(query)==[]


def test_query_count_is_bounded_and_keeps_explicit_operation_without_invented_machine():
    assert compound_operation_queries('Устройство газонов с посевом и поливом и уплотнением')==['посев газонов','полив газонов']


def hit(code,kind='norm'):
    return dict(id=code,code=code,entity_kind=kind,status='needs_facts',source_id='pdf',
        family='ГЭСН',table_code=code.rsplit('-',1)[0],checks=[dict(state='unknown')],
        automatic_application_allowed=False)


def test_bounded_catalogue_retains_primary_definitions_and_compound_candidates_without_applying_them():
    soil=[hit('47-01-046-01'),hit('47-01-046-03')]
    seeding=[hit('47-01-047-01'),hit('47-01-046-06')]
    before=deepcopy([*soil,*seeding])
    kb=Mock()
    def search(query,kind,facts,limit):
        if query=='посев газонов':
            return seeding[:limit]
        return (soil if kind=='GESN' else [hit('clause','clause')])[:limit]
    kb.search.side_effect=search
    with patch('normative_preflight.same_table_adjustments',return_value=[]):
        result=preflight_candidates(kb,[LAWN,LAWN],{'unit':'м2'},definition_queries=[LAWN])
    assert {item['code'] for item in result}>={'47-01-046-03','47-01-047-01'}
    assert len(result)<=12 and all(not item['automatic_application_allowed'] for item in result)
    assert [*soil,*seeding]==before
    assert len([call for call in kb.search.call_args_list if call.args[0]=='посев газонов'])==1


def test_exact_references_and_twelve_candidate_limit_are_not_overridden():
    exact=[hit('27-04-003-06')]
    kb=Mock()
    def search(query,kind,facts,limit):
        if query=='27-04-003-06':
            return exact
        return [hit(query+str(index), 'norm' if kind=='GESN' else 'clause') for index in range(limit)]
    kb.search.side_effect=search
    with patch('normative_preflight.same_table_adjustments',return_value=[]):
        result=preflight_candidates(kb,['27-04-003-06',LAWN,*[f'query {i}' for i in range(10)]],{},definition_queries=[LAWN])
    assert len(result)==12 and result[0] is exact[0]


def test_comment_query_does_not_gain_work_operation_searches():
    kb=Mock()
    kb.search.return_value=[]
    with patch('normative_preflight.same_table_adjustments',return_value=[]):
        assert preflight_candidates(kb,[LAWN],{},definition_queries=[])==[]
    assert not any(call.args[0]=='посев газонов' for call in kb.search.call_args_list)
