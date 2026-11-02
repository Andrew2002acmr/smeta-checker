import type {
  NormativePositionCheck,
  NormativeParameterCard,
  WorkspaceDto,
} from '~/generated/types.gen'
import { cardTitle, cardValue, compactText } from './normativePresentation'
import { firstSentence } from './presentation'

export type DecisionRow = Pick<WorkspaceDto['row'], 'queue' | 'problem'>
function title(card: NormativeParameterCard) {
  if (card.id !== 'quantity') return cardTitle(card)
  const unit = card.unit.normalize('NFKC').replace(/\./g, '').trim().toLowerCase()
  return unit.includes('м2')
    ? 'Площадь'
    : unit.includes('м3')
      ? 'Объём'
      : ['м', 'км', 'мм', 'см'].includes(unit)
        ? 'Длина'
        : ['т', 'кг'].includes(unit)
          ? 'Масса'
          : 'Количество'
}
export function normativeDecision(result: NormativePositionCheck, row?: DecisionRow, busy = false) {
  const state =
    row?.queue === 'ok'
      ? 'ok'
      : row?.queue === 'fix'
        ? 'fix'
        : row?.queue === 'pending' || busy
          ? 'pending'
          : 'review'
  const cards = result.parameter_cards ?? []
  const issues = cards
    .filter((c) => c.required !== false && c.state !== 'confirmed')
    .map((card) => {
      let detail = '',
        action: 'basis' | 'norm' = 'basis'
      if (card.state === 'possible_mismatch') {
        const comparison = cardValue({
          ...card,
          declared_value: card.compared_value || card.declared_value,
          compared_value: null,
        })
        detail = card.compared_value
          ? `${comparison} — сравнивается общая толщина. Если ПД верен, приведите сумму толщин связанных слоёв к проектной толщине.`
          : `${comparison}. Сверьте источник. Если значение ПД верно, исправьте эту величину в ВОР.`
      } else if (card.role === 'coefficient') {
        detail = 'Найдите пункт документа с этим коэффициентом и проверьте условия его применения.'
        action = 'norm'
      } else if (card.role === 'unit_scale') {
        detail = 'Выберите подходящую норму и проверьте её измеритель перед пересчётом объёма.'
        action = 'norm'
      } else if (card.role === 'calculation')
        detail = 'Проверьте формулу и исходные значения в строке ВОР.'
      else if (card.id === 'thickness')
        detail = `${cardValue(card)}. Найдите полную толщину конструкции в ПД и укажите этот фрагмент.`
      else
        detail = `${cardValue(card)} в ВОР. Найдите соответствующую строку таблицы ПД и укажите её как основание.`
      return { card, title: title(card), detail, action }
    })
  const confirmed = cards
    .filter((c) => c.required !== false && c.state === 'confirmed')
    .map((c) => `${title(c)}: ${cardValue(c)}`)
  if (result.items.some((v) => v.state === 'confirmed' && /общая толщина и порог/.test(v.topic)))
    confirmed.push('Разбиение на два слоя обосновано нормативом.')
  return {
    state,
    issues,
    confirmed,
    title: {
      ok: 'Можно оставить без изменений',
      fix: 'Нужно устранить расхождение',
      pending: 'Проверка ещё идёт',
      review: 'Не хватает подтверждения',
    }[state],
    explanation:
      state === 'ok'
        ? 'Обязательные проверки позиции пройдены. Исправление не требуется.'
        : state === 'pending'
          ? 'Дождитесь окончания нормативного этапа. Готовые результаты сохраняются.'
          : state === 'fix'
            ? compactText(firstSentence(row?.problem ?? result.summary), 240)
            : 'Ошибка пока не доказана. Уточните основание по ПД или нормативам.',
  }
}
