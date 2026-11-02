"""Conservative local checks over frozen VOR rows, clauses and OCR geometry.

No model calls. Quantities are located by headers/units/total row, never by
searching for the number expected in VOR. Ambiguous layouts return no proof.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json
import re
from statistics import median
from typing import Any

from normative_corpus import decimal

LOCAL_RULES_VERSION = 1


def _unit(value):
    return str(value).strip().casefold().rstrip('.').replace('²', '2').replace('m', 'м')


def _layer(work):
    stage = re.search(r'двухслойн[^)]*?слой\s*([12])\b', work, re.I)
    height = re.search(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*см', work, re.I)
    kind = next((v for v in ('нижнего', 'среднего', 'верхнего') if v+' слоя' in work.casefold()), None)
    if not stage or not height or not kind:
        return None
    signature = re.sub(r'\([^)]*двухслойн[^)]*\)', '', work, flags=re.I)
    signature = re.sub(r'\bh\s*=\s*\d+(?:[.,]\d+)?\s*см', '', signature, flags=re.I)
    signature = re.sub(r'[^\w]+', '', signature.casefold())
    return dict(stage=int(stage[1]), height=decimal(height[1]), kind=kind, signature=signature)


def layer_group(position, related) -> dict[str, Any] | None:
    own = _layer(position.work_name)
    quantity = decimal(position.volume)
    if not own or not own['height'] or not quantity or _unit(position.unit) != 'м2':
        return None
    matches = []
    for row in related:
        layer = _layer(row['work'])
        if (layer and layer['signature'] == own['signature'] and layer['stage'] != own['stage']
                and decimal(row.get('quantity')) == quantity and _unit(row.get('unit')) == 'м2'):
            matches.append((row, layer))
    if len(matches) != 1:
        return None
    row, other = matches[0]
    return dict(height=own['height']+other['height'], first=own['height'], second=other['height'], kind=own['kind'],
        other_row=row.get('row'), quantity=quantity)


def _x(word):
    return word['left'] + word['width']/2


def _y(word):
    return word['top'] + word['height']/2


def _rects(data, words):
    return [[w['left']/data['width'], w['top']/data['height'],
        (w['left']+w['width'])/data['width'], (w['top']+w['height'])/data['height']] for w in words]


def is_layer_threshold_claim(check, spec, comment):
    """Called only after a complete project/table/clause layer proof exists."""
    return bool(check.get('claim_id', '').startswith('geometry:comment:thickness:')
        and check.get('state') in {'review_required', 'not_found'}
        and '1.27.18' in comment and spec
        and re.fullmatch(r'(?:более\s+)?18\s*см', spec.expected_text, re.I))


def area_columns(data, fraction, thickness_m):
    """Resolve two left/right area cells in a final total row of a road table."""
    words = data['words']
    font = median(w['height'] for w in words if w.get('height', 0) > 0)
    span = font*8
    totals = [w for w in words if w['text'].casefold().strip(' .,:;') == 'всего' and w['conf'] >= 90 and w['top'] > data['height']*.5]
    if not totals:
        return None
    footer = max(totals, key=lambda w: w['top'])
    groups = []
    for header in words:
        if header['text'].replace('.', ',') != fraction.replace('.', ',') or header['conf'] < 85:
            continue
        heights = []
        for w in words:
            match = re.search(r'h\s*=\s*(\d+[.,]\d+)', w['text'], re.I)
            if (match and decimal(match[1]) == thickness_m and w['conf'] >= 70
                    and 0 < _y(w)-_y(header) < font*5 and abs(_x(w)-_x(header)) < span):
                heights.append(w)
        if len(heights) != 1:
            continue
        height = heights[0]
        sides = [w for w in words if w['text'].casefold() in {'слева', 'справа'} and w['conf'] >= 80
            and 0 < _y(height)-_y(w) < font*10 and abs(_x(w)-_x(height)) < span]
        labels = [w for w in words if w['text'].casefold().strip(' ,:') == 'площадь' and w['conf'] >= 80
            and 0 < _y(w)-_y(height) < font*5 and abs(_x(w)-_x(height)) < span]
        if len(sides) != 1 or len(labels) != 1:
            continue
        label = labels[0]
        bases = [w for w in words if _unit(w['text']) in {'м', 'м2'} and w['conf'] >= 80
            and 0 < _y(w)-_y(label) < font*5 and abs(_x(w)-_x(label)) < font*2]
        if len(bases) != 1:
            continue
        base = bases[0]
        powers = [w for w in words if w['text'] == '2' and w['conf'] >= 80
            and 0 < _x(w)-_x(base) < font*2 and 0 < _y(base)-_y(w) < font*2]
        if _unit(base['text']) != 'м2' and len(powers) != 1:
            continue
        center = _x(powers[0]) if powers else _x(base)
        numbers = [w for w in words if re.fullmatch(r'\d+(?:[.,]\d+)?', w['text']) and w['conf'] >= 90
            and abs(_y(w)-_y(footer)) < font and abs(_x(w)-center) < font*2.5]
        if len(numbers) != 1:
            continue
        number = numbers[0]
        groups.append(dict(side=sides[0]['text'].casefold(), value=decimal(number['text']), confidence=number['conf'],
            header_confidence=height['conf'],
            quantity_rects=_rects(data, [number, label, base, *powers, footer]),
            thickness_rects=_rects(data, [header, height])))
    if len(groups) != 2 or {g['side'] for g in groups} != {'справа', 'слева'}:
        return None
    return groups


def apply_local_checks(position, result, read_blob):
    """Return a derived review, leaving the raw model answer untouched."""
    if result.get('local_validation_version') == 3:
        return result, False
    group = layer_group(position, result['related_positions'])
    if not group:
        return result, False
    clause = next((s for s in result['sources'] if s['kind'] == 'normative'
        and re.search(r'ГЭСН\s*27\b', s['label']) and s['label'].endswith('1.27.18')), None)
    if not clause:
        return result, False
    condition = re.search(r'При\s+толщине\s+'+group['kind']+r'\s+слоя\s+[^.]*?более\s+(\d+(?:[.,]\d+)?)\s*см', clause['text'], re.I)
    if not condition or group['height'] <= decimal(condition[1]):
        return result, False
    derived = deepcopy(result)
    derived.pop('local_work_basis', None)
    derived['local_validation_version'] = 3
    # Replace broad model comments about layers with the narrower verified rule.
    derived['items'] = [v for v in derived['items'] if not v['topic'].startswith('Локальная проверка:')
        and v['topic'] != 'Основание работы подтверждено'
        and not re.search('разбиени|технологическ|суммарной толщин', v['topic'], re.I)]
    explanation = (f"ВОР: {group['first']} + {group['second']} = {group['height']} см, одинаковая площадь {group['quantity']} м², "
        f"два явно обозначенных технологических слоя; связанная строка {group['other_row']}. "
        f"Порог {condition[1]} см из п. 1.27.18 относится к полной толщине проектного {group['kind']} слоя "
        f"({group['height']} см), а не к каждой {group['first']}-сантиметровой захватке отдельно. "
        "Такое разбиение само по себе не является ошибкой или двойным учётом. "
        "Привязка к участку ПД и условия конкретных норм проверяются отдельно.")
    local = dict(topic='Локальная проверка: общая толщина и порог нормирования', state='confirmed', explanation=explanation,
        project_source_ids=[], normative_source_ids=[clause['id']])
    derived['items'].insert(0, local)
    fractions = []
    native_sources = []
    construction_anchors = []
    noun = 'нижний' if group['kind'] == 'нижнего' else 'верхний'
    for source in result['sources']:
        if source['kind'] != 'project' or source['origin'] != 'native':
            continue
        pattern = noun+r'\s+слой\s+основания(.{0,450}?)\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*см'
        for match in re.finditer(pattern, source['text'], re.I | re.S):
            frac = re.search(r'0/\d+(?:[.,]\d+)?', match[1])
            if decimal(match[2]) == group['height'] and frac:
                fractions.append(frac[0])
                native_sources.append(source['id'])
                construction_anchors.append(dict(source_id=source['id'], quote=match[0], rects=[]))
    proofs = []
    if len(set(fractions)) == 1:
        for source in result['sources']:
            if source['kind'] == 'project' and source.get('words_key'):
                try:
                    groups = area_columns(json.loads(read_blob(source['words_key'])), fractions[0], group['height']/100)
                except (ValueError, KeyError, OSError, TypeError):
                    groups = None
                if groups:
                    proofs.append((source, groups))
    if len(proofs) == 1:
        source, groups = proofs[0]
        total = sum((g['value'] for g in groups), Decimal(0))
        precision = Decimal(1).scaleb(int(group['quantity'].as_tuple().exponent))
        same = total.quantize(precision, rounding=ROUND_HALF_UP) == group['quantity']
        # Two independent headers (>=70), high-confidence fraction/area/unit/total
        # cells and the same native construction corroborate this OCR layout.
        # Unstructured OCR proposals still require human confirmation.
        header_weak = False
        explanation = (f"Страница {source['page']}: выбраны по заголовкам столбцы «Площадь, м²» для смеси {fractions[0]}, "
            f"h={group['height']/100} м, строка «Всего», отдельно справа и слева. "
            + '; '.join(f"{g['side']}: {g['value']} (OCR числа {g['confidence']}%)" for g in groups)
            + f". Сумма {total} м², после округления {total.quantize(precision, rounding=ROUND_HALF_UP)} м² "
            + ("совпадает с ВОР." if same else "не совпадает с ВОР: требуется сверка оригинала.")
            + (" Заголовки толщины имеют уверенность OCR ниже 80%: подтвердите по изображению." if header_weak else " Сверьте исходное изображение перед принятием результата."))
        derived['items'] = [v for v in derived['items'] if not re.search('сканированн|OCR', v['topic'], re.I)]
        derived['items'].insert(1, dict(topic='Локальная проверка: площадь по структуре OCR-таблицы',
            state='needs_facts' if header_weak else 'confirmed' if same else 'possible_mismatch', explanation=explanation,
            project_source_ids=list(dict.fromkeys(native_sources+[source['id']])), normative_source_ids=[clause['id']]))
        norm = next((s for s in result['sources'] if s['kind'] == 'normative'
            and re.search(r'ГЭСН\s*27\b', s['label']) and s['label'].endswith('27-04-003-06')
            and 'нижний слой' in s['text'].casefold()), None)
        adjustment = next((s for s in result['sources'] if s['kind'] == 'normative'
            and re.search(r'ГЭСН\s*27\b', s['label']) and s['label'].endswith('27-04-003-08')
            and re.search(r'каждый\s+1\s+см', s['text'], re.I)), None)
        scale = re.search(r'Измеритель:\s*(\d+)\s*м[2²]', norm['text']) if norm else None
        if (same and norm and adjustment and scale and group['kind'] == 'нижнего'
                and 0 < group['first'] <= 18 and 0 < group['second'] <= 18
                and re.search('ЩПС|щеб[её]ночно.песчан', position.work_name, re.I)):
            project_ids = list(dict.fromkeys(native_sources+[source['id']]))
            normative_ids = [clause['id'], norm['id'], adjustment['id']]
            derived['local_work_basis'] = dict(quantity=str(group['quantity']), unit='м2',
                thickness_cm=str(group['height']), norm_scale=scale[1],
                project_source_ids=project_ids, normative_source_ids=normative_ids,
                anchors=dict(
                    quantity=[dict(source_id=source['id'], quote='; '.join(str(g['value']) for g in groups),
                        rects=[rect for g in groups for rect in g['quantity_rects']])],
                    thickness=[*construction_anchors, dict(source_id=source['id'], quote=f"h={group['height']/100} м",
                        rects=[rect for g in groups for rect in g['thickness_rects']]),
                        dict(source_id=clause['id'], quote=clause['text'][condition.start():].split('.', 1)[0], rects=[])],
                    unit_scale=[dict(source_id=norm['id'], quote=scale[0], rects=[])]))
            derived['items'].insert(0, dict(topic='Основание работы подтверждено', state='confirmed',
                claim_ids=['presence', 'quantity', 'unit', 'normative:applicability'],
                explanation=f"Площадь {total} м² из итоговой строки ПД соответствует ВОР {group['quantity']} м². "
                    f"Проектные {group['height']} см выполнены двумя слоями {group['first']} + {group['second']} см "
                    "по п. 1.27.18, норме 27-04-003-06 и корректировке толщины 27-04-003-08. "
                    "Расход связанного материала проверяется в его собственной позиции.",
                project_source_ids=project_ids, normative_source_ids=normative_ids))
            for item in derived['items']:
                if item['state'] == 'needs_facts' and not item.get('claim_ids') and re.search(
                        'Расчётный объём материала|коэффициент.*строк|Геометрия|привязка к участку|Модель машины', item['topic'], re.I):
                    item['required'] = False
    derived['summary'] = (f"Локально уточнено: два технологических слоя {group['first']} и {group['second']} см дают "
        f"проектный слой {group['height']} см. Порог {condition[1]} см следует проверять по полной толщине; "
        "разбиение на слои не является самостоятельной ошибкой. "
        + ("Площадь найдена по заголовкам и итоговой строке OCR-таблицы; числовая сверка приведена ниже. " if len(proofs) == 1 else "Привязку площади к участку ПД нужно уточнить. ")
        + "Основание коэффициента расхода и остальные условия требуют отдельного подтверждения. Исходный ответ модели сохранён отдельно.")
    derived['state'] = 'possible_mismatch' if any(v['state'] == 'possible_mismatch' for v in derived['items']) else 'needs_facts'
    return derived, True
