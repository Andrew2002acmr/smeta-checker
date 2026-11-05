"""Drawings keep physical provenance and cannot silently confirm work items."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

from audit_core import PageTextIndex
from pdf_drawing_readings import apply, identity, save, validate

VERSION = str(UUID(int=1))


def fixture():
    index = PageTextIndex(['original OCR'], {0: 'table timeout'}, locators=[[]],
        metadata={'document_version_id': VERSION, 'pdf_sha256': 'b' * 64})
    reading = dict(version_id=VERSION, pdf_sha256='a' * 64, page=1,
        extraction_sha256='b' * 64, render_sha256='c' * 64,
        unrotated_page_size=[100, 200], reader='visual reader', method='original raster',
        facts=[dict(id='axis', text='Отметка оси 124,88 м', bbox=[10, 20, 30, 40])],
        limitations=['Not a complete geometry model'])
    ident = identity(str(UUID(int=2)), VERSION, 'a' * 64, 1)
    record = dict(project_id=ident['project_id'], config={'pdf_versions': {'plan': VERSION}})
    artifacts = []

    def store(project, key, payload):
        artifacts.append(dict(identity=key, payload=deepcopy(payload)))
        return payload

    repo = SimpleNamespace(supports_analysis_replay=True,
        load_version=lambda _: SimpleNamespace(sha256='a' * 64),
        save_analysis_artifact=store,
        drawing_reading_artifacts=lambda *_: artifacts)
    return SimpleNamespace(repository=repo), record, index, reading, ident


def test_saved_reading_is_searchable_idempotent_and_does_not_clear_ocr_error():
    context, record, index, reading, _ = fixture()
    save(context, record, VERSION, reading, index)
    enriched = apply(context, record, VERSION, index)
    assert enriched is not index and index.texts == ('original OCR',)
    assert '124,88' in enriched.texts[0]
    assert enriched.errors == index.errors
    assert enriched.locators[0][0]['bbox'] == [10, 20, 30, 40]
    assert enriched.locators[0][0]['origin'] == 'visual_transcription'
    from audit_models import SourceLocator
    assert SourceLocator.model_validate(enriched.locators[0][0]).page == 1
    assert apply(context, record, VERSION, enriched) is enriched
    assert 'verdict' not in enriched.metadata


@pytest.mark.parametrize('field,value', [
    ('page', 2), ('version_id', 'another'), ('pdf_sha256', 'd' * 64),
    ('extraction_sha256', 'd' * 64), ('reader', ''), ('render_sha256', ''),
    ('facts', [dict(id='bad', text='1', bbox=[-1, 0, 1, 1])]),
    ('facts', [dict(id='bad', text='1', bbox=[0, 0, float('nan'), 1])]),
    ('facts', [dict(id='bad', text='', bbox=[0, 0, 1, 1])]),
])
def test_wrong_source_or_geometry_rejected(field, value):
    _, _, index, reading, ident = fixture()
    reading[field] = value
    with pytest.raises(ValueError):
        validate(reading, ident, index)


def test_unpinned_document_cannot_receive_readings():
    context, record, index, reading, _ = fixture()
    record['config']['pdf_versions'] = {}
    with pytest.raises(ValueError):
        save(context, record, VERSION, reading, index)
    assert apply(context, record, VERSION, index) is index


def test_stale_corpus_cannot_receive_saved_readings():
    context, record, index, reading, _ = fixture()
    save(context, record, VERSION, reading, index)
    index.metadata['pdf_sha256'] = 'd' * 64
    with pytest.raises(ValueError):
        apply(context, record, VERSION, index)
