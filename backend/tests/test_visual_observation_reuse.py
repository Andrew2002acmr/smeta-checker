"""Reviewed physical readings survive unrelated OCR changes, never relocation."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import UUID

import pytest

from normative_table_headers import apply, save, verified_observation_crop
from test_visual_header_transcription import fixture


def sample():
    context, record, table, header, artifacts = fixture()
    version = str(UUID(int=123))
    record['config']['pdf_versions'] = {'p.pdf': version}
    raw = b'original verified crop'
    sha = hashlib.sha256(raw).hexdigest()
    header['visual_transcription'].update(document_version_id=version, crop_image_sha256=sha)
    header['header_refinement']['image_sha256'] = sha
    table['cells'][0]['header_refinement']['image_sha256'] = sha
    save(context, record, version, 89, table, [header], provenance='independent_visual_header_transcription')
    context.repository.visual_header_artifacts = lambda *_: [dict(**v, key=k) for k, v in artifacts.items()]
    context.repository.load_version = lambda _: SimpleNamespace(sha256='a' * 64)
    context.blob_store = SimpleNamespace(get=lambda _: raw)
    current = deepcopy(table)
    current['cells'][0].pop('header_refinement')
    current['cells'][0]['quality'] = 'uncertain'
    current['cells'][1]['text'] = '999'  # New OCR/body must remain authoritative.
    return context, record, version, current, artifacts


def test_same_source_header_can_be_reused_without_reusing_any_verdict():
    context, record, version, table, _ = sample()
    before = deepcopy(table)
    events = []
    result = apply(context, record, version, 89, [table], events)[0]
    assert result['cells'][0]['text'] == 'Основание — ЩПС 0/31,5, м² (20см)'
    assert result['cells'][0]['quality'] == 'uncertain'
    assert result['cells'][1]['text'] == '999'
    assert table == before
    assert events[0]['kind'] == 'visual_header_observation_reused_v1'
    assert events[0]['model_verdict_reused'] is False


@pytest.mark.parametrize('change', ['pdf', 'raw', 'bbox', 'role', 'crop', 'new_crop', 'proof_page'])
def test_changed_physical_or_raw_source_does_not_reuse_old_reading(change):
    context, record, version, table, artifacts = sample()
    if change == 'pdf':
        context.repository.load_version = lambda _: SimpleNamespace(sha256='d' * 64)
    elif change == 'raw':
        table['cells'][0]['text'] = 'Different OCR'
    elif change == 'bbox':
        table['cells'][0]['bbox'] = [0, 0, 11, 5]
    elif change == 'role':
        table['cells'][0]['role'] = 'cell'
    elif change == 'crop':
        context.blob_store.get = lambda _: b'corrupted image'
    elif change == 'new_crop':
        table['cells'][0]['header_refinement'] = dict(image_sha256='d' * 64)
    else:
        next(iter(artifacts.values()))['payload']['headers'][0]['visual_transcription']['page'] = 90
    before = deepcopy(table)
    assert apply(context, record, version, 89, [table], []) == [before]
    assert table == before


def test_missing_crop_is_reproduced_from_exact_pdf_without_ocr(tmp_path, monkeypatch):
    import pymupdf
    import ocr_engine
    from storage_blobs import FileBlobStore
    monkeypatch.setattr(ocr_engine, 'run_tsv', lambda *a, **kw: pytest.fail('OCR must not run'))
    blobs = FileBlobStore(tmp_path)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=120, height=100)
        page.insert_text((10, 20), 'Physical header')
        raw = pdf.tobytes()
    original = blobs.put(raw)
    document = SimpleNamespace(sha256=original.sha256, blob_key=original.key)
    box = [0, 0, 100, 30]
    with pymupdf.open(stream=raw, filetype='pdf') as pdf:
        pix = pdf[0].get_pixmap(dpi=600, clip=pymupdf.Rect(box), alpha=False)
        crop = ocr_engine._to_png(ocr_engine._pil_from_pixmap(pix))
    sha = hashlib.sha256(crop).hexdigest()
    refinement = dict(dpi=600, rotation=0, bbox=box, image_sha256=sha)
    assert verified_observation_crop(blobs, document, 1, refinement)
    assert blobs.get(f'{sha[:2]}/{sha[2:4]}/{sha}') == crop
    assert blobs.get(original.key) == raw
