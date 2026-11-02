from unittest.mock import Mock,patch

import pytest

from normative_machine_query import machine_query
from normative_preflight import work_variants


@pytest.mark.parametrize('number,word',[(1,'первого'),(2,'второго'),(3,'третьего'),(4,'четвертого')])
def test_machine_class_is_reworded_without_touching_model_thickness_or_section(number,word):
    query=f'Тротуары: асфальтоукладчиком Vögele SUPER 1600 {number} типоразмера, h=5 см; площадь2843 м2'
    expected=query.replace(f'{number} типоразмера',f'{word} типоразмера')
    assert machine_query(query)==expected
    assert machine_query(expected)==expected


@pytest.mark.parametrize('query',[
    'Асфальтоукладчик Vögele SUPER1600, h=1 см',
    'Асфальтоукладчик не 1 типоразмера',
    'Асфальтоукладчики 1–3 типоразмера',
    'Асфальтоукладчики 1 и 2 типоразмера',
    'Асфальтоукладчики 1/2 типоразмера',
    'Комплект из 2 типоразмеров',
    'Не менее 2 типоразмера',
    'Устройство 2,5 типоразмера',
    'Устройство 11 типоразмера',
    'ГОСТ58406.2-2020, норма27-06-031-01',
])
def test_numbers_negations_ranges_and_unknown_classes_are_not_reinterpreted(query):
    assert machine_query(query)==query


def test_generic_equipment_class_and_conflicting_classes_remain_explicit():
    assert machine_query('Экскаватором 2-го типоразмера')=='Экскаватором второго типоразмера'
    assert machine_query('Работа 3 типоразмера; комментарий 2 типоразмера')==(
        'Работа третьего типоразмера; комментарий второго типоразмера')


def test_bounded_preflight_uses_equivalent_query_without_promoting_candidates():
    rows=[dict(id='native-norm',state='needs_facts',automatic_application_allowed=False)]
    kb=Mock()
    kb.search.return_value=rows
    query='Покрытие тротуара асфальтоукладчиком 1 типоразмера'
    output=work_variants(kb,query,{'unit':'м2'})
    assert kb.search.call_args_list[0].args==('Покрытие тротуара асфальтоукладчиком первого типоразмера',)
    assert kb.search.call_args_list[0].kwargs['limit']==2
    assert kb.search.call_args_list[1].kwargs['limit']==1
    assert all(item['state']=='needs_facts' and not item['automatic_application_allowed'] for item in output)


def test_machine_comment_query_is_normalized_too_without_rewriting_explicit_document_reference():
    from normative_preflight import preflight_candidates
    with patch('normative_preflight.search_evidence',return_value=[]) as search:
        preflight_candidates(Mock(),['Vögele SUPER 1600 1 типоразмера','ГОСТ 123-2020 1 типоразмера'],{})
    assert search.call_args_list[0].args[1]=='Vögele SUPER 1600 первого типоразмера'
    assert search.call_args_list[1].args[1]=='ГОСТ 123-2020 1 типоразмера'
