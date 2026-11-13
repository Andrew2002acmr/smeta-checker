"""HTTP contracts expose stable sources while stored proof metadata is retained."""

from copy import deepcopy

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from normative_api import register_normative_routes
from normative_public_sources import public_sources
from workspace_models import NormativeCheckSource, NormativeReviewDTO, NormativeStateDTO
from test_normative_source_roles import sources


def review():
    ss = sources()
    ss.append(dict(id='methodology:571:5',kind='normative',label='Приказ 571/пр',page=3,
        text='Reviewed methodology clause.',origin='reviewed_literal_transcription',source_id='pdf-methodology',
        citation_pages=[3],clause_id='571:5',source_sha256='a'*64,kb_identity={'release_id':'pinned'}))
    return dict(id='review',kind='check',actor='local-test',created_at='2026-11-01T00:00:00Z',payload=dict(
        work_item_id='fixture',query='fixture',kind='ALL',facts={},fact_origins={},kb={},
        vor_version_id='vor',position_key='position',status='needs_facts',candidates=[],
        missing_facts=[],checks=[],project_citations=[],position_check=dict(summary='',state='needs_facts',
            items=[],sources=ss,related_positions=[],telemetry={'model':'offline','provider':'local'})))


def test_projection_retains_public_fields_and_never_mutates_stored_private_metadata():
    saved = review()
    original = deepcopy(saved)
    with pytest.raises(ValidationError):
        NormativeCheckSource.model_validate(saved['payload']['position_check']['sources'][1])
    projected = public_sources(saved)
    NormativeReviewDTO.model_validate(projected)
    assert saved == original and projected != saved
    assert public_sources(projected) == projected
    raw = saved['payload']['position_check']['sources'][1]
    public = projected['payload']['position_check']['sources'][1]
    assert raw['classification_claim_ids'] and raw['evidence_role']
    assert public['source_id'] == raw['source_id'] and public['text'] == raw['text']
    assert set(public) <= set(NormativeCheckSource.model_fields)
    public['text'] = 'changed public view'
    assert saved == original


@pytest.mark.parametrize('endpoint', ['state', 'search', 'selection'])
def test_real_response_model_routes_accept_private_source_metadata_without_storage_changes(monkeypatch, endpoint):
    stored = review()
    stored['payload']['position_check'].update(pipeline_version=5, decision_origin='model_with_validated_sources')
    before = deepcopy(stored)
    state = dict(work_name='Work',facts={},fact_origins={},project_citations=[],latest_check=stored)
    monkeypatch.setattr('normative_service.state', lambda *_: state)
    monkeypatch.setattr('normative_service.search', lambda *_: stored)
    monkeypatch.setattr('normative_service.select', lambda *_: stored)
    app = FastAPI()
    router = APIRouter()
    register_normative_routes(router, lambda: object(), lambda *_: {})
    app.include_router(router)
    base = '/api/v1/audits/run/positions/0/normative'
    with TestClient(app) as client:
        if endpoint == 'state':
            response = client.get(base)
            assert response.status_code == 200, response.text
            NormativeStateDTO.model_validate(response.json())
            output = response.json()['latest_check']
        else:
            tail, payload = {'search':('/search',{'query':'test query'}),
                'selection':('/selection',{'search_id':'source','norm_ids':[]})}[endpoint]
            response = client.post(base + tail, json=payload)
            assert response.status_code == 200, response.text
            output = response.json()
            NormativeReviewDTO.model_validate(output)
    assert stored == before
    assert output['payload']['position_check']['decision_origin'] == 'model_with_validated_sources'
    for source in output['payload']['position_check']['sources']:
        assert 'classification_claim_ids' not in source and 'source_sha256' not in source


def test_projection_does_not_hide_invalid_non_source_contract_fields():
    saved = review()
    saved['payload']['position_check']['invented_field'] = 'wrong'
    with pytest.raises(ValidationError):
        NormativeReviewDTO.model_validate(public_sources(saved))


def test_actual_allocation_cards_use_existing_public_calculation_contract_losslessly():
    from normative_parameters import enrich
    from test_planned_allocation_integration import prepared
    positions, raw, relation = prepared()
    checked = enrich(positions[1], raw, positions, relation)
    saved = review()
    saved['payload']['position_check'].update(parameter_cards=checked['parameter_cards'],
        evidence_trace=checked['evidence_trace'])
    before = deepcopy(saved)
    projected = public_sources(saved)
    parsed = NormativeReviewDTO.model_validate(projected)
    assert saved == before
    assert projected['payload']['position_check']['evidence_trace'] == checked['evidence_trace']
    cards = parsed.payload.position_check.parameter_cards
    allocated = next(c for c in cards if c.label == 'Заявленный объём распределения')
    assert allocated.role == 'calculation' and 'не измеренный' in allocated.reason
    assert next(c for c in cards if c.id == 'quantity').found_value == '133.3584'
    broken = deepcopy(projected)
    broken['payload']['position_check']['parameter_cards'][0]['invented_field'] = True
    with pytest.raises(ValidationError):
        NormativeReviewDTO.model_validate(public_sources(broken))
