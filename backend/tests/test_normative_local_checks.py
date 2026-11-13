from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from normative_local_checks import area_columns, apply_local_checks, layer_group


def test_proved_normative_threshold_resolves_missing_proposal_but_not_an_actual_conflict():
    from normative_local_checks import is_layer_threshold_claim
    check=dict(claim_id='geometry:comment:thickness:1',state='not_found',reasons=['no_proposal'])
    spec=SimpleNamespace(expected_text='18 см')
    assert is_layer_threshold_claim(check,spec,'ГЭСН п. 1.27.18')
    assert not is_layer_threshold_claim(check,spec,'Проектная толщина')
    assert not is_layer_threshold_claim(check,SimpleNamespace(expected_text='12 см'),'1.27.18')
    check['state']='contradiction'
    assert not is_layer_threshold_claim(check,spec,'1.27.18')


def test_layer_threshold_applies_to_full_project_layer_not_each_pass():
    position = SimpleNamespace(work_name="Устройство нижнего слоя основания ЩПС h=12 см (двухслойного основания - слой1)", volume="431.61", unit="м2")
    related = [dict(work=position.work_name.replace('слой1', 'слой2'), quantity='431.61', unit='м2', row=20)]
    original = dict(summary='raw model', state='needs_facts', items=[dict(topic='Разбиение на технологические слои', state='confirmed', explanation='raw model', project_source_ids=[], normative_source_ids=['n1'])], sources=[dict(id='n1', kind='normative', label='ГЭСН 27 · 1.27.18', text='При толщине нижнего слоя основания или покрытия более 18 см его нормировать в два слоя.')], related_positions=related)
    saved = deepcopy(original)
    derived, changed = apply_local_checks(position, original, lambda _: b'{}')
    assert changed and original == saved
    assert '24 см' in derived['items'][0]['explanation']
    assert 'не к каждой' in derived['items'][0]['explanation']
    assert derived['items'][0]['state'] == 'confirmed'
    assert apply_local_checks(position, derived, lambda _: b'{}')[1] is False
    related[0]['quantity'] = '500'
    assert layer_group(position, related) is None


def _word(text, x, y, width=20, height=12, conf=96):
    return dict(text=text, left=x, top=y, width=width, height=height, conf=conf)


def _table(left='40', right='60'):
    words = [_word('Всего:', 0, 900)]
    for center, side, value in [(250, 'Справа', right), (650, 'Слева', left)]:
        words += [_word(side, center-10, 20), _word('0/31,5', center+15, 80, width=30),
            _word('h=0,24m', center-15, 110, width=30), _word('Площадь,', center-65, 145, width=30),
            _word('м', center-60, 175, width=10), _word('2', center-44, 165, width=7),
            _word(value, center-60, 900, width=25), _word('9999', center+50, 900)]
    return dict(words=words, width=1000, height=1000)


def test_area_numbers_are_chosen_by_columns_and_total_not_expected_quantity():
    data = _table()
    groups = area_columns(data, '0/31,5', Decimal('.24'))
    assert groups and sum(g['value'] for g in groups) == 100
    data = _table(left='50')
    groups = area_columns(data, '0/31,5', Decimal('.24'))
    assert groups and sum(g['value'] for g in groups) == 110
    assert area_columns(data, '0/63', Decimal('.24')) is None
    assert area_columns(data, '0/31,5', Decimal('.22')) is None


def test_wrong_unit_or_low_confidence_number_is_not_accepted():
    data = _table()
    next(w for w in data['words'] if w['text'] == '2')['text'] = '3'
    assert area_columns(data, '0/31,5', Decimal('.24')) is None
    data = _table()
    next(w for w in data['words'] if w['text'] == '60')['conf'] = 40
    assert area_columns(data, '0/31,5', Decimal('.24')) is None


def test_complete_layer_basis_needs_native_construction_both_area_columns_and_norms():
    import json
    p = SimpleNamespace(work_name='Устройство нижнего слоя основания ЩПС h=12 см (двухслойного основания - слой1)', volume='100.00', unit='м2')
    related = [dict(work=p.work_name.replace('слой1', 'слой2'), quantity=p.volume, unit='м2', row=20)]
    result = dict(items=[], sources=[
        dict(id='n1', kind='normative', label='ГЭСН 27 · 1.27.18', text='При толщине нижнего слоя основания более 18 см его нормировать в два слоя.'),
        dict(id='n2', kind='normative', label='ГЭСН 27 · 27-04-003-06', text='двухслойных нижний слой толщиной 15 см. Измеритель: 1000 м2'),
        dict(id='n3', kind='normative', label='ГЭСН 27 · 27-04-003-08', text='На каждый 1 см изменения толщины'),
        dict(id='p1', kind='project', origin='native', text='Нижний слой основания ЩПС фр. 0/31,5 h=24 см'),
        dict(id='p2', kind='project', origin='ocr_unverified', text='', page=89, words_key='ocr')], related_positions=related)
    def run(data):
        return apply_local_checks(p, result, lambda _: json.dumps(data).encode())[0]
    data = _table()
    for word in data['words']:
        if word['text'].startswith('h='):
            word['conf'] = 76  # Corroborated twice and by native construction.
    proof = run(data)
    assert proof['local_work_basis']['quantity'] == '100.00'
    assert {'quantity', 'unit', 'normative:applicability'} <= set(proof['items'][0]['claim_ids'])
    anchors = proof['local_work_basis']['anchors']
    assert anchors['quantity'][0]['source_id'] == 'p2'
    cells = anchors['quantity'][0]['rects']
    assert [(.19, .9, .215, .912), (.59, .9, .615, .912)] == [tuple(r) for r in cells if r[1] == .9 and r[0] > .1]
    assert all(0 <= v <= 1 for rect in cells for v in rect)
    assert anchors['thickness'][0]['quote'].endswith('h=24 см')
    assert 'нормировать в два слоя' in anchors['thickness'][-1]['quote']
    old = deepcopy(proof)
    old['local_validation_version'] = 2
    upgraded = apply_local_checks(p, old, lambda _: json.dumps(data).encode())[0]
    assert len([v for v in upgraded['items'] if v['topic'] == 'Основание работы подтверждено']) == 1
    broken = deepcopy(old)
    broken['sources'] = [s for s in broken['sources'] if s['id'] != 'p1']
    assert 'local_work_basis' not in apply_local_checks(p, broken, lambda _: json.dumps(data).encode())[0]
    assert 'local_work_basis' not in run(_table(left='50'))
    result['sources'] = [s for s in result['sources'] if s['id'] != 'n3']
    assert 'local_work_basis' not in run(data)
    result['sources'] = [s for s in result['sources'] if s['id'] != 'p1']
    assert 'local_work_basis' not in run(data)
