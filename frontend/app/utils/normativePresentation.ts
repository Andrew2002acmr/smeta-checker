import type { NormativeCheckSource, NormativeParameterCard } from '~/generated/types.gen'
import { evidenceText } from '~/utils/evidenceText'
import { firstSentence, unitText } from '~/utils/presentation'

export function compactText(raw: string, limit = 180): string {
  const text = evidenceText(raw).replace(/\s+/g, ' ').trim()
  if (text.length <= limit) return text
  return (text.slice(0, limit).replace(/\s+\S*$/, '') || text.slice(0, limit)) + '…'
}

export function formulaPreview(raw: string): string {
  return compactText(raw.replace(/(?<=\d)\.(?=\d)/g, ',').replace(/\*/g, ' × '), 160)
}

function exactValue(value: string, unit?: string | null): string {
  const displayUnit = unitText(unit).replace(
    /([мm])([23])(?=$|[/\s])/g,
    (_, prefix, power) => prefix + (power === '2' ? '²' : '³'),
  )
  return [String(value).replace(/(?<=\d)\.(?=\d)/g, ','), displayUnit].filter(Boolean).join(' ')
}

export function sourceSection(source: NormativeCheckSource): string {
  if (source.kind !== 'normative' || !source.label.includes('·')) return ''
  const section = source.label.split('·').at(-1)?.trim() ?? ''
  return /^(?:\d+(?:\.\d+)+|\d{2}-\d{2}-\d{3}(?:-\d{2,3})?)$/.test(section) ? section : ''
}

export function sourceAnchor(source: NormativeCheckSource, quote = ''): string {
  if (quote.trim() && quote !== source.text) return evidenceText(quote).slice(0, 480)
  const section = sourceSection(source)
  if (section) return section.replace(/^(\d{2}-\d{2}-\d{3})-\d{2,3}$/, '$1')
  return evidenceText(quote).slice(0, 140)
}

export function evidenceRects(rects?: number[][]): number[][] {
  return (rects ?? [])
    .filter(
      (r) =>
        r.length === 4 &&
        r.every((v) => Number.isFinite(v) && v >= 0 && v <= 1) &&
        r[0]! < r[2]! &&
        r[1]! < r[3]!,
    )
    .slice(0, 40)
    .map((r) => [...r])
}

export function cardTitle(card: NormativeParameterCard): string {
  if (card.role === 'calculation') return `Расчёт${card.row ? ` · строка ${card.row}` : ''}`
  if (card.role === 'coefficient') return `Параметр ${exactValue(card.declared_value, card.unit)}`
  if (card.role === 'unit_scale') return 'Измеритель нормы'
  if (card.id === 'thickness') return 'Толщина слоя'
  return card.label.includes('родительской') ? 'Объём связанной работы' : 'Объём по проекту'
}

export function cardExplanation(card: NormativeParameterCard): string {
  if (card.role === 'calculation' && card.state === 'confirmed')
    return 'Расчёт совпадает с объёмом ВОР.'
  if (card.role === 'coefficient' && card.state === 'needs_facts' && !card.quote)
    return 'Нужно нормативное основание и условия применения.'
  if (card.reason.includes('OCR') || card.reason.includes('распозна'))
    return 'Распознанные данные нужно подтвердить по оригиналу ПД.'
  if (card.state === 'needs_facts' && !card.quote && !card.found_value)
    return card.role === 'unit_scale'
      ? 'Нужно установить единицу и масштаб нормы.'
      : 'Нужно подтверждение в ПД.'
  return compactText(firstSentence(evidenceText(card.reason)), 180)
}

export function cardQuote(card: NormativeParameterCard): string {
  if (card.quote) return compactText(card.quote, 190)
  return ''
}

export function cardValue(card: NormativeParameterCard): string {
  if (card.role === 'unit_scale')
    return card.found_value
      ? exactValue(card.found_value, card.found_unit)
      : `${unitText(card.unit)} · масштаб не указан`
  const declared = exactValue(card.declared_value, card.unit)
  if (card.compared_value && card.compared_value !== card.declared_value)
    return `${declared} → ${exactValue(card.compared_value, card.unit)} всего`
  if (card.found_value && card.state === 'possible_mismatch')
    return `${declared} → ${exactValue(card.found_value, card.found_unit ?? card.unit)}`
  return declared
}
