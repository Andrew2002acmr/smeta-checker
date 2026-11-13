from normative_evidence_plan import build_work_plan
from normative_source_registry import source_fingerprint
from normative_work_method import method_candidates


def _norm(sid, code, row, *, origin='native'):
    return dict(id=sid, kind='normative', source_id='document', page=1,
                label=f'ГЭСН 27 ФСНБ-2022 · {code}', origin=origin, text=row)


def test_method_candidate_matches_exact_variant_row_not_shared_header():
    code = '27-06-031-02'
    title = 'Устройство покрытия асфальтоукладчиками третьего и четвертого типоразмеров.'
    wrong = _norm('wrong', code, f'{title}\n{code} четвертого типоразмера')
    exact = _norm('exact', '27-06-031-01', f'{title}\n27-06-031-01 третьего типоразмера')
    unread = _norm('ocr', '27-06-031-01', exact['text'], origin='ocr_uncertain')
    assert method_candidates('Укладка а/укладчиком 3 типоразмера', 'method', [wrong, exact, unread]) == [
        dict(source_id='exact', role='norm_definition_or_composition')]


def test_method_plan_can_reread_existing_norm_without_confirming_compatibility():
    claim = 'method:comment:equipment:1'
    exact = _norm('exact', '27-06-031-01',
        '27-06-031-01 Устройство покрытия асфальтоукладчиком третьего типоразмера.')
    review = dict(items=[], sources=[exact], parameter_cards=[], coverage=dict(model_context_packing=[
        dict(response_state='completed', delivery_contract_version=1,
             source_fingerprints={'exact': source_fingerprint(exact)}, supplied_source_ids=[]),
    ]))
    position = dict(work_name='Устройство покрытия', position_type='Работа',
                    comment='Укладка а/укладчиком VOGELE SUPER 1900 3 типоразмера')
    matching = dict(status='Требуется ручная проверка',
                    checks=[dict(claim_id=claim, required=True, state='not_found')])

    action = next(a for a in build_work_plan(position, review, matching)['actions']
                  if a['key'] == 'claim:' + claim)
    assert action['family'] == 'work_method'
    assert action['verification']['required']
    assert action['verification']['automatic_confirmation'] is False
    assert action['method_candidate_routes'] == [
        dict(source_id='exact', role='norm_definition_or_composition')]
    assert action['read_requests'] == [dict(tool='read_source', source_id='exact', offset=0)]
    assert review['items'] == []
