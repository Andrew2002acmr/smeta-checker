"""Integrated tool entry points, without a provider or fake model responses."""

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pymupdf as fitz
import pytest

from normative_agent import EvidenceRequest, EvidenceTools
from storage_blobs import FileBlobStore


def test_source_pagination_preserves_original_and_has_terminal_cursor():
    tools = EvidenceTools.__new__(EvidenceTools)
    text = 'literal source ' * 1000
    original = dict(id='source', text=text, kind='project', origin='native')
    tools._reset([original], None, {})
    first = tools.execute(EvidenceRequest(tool='read_source', source_id='source'))
    assert first['next_offset'] == 3500
    offset = first['next_offset']
    pieces = [text[:offset]]
    while offset is not None:
        part = tools.execute(EvidenceRequest(tool='read_source', source_id='source', offset=offset))
        pieces.append(part['text'])
        offset = part['next_offset']
    assert ''.join(pieces) == text
    assert original['text'] == text
    assert tools.execute(EvidenceRequest(tool='read_source', source_id='source', offset=len(text)+1))['error'] == 'source_offset_out_of_range'


@pytest.fixture
def drawing(tmp_path):
    version = str(uuid4())
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=200)
        page.insert_text((20, 40), 'Drawing A')
        raw = pdf.tobytes()
    blobs = FileBlobStore(tmp_path)
    ref = blobs.put(raw)
    document = SimpleNamespace(blob_key=ref.key, sha256=hashlib.sha256(raw).hexdigest(), filename='drawing.pdf')
    tools = EvidenceTools.__new__(EvidenceTools)
    tools._reset([], None, {})
    tools.version = version
    tools.context = SimpleNamespace(blob_store=blobs, repository=SimpleNamespace(load_version=lambda _: document))
    tools.record = dict(config=dict(pdf_versions={'drawing.pdf': version}))
    return tools, document


def test_real_local_crop_is_addressed_but_never_a_verified_reading(drawing):
    tools, document = drawing
    result = tools.execute(EvidenceRequest(tool='read_page', page=1, bbox=[0, 0, 150, 100], scale=2))
    assert (result['width'], result['height']) == (300, 200)
    assert result['pdf_sha256'] == document.sha256
    assert result['interpretation_state'] == 'not_performed'
    assert result['automatic_confirmation_allowed'] is False
    assert hashlib.sha256(tools.context.blob_store.get(result['image_key'])).hexdigest() == result['image_sha256']
    assert tools.sources[0]['numeric_verified'] is False


@pytest.mark.parametrize('fields, error', [
    ({'bbox': [0, 0, 400, 100]}, 'drawing_region_out_of_bounds'),
    ({'bbox': [100, 0, 0, 100]}, 'invalid_drawing_region'),
    ({'bbox': [0, 0, 100, 100], 'page': 2}, 'page_out_of_range'),
    ({'bbox': [0, 0, 100, 100], 'document_version_id': str(uuid4())}, 'drawing_requires_pinned_document'),
])
def test_drawing_invalid_requests_do_not_create_evidence(drawing, fields, error):
    tools, _ = drawing
    assert tools.execute(EvidenceRequest(tool='read_page', **fields))['error'] == error
    assert not tools.sources


def test_graph_entity_unknown_does_not_raise_technical_failure():
    tools = EvidenceTools.__new__(EvidenceTools)
    tools._reset([], None, {})
    tools.engineering_graph = dict(entities=[], relations=[])
    assert tools.execute(EvidenceRequest(tool='read_source', entity_id='unknown'))['error'] == 'unknown_engineering_entity'
