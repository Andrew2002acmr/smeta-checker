"""Derive the published verdict after normative validation; raw matching is immutable."""
from copy import deepcopy

PENDING = 'Проверяется по нормативам'
FIX = 'Обнаружено расхождение'
REVIEW = 'Требуется ручная проверка'


def verified_quantity(review, position_key, unit, *, derived=False):
    """Use a scoped, source-backed quantity in the UI without mutating matching."""
    if derived or not review or review.get('position_key') != position_key:
        return None
    def normalize(value):
        return str(value).strip().rstrip('.').replace('²','2').replace('³','3')
    card = next((c for c in review.get('parameter_cards') or [] if c.get('id') == 'quantity'
        and c.get('state') == 'confirmed' and c.get('source_ids') and c.get('found_value') is not None
        and normalize(c.get('found_unit')) == normalize(unit)), None)
    return (card['found_value'], card['found_unit']) if card else None


def required_claims(stored, report_status, derived=False):
    unresolved = {c['claim_id'] for c in stored.get('checks') or []
        if c.get('claim_id') and c.get('required', True)
        and c.get('state') not in {'confirmed', 'not_applicable'}}
    if report_status != 'ОК' and not unresolved:
        unresolved.add('matching:verdict')
    unresolved.add('normative:applicability')
    if derived:
        unresolved.update({'derived:rate', 'derived:parent'})
    return unresolved


def publish(report, stored, position_key, validation, *, enabled=False, derived=False):
    """A failed/unsupported second layer is uncertainty, never a proved mismatch."""
    if not enabled and not validation:
        return report
    if report['Статус аудита'] == 'Проверка не выполнена':
        return report
    output = deepcopy(report)
    stage = (validation or {}).get('stage')
    review = (validation or {}).get('review')
    if review and review.get('position_key') != position_key:
        review = None
    if review and stage in {'pending', 'dispatched', 'running'}:
        from normative_local_checks import LOCAL_RULES_VERSION
        if review.get('local_rules_version', 0) < LOCAL_RULES_VERSION:
            output['Статус аудита'] = PENDING
            output['Что не так'] = 'Автоматически обновляем основание по новым правилам. Готовый результат сохранён; новый запрос к модели не выполняется.'
            return output
    if not review:
        pending = stage in {'pending', 'dispatched', 'running'} or (enabled and stage is None)
        output['Статус аудита'] = PENDING if pending else REVIEW
        output['Что не так'] = ('Сопоставление выполнено. Проверяем основание по нормативам.' if pending else
            'Нормативная валидация не завершена или недостаточно источников. Расхождение не подтверждено.')
        return output
    items = review.get('items') or []
    arithmetic_errors = [v.get('message') or 'Ошибка арифметики формулы ВОР.' for v in stored.get('workbook_notes') or []
        if v.get('kind') == 'formula-mismatch']
    if arithmetic_errors:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(arithmetic_errors)
        return output
    supported = [v for v in items if v.get('project_source_ids') and v.get('normative_source_ids') and v.get('claim_ids')]
    contradictions = [v for v in supported if v.get('state') == 'possible_mismatch']
    confirmed = {claim for v in supported if v.get('state') == 'confirmed' for claim in v['claim_ids']}
    needed = required_claims(stored, report['Статус аудита'], derived)
    cards = review.get('parameter_cards') or []
    card_gaps = [c for c in cards if c.get('required', True) and c['state'] != 'confirmed']
    card_conflicts = [c for c in card_gaps if c['state'] == 'possible_mismatch']
    if card_conflicts and 'normative:applicability' in confirmed:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(c['label']+': '+c['reason'] for c in card_conflicts)
        return output
    if contradictions:
        output['Статус аудита'] = FIX
        output['Что не так'] = ' '.join(v['explanation'] for v in contradictions)
    elif items and needed <= confirmed and not card_gaps and all(
            v.get('state') == 'confirmed' or (v.get('state') == 'needs_facts'
                and v.get('claim_ids') and set(v['claim_ids']) <= confirmed)
            or v.get('required') is False for v in items):
        output['Статус аудита'] = 'ОК'
        output['Что не так'] = 'Основание работы подтверждено по ПД и нормативам. ' + next(
            (v['explanation'] for v in supported if v.get('topic') == 'Основание работы подтверждено'), review.get('summary', ''))
    else:
        output['Статус аудита'] = REVIEW
        uncertain = [v['explanation'] for v in items if v.get('state') == 'needs_facts']
        output['Что не так'] = (' '.join(uncertain) or review.get('summary') or 'Недостаточно нормативных оснований для окончательного вывода.')
        if needed - confirmed:
            output['Что не так'] += ' Не все условия подтверждены; расхождение не доказано.'
        if card_gaps:
            output['Что не так'] = ' '.join(c['label']+': '+c['reason'] for c in card_gaps)
    return output


def apply_parent_verdicts(rows, *, enabled, running):
    """A derived material cannot be confirmed before its parent work basis."""
    if not enabled:
        return rows
    by_index = {r.position_index: r for r in rows}
    for row in rows:
        if not row.related_work or row.queue in {'fix', 'missing'}:
            continue
        parent = by_index.get(row.related_work['index'])
        if (parent is None and running) or (parent is not None and parent.queue == 'pending'):
            row.queue, row.report_status = 'pending', PENDING
            row.problem = f"Расчётный материал работы №{row.related_work['number']}. Ожидаем проверку основания связанной работы."
        elif row.queue == 'ok' and (parent is None or parent.queue != 'ok'):
            row.queue, row.report_status = 'review', REVIEW
            row.problem = f"Расчёт связан с работой №{row.related_work['number']}, но её основание ещё не подтверждено."
    return rows
