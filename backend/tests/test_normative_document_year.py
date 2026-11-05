from normative_corpus import _document_matches


def test_two_digit_gost_year_is_an_exact_document_reference():
    assert _document_matches("8267-93", "ГОСТ 8267-93")
    assert _document_matches("23558-94", "ГОСТ 23558-94")
    assert _document_matches("6665-91", "ГОСТ 6665-91 с изменениями")
    assert not _document_matches("8267-93", "ГОСТ 8267-82")
    assert not _document_matches("23558-94", "ГОСТ 23558-79")


def test_full_year_and_undated_references_preserve_scope():
    assert _document_matches("70458-2022", "ГОСТ Р 70458-2022")
    assert not _document_matches("70458-2022", "ГОСТ Р 70458-2023")
    assert _document_matches("8267", "ГОСТ 8267-93")
    assert _document_matches("396.1325800", "СП 396.1325800.2018")
