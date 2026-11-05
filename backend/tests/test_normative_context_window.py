from copy import deepcopy

from normative_agent import compact, MAX_CONTEXT
from normative_context_window import window
from normative_context_packing import serialize_context
from test_normative_context_packing import CAPTURES, size
from normative_embedded_context import literal_paths


def test_many_page_ocr_flags_are_lossless_and_leave_room_for_literal_sources():
    from normative_context_window import columnar_page_coverage
    pages = {str(page): dict(version='inline-raster-v1', detected=page % 4 + 1,
        candidates=page % 3, visually_transcribed=0, unresolved=page % 2,
        visual_review_required=True, absence_proven=False, future_flag='retained')
        for page in range(1, 100)}
    projected = columnar_page_coverage(pages)
    restored = {page: {**projected['shared'], **dict(zip(projected['columns'], values, strict=True))}
        for page, values in projected['pages'].items()}
    assert restored == pages
    assert len(serialize_context(projected)) < len(serialize_context(pages)) // 3
    case = deepcopy(CAPTURES[0])
    case['metadata']['work_scope_graph'] = dict(positions=[], same_work_other_scopes=[])
    case['metadata'].setdefault('coverage', {})['inline_raster'] = pages
    before = deepcopy(case)
    packed = compact(case['metadata'], case['sources'], case['preferred'],
        measure=lambda v: size(v, case['sources']))
    assert size(packed, case['sources']) <= MAX_CONTEXT
    assert packed['required_validation_claims'] == case['metadata']['required_validation_claims']
    assert case == before


def test_heterogeneous_unknown_page_flags_are_not_dropped():
    from normative_context_window import columnar_page_coverage
    pages = {'1': {'unresolved': 1}, '2': {'unresolved': 0, 'new_field': ['literal']}}
    assert columnar_page_coverage(pages) is pages


def test_small_window_is_lossless():
    value = dict(
        work_scope_graph={"positions": []},
        required_validation_claims=["quantity"],
        position={"work": "Work"},
    )
    assert window(value, [], limit=58000) == value


def test_followup_delivery_receipts_cannot_starve_completed_source_read():
    text='Literal condition with exceptions. '*65
    source=dict(id='read',kind='normative',origin='native',text=text,label='Source')
    actions=[dict(key=f'claim:{i}',action='read_registered_source',source_ids=['read'],
        read_requests=[dict(tool='read_source',source_id='read',offset=0)],
        reason='Verify the literal condition.',retry_fingerprint='h'*64,
        model_delivery=dict(source_states={f'source-{j}-'+'s'*60:'partial_text' for j in range(28)})) for i in range(8)]
    value=dict(work_scope_graph=dict(positions=[]),position=dict(work='Work'),
        required_validation_claims=['quantity','normative:applicability'],
        evidence_followup=dict(actions=actions),
        tool_results=[dict(request=dict(tool='read_source',source_id='read'),result=dict(source_id='read'))])
    original=deepcopy(value)
    # compact uses the production cap; window/pack_sources expose the same
    # boundary at a smaller limit so the regression stays small and readable.
    from normative_context_packing import pack_sources
    bounded=window(value,[source],limit=20000)
    packed=pack_sources(bounded,[source],preferred=['read'],limit=20000)
    assert packed['sources'][0]['text']==text and not packed['sources'][0]['excerpt_truncated']
    assert packed['required_validation_claims']==value['required_validation_claims']
    assert all(a['read_requests']==actions[i]['read_requests'] and a['source_ids']==['read']
        and a['reason']==actions[i]['reason'] for i,a in enumerate(packed['evidence_followup']['actions']))
    assert all('model_delivery' not in a for a in packed['evidence_followup']['actions'])
    assert value==original


def test_dense_real_capture_reserves_room_for_project_operands_without_losing_norms():
    case = deepcopy(CAPTURES[0])
    original = deepcopy(case)
    metadata = case["metadata"]
    metadata["work_scope_graph"] = dict(positions=[], same_work_other_scopes=[])
    metadata["extra_navigation"] = "n" * 6000
    packed = compact(
        metadata,
        case["sources"],
        case["preferred"],
        measure=lambda v: size(v, case["sources"]),
    )
    assert size(packed, case["sources"]) <= MAX_CONTEXT
    preview = {s["id"]: s for s in packed["sources"]}
    for sid in [
        "agent:p89:cell:feb8114d3c30a3c1318def97",
        "agent:p89:cell:b10b80c2f6781706ffd1f953",
        "agent:sum:ea3f8bc61fa62ef5f0b6f647",
    ]:
        assert preview[sid]["text"] == next(
            s["text"] for s in case["sources"] if s["id"] == sid
        )
    deferred = {s["source_id"] for s in packed["context_window"]["deferred_sources"]}
    full = set(literal_paths(packed, case["sources"]))
    assert {
        sid
        for sid in literal_paths(original["metadata"], case["sources"])
        if sid.startswith("methodology:")
    } <= full
    assert set(literal_paths(original["metadata"], case["sources"])) <= deferred | full
    assert case["sources"] == original["sources"]
    assert (
        packed["required_validation_claims"] == metadata["required_validation_claims"]
    )


def test_unknown_large_required_metadata_is_not_silently_dropped():
    value = dict(
        work_scope_graph={"positions": []},
        required_validation_claims=["quantity"],
        unknown_required="x" * 10000,
    )
    output = window(value, [], limit=1000)
    assert output["unknown_required"] == value["unknown_required"]
    assert len(serialize_context(output)) > 1000


def test_duplicate_table_navigation_is_bounded_without_dropping_selected_cells():
    value=dict(work_scope_graph=dict(positions=[]),required_validation_claims=['quantity'],
        quantity_candidates=[dict(operand_source_ids=['left','right'],table_id='selected')],
        coverage=dict(available_tables=[dict(table_id=f't{i}',columns=['Navigation '*40]) for i in range(80)]))
    output=window(value,[],limit=20000)
    assert len(output['coverage']['available_tables'])==8
    assert output['coverage']['table_catalog_deferred']['previously_shown']==80
    assert output['quantity_candidates']==value['quantity_candidates']
    assert output['required_validation_claims']==['quantity']
    assert len(value['coverage']['available_tables'])==80


def test_old_prose_is_not_allowed_to_crowd_out_current_source_evidence():
    items=[dict(claim_ids=['method'],state='confirmed',project_source_ids=['p'],
        normative_source_ids=['n'],explanation='Prior interpretation. '*150) for _ in range(8)]
    value=dict(work_scope_graph=dict(positions=[]),previous_review_items=items,
        required_validation_claims=['method'],quantity_candidates=[dict(source_id='p')])
    output=window(value,[],limit=10000)
    assert len(serialize_context(output))<10000
    assert len(output['previous_review_items'])==len(items)
    for row in output['previous_review_items']:
        assert row['claim_ids']==['method'] and row['state']=='confirmed'
        assert row['project_source_ids']==['p'] and row['normative_source_ids']==['n']
    assert 'explanation' in items[0]
