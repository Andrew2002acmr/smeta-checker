import { describe, it, expect } from 'vitest'
import {
  clampPage,
  queueColor,
  quantityDelta,
  quantityText,
  quantityWithUnit,
  firstSentence,
} from '../../app/utils/presentation'

describe('short result in the audit list', () => {
  it('shows one sentence without splitting decimal quantities', () => {
    expect(firstSentence('Объём 108,66 м³ в проекте не нашёлся. В ВОР единица «м³».')).toBe(
      'Объём 108,66 м³ в проекте не нашёлся.',
    )
    expect(firstSentence('В ВОР 8.395 т, в ПД 8.3 т. Проверьте расчёт.')).toBe(
      'В ВОР 8.395 т, в ПД 8.3 т.',
    )
    expect(firstSentence('Расхождений не обнаружено.')).toBe('Расхождений не обнаружено.')
    expect(firstSentence('')).toBe('')
  })
})

describe('domain status mapping', () => {
  it('maps the four queues to distinct functional colors', () => {
    expect(queueColor('fix')).toBe('error')
    expect(queueColor('review')).toBe('warning')
    expect(queueColor('ok')).toBe('success')
    // A technical failure / unchecked state is neutral, never red.
    expect(queueColor('missing')).toBe('neutral')
    expect(queueColor('unknown')).toBe('neutral')
  })
})

describe('quantity rendering', () => {
  it('keeps precision and separators, never float artifacts', () => {
    expect(quantityText('78.9')).toBe('78,9')
    expect(quantityText(45)).toBe('45')
    expect(quantityText('')).toBe('—')
    expect(quantityText(null)).toBe('—')
    expect(quantityWithUnit('45', 'м')).toBe('45 м')
    expect(quantityWithUnit('45 м', 'м')).toBe('45 м')
    expect(quantityWithUnit(null, null)).toBe('—')
  })
  it('normalizes equal quantities to identical strings', () => {
    expect(quantityText('231.0')).toBe('231')
    expect(quantityText('231,0')).toBe('231')
    expect(quantityText('78.90')).toBe('78,9')
    expect(quantityWithUnit('231,0', 'шт.')).toBe('231 шт')
    expect(quantityWithUnit('231', 'шт')).toBe('231 шт')
  })
  it('computes signed deltas without float artifacts', () => {
    expect(quantityDelta('231,0', '231')).toBe('±0')
    expect(quantityDelta('231', '243,5')).toBe('+12,5')
    expect(quantityDelta('100', '87')).toBe('−13')
    expect(quantityDelta('0,1', '0,3')).toBe('+0,2')
    expect(quantityDelta(null, '5')).toBe(null)
    expect(quantityDelta('abc', '5')).toBe(null)
  })
})

describe('physical page clamp', () => {
  it('keeps 1-based PDF.js pages inside the document', () => {
    expect(clampPage(1, 10)).toBe(1)
    expect(clampPage(0, 10)).toBe(1)
    expect(clampPage(-3, 10)).toBe(1)
    expect(clampPage(11, 10)).toBe(10)
    expect(clampPage(Number.NaN, 10)).toBe(1)
  })
})
