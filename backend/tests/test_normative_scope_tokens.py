from normative_scope_tokens import scope_tokens,compare_scopes
from normative_retrieval import DocumentSearch


def test_scope_ids_keep_numbers_that_general_word_stemming_omits():
    assert scope_tokens('Участок №01, секция Б, путь 2')=={'scope:участок:1','scope:секция:б','scope:путь:2'}
    assert scope_tokens('Объём 431,61 м2, позиция 18, работа с уплотнением')==set()
    assert scope_tokens('участок с уплотнением')==set()


def test_chainage_aliases_are_contextual_and_do_not_change_literal_pdf_text():
    assert scope_tokens('ΠК0+22,430 — ПК1+00')=={'scope:пк:0+22.43','scope:пк:1+0'}
    assert not scope_tokens('22,43 м2')


def test_same_label_different_id_is_not_a_fuzzy_match():
    assert compare_scopes({'scope:участок:1'},{'scope:участок:12'})=='conflicting'
    assert compare_scopes({'scope:участок:1'},{'scope:съезд:1'})=='not_observed'
    assert compare_scopes({'scope:участок:1'},{'scope:участок:1'})=='matched'
    assert compare_scopes({'scope:участок:1'},{'scope:участок:1','scope:участок:2'})=='includes_requested_and_other_scopes'


def test_identifier_specific_page_survives_generic_schedule_marker_bonus():
    texts=['Ведомость материалов. Участок №'+str(i) for i in range(1,14)]
    original=list(texts)
    found=DocumentSearch(texts).search('Участок №12',tables=True)
    assert found[0]['page']==12 and texts==original
