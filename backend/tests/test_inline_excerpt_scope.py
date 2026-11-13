"""A remote unresolved image must not poison an independently readable quote."""
from types import SimpleNamespace

from pdf_inline_evidence import excerpt_origin, native_fragments


def index(context='Масса кабеля', state='unresolved'):
    return SimpleNamespace(texts=['Толщина покрытия h=5 см.\n' + 'text ' * 800 + '\nМасса кабеля'],
        quality={0: dict(origin='native', inline_raster=dict(detected=1))},
        locators=[[dict(role='inline_image', context=context, state=state)]])


def test_distant_image_does_not_change_native_paragraph_origin():
    source = index()
    assert excerpt_origin(source, 1, 0, 100) == 'native'
    assert excerpt_origin(source, 1, len(source.texts[0]) - 20, 20) == 'ocr_inline_unreviewed'


def test_missing_or_unlocatable_inline_provenance_stays_uncertain():
    assert excerpt_origin(index(context='not locatable'), 1, 0, 100) == 'ocr_inline_unreviewed'
    source = index()
    source.locators = None
    assert excerpt_origin(source, 1, 0, 100) == 'ocr_inline_unreviewed'


def test_reviewed_inline_region_does_not_claim_unverified_ocr():
    source = index(state='visually_transcribed')
    assert excerpt_origin(source, 1, 0, len(source.texts[0])) == 'native'


def test_whitespace_and_excerpt_boundary_still_detect_affected_text():
    source = index(context='Масса\nкабеля')
    assert excerpt_origin(source, 1, len(source.texts[0]) - 5, 5) == 'ocr_inline_unreviewed'


def test_native_fragment_rule_cannot_certify_a_whole_page_scan():
    source = index()
    source.quality[0]['origin'] = 'ocr'
    assert excerpt_origin(source, 1, 0, 100) == 'ocr_unverified'
    assert native_fragments(source, 1, 0, 100) == []


def test_wide_uncertain_excerpt_keeps_readable_native_spans_without_filling_a_gap():
    source = index()
    source.texts = ['Нижний слой основания: h=24 см. ' * 10 + '\nМасса кабеля\n' + 'Другой читаемый текст. ' * 10]
    before = source.texts[0]
    assert excerpt_origin(source, 1, 0, len(before)) == 'ocr_inline_unreviewed'
    spans = native_fragments(source, 1, 0, len(before))
    assert len(spans) == 2
    for span in spans:
        assert span['text'] == before[span['offset']:span['offset'] + len(span['text'])]
        assert 'Масса кабеля' not in span['text']
        assert excerpt_origin(source, 1, span['offset'], len(span['text'])) == 'native'
    assert source.texts[0] == before


def test_unlocatable_insert_cannot_create_native_fragments():
    source = index(context='missing source context')
    assert native_fragments(source, 1, 0, len(source.texts[0])) == []
