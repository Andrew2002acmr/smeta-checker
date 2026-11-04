from audit_core import build_claim_specs
from audit_models import ClaimKind, ClaimSpec, VorPosition
from normative_claim_definitions import definition
from normative_verification_contract import contract, plan


def test_four_source_responsibilities_do_not_make_refinements_optional():
    assert contract('quantity')['family'] == 'project_requirement'
    assert contract('normative:applicability')['family'] == 'normative_condition'
    assert contract('derived:rate')['family'] == 'calculation_basis'
    refinement = contract('attribute:name:designation:1')
    assert refinement['family'] == 'vor_refinement'
    assert refinement['relation'] == 'satisfies_project_constraints'
    assert refinement['required'] and not refinement['automatic_confirmation']
    assert not refinement['project_literal_required']
    assert not refinement['missing_project_literal_is_mismatch']


def test_semantic_norm_instruction_overrides_legacy_consumption_handle():
    spec = ClaimSpec(claim_id='basis:comment:consumption:1', kind=ClaimKind.BASIS,
        property_label='normative_instruction', expected_text='Нормировать в два слоя')
    assert definition(spec)['verification']['family'] == 'normative_condition'
    assert definition(spec)['evidence_task']['kind'] == 'normative_condition'


def test_work_plan_preserves_all_claims_and_exact_expected_properties():
    position = VorPosition(index=0, work_name='Щебеночно-песчаная смесь М800',
        volume='4', unit='м3', position_type='Материал', comment='Коэффициент расхода - 1,26')
    specs = build_claim_specs(position)
    ids = {s.claim_id for s in specs} | {'derived:parent', 'derived:rate', 'normative:applicability'}
    tasks = plan(position, ids)
    assert {t['claim_id'] for t in tasks} == ids
    mark = next(t for t in tasks if ':designation:' in t['claim_id'])
    assert mark['expected_text'] == 'М800'
    assert mark['verification']['family'] == 'vor_refinement'
    assert mark['verification']['required']


def test_unreviewed_image_evidence_cannot_confirm_or_publish_mismatch():
    from normative_position_check import CheckedItem, validate_items

    for state in ('confirmed', 'possible_mismatch'):
        item = CheckedItem(topic='Материал', state=state, explanation='Совместимость',
            claim_ids=['attribute:name:designation:1'], project_source_ids=['p'], normative_source_ids=['n'])
        for source in ('p', 'n'):
            sources = [dict(id='p', kind='project'), dict(id='n', kind='normative')]
            sources[0 if source == 'p' else 1]['origin'] = (
                'ocr_inline_unreviewed' if source == 'p' else 'ocr_unreviewed_inline_raster')
            assert validate_items([item], sources)[0]['state'] == 'needs_facts'


def test_requested_normative_symbol_survives_context_packing(monkeypatch):
    from types import SimpleNamespace
    from normative_agent import EvidenceRequest, compact
    from test_normative_agent import toolkit

    tools = toolkit()
    tools.facts = {}
    tools.kb = SimpleNamespace(search=lambda *args, **kwargs: [dict(id='norm')])
    text = 'Scope ' * 300 + 'Марка В₁; Пл₁.'
    candidate = dict(id='norm', text=text, citations=[dict(page=7, source_id='pdf')],
        designation='Test standard', code='5.8', extraction_status='native_with_visual_transcriptions', resources=[])
    monkeypatch.setattr('normative_service.candidate', lambda *args: candidate)
    result = tools.execute(EvidenceRequest(tool='search_norms', query='ГОСТ 1234-2022 В1'))
    sid = result['results'][0]['source_id']
    packed = compact({}, tools.sources, tools.preferred)
    received = next(s for s in packed['sources'] if s['id'] == sid)
    assert 'В₁' in received['text'] and 'Пл₁' in received['text']


def test_followup_retains_typed_obligation_from_v2_trace():
    from normative_evidence_followup import plan_for
    task = dict(key='claim:attribute:name:designation:1', action='retrieve_normative_or_project_basis',
        retry_fingerprint='same-input', verification=contract('attribute:name:designation:1'))
    payload = dict(position_check=dict(pipeline_version=9, execution_state='complete', state='needs_facts',
        evidence_trace=[dict(kind='evidence_work_plan_v2', actions=[task])], coverage={}))
    assert plan_for(payload)['actions'][0]['verification']['relation'] == 'satisfies_project_constraints'
