from normative_code_tokens import code_tokens
from normative_retrieval import terms


def test_alias_preserves_spaced_grade_and_mixed_alphabet_without_modifying_source():
    raw='Покрытие А22 НТ на БНД 70/100'
    assert 'а22нт' in code_tokens(raw)
    assert 'а22нт' in code_tokens('A22HT')
    assert 'а22нт' in terms('а22нт')
    assert raw=='Покрытие А22 НТ на БНД 70/100'
    assert 'м800' in code_tokens('M800 НА ОСНОВАНИИ')
    assert 'бр100.30.18' in code_tokens('БР 100,30,18')


def test_alias_does_not_turn_common_parameter_words_into_material_codes():
    assert not code_tokens('Объём 12 м3; СЛОЙ 12 см; площадь 25 м2; ПК0+20')
    assert 'а22нт' not in code_tokens('А22 НП')
    assert 'а22нп' not in code_tokens('А22 НТ')
