import { describe, expect, it } from 'vitest'
import type { NormativeCheckSource, NormativeParameterCard } from '../../app/generated/types.gen'
import {
  cardExplanation,
  cardValue,
  compactText,
  formulaPreview,
  sourceAnchor,
  sourceSection,
  evidenceRects,
} from '../../app/utils/normativePresentation'

const source = (label: string): NormativeCheckSource => ({
  id: 'n1',
  kind: 'normative',
  label,
  page: 2,
  text: 'Норматив',
  origin: 'native',
})
const card = (changes: Partial<NormativeParameterCard> = {}): NormativeParameterCard => ({
  id: 'quantity',
  label: 'Количество',
  role: 'project_value',
  declared_value: '65.259432',
  unit: 'м3',
  state: 'needs_facts',
  reason: 'Нужно подтверждение.',
  ...changes,
})

describe('compact normative chain', () => {
  it('keeps exact numeric precision while shortening explanation', () => {
    expect(cardValue(card())).toBe('65,259432 м³')
    expect(formulaPreview('(431.61*0.12)*1.26')).toBe('(431,61 × 0,12) × 1,26')
    expect(compactText('Полное пояснение '.repeat(40), 100).length).toBeLessThanOrEqual(101)
    expect(cardExplanation(card({ role: 'coefficient', quote: '' }))).toContain(
      'условия применения',
    )
  })
  it('keeps discrepancies and summed layer thickness visible', () => {
    expect(cardValue(card({ unit: 'см', declared_value: '12', compared_value: '24' }))).toBe(
      '12 см → 24 см всего',
    )
    expect(
      cardValue(card({ state: 'possible_mismatch', found_value: '60', found_unit: 'м3' })),
    ).toBe('65,259432 м³ → 60 м³')
  })
  it('anchors a subsection rather than the document edition and table rather than variant column', () => {
    expect(sourceSection(source('СП 76.13330.2016 · 6.3.1'))).toBe('6.3.1')
    expect(sourceAnchor(source('ГЭСН 27 · 1.27.18'))).toBe('1.27.18')
    expect(sourceAnchor(source('ГЭСН 27 · 27-04-003-06'))).toBe('27-04-003')
    expect(sourceAnchor({ ...source('file.pdf'), kind: 'project' }, 'Текст основания')).toBe(
      'Текст основания',
    )
  })
  it('prefers the actual parameter quote to a generic section header', () => {
    expect(sourceAnchor(source('ГЭСН 27 · 27-04-003-06'), 'Измеритель: 1000 м2')).toBe(
      'Измеритель: 1000 м2',
    )
    const s = source('ГЭСН 27 · 1.27.18')
    expect(sourceAnchor(s, s.text)).toBe('1.27.18')
  })
  it('accepts only bounded page-relative rectangles and preserves separate cells', () => {
    const rects = [
      [0.2, 0.3, 0.25, 0.32],
      [0.7, 0.3, 0.75, 0.32],
    ]
    expect(evidenceRects(rects)).toEqual(rects)
    expect(
      evidenceRects([
        [20, 30, 25, 32],
        [0, 0, 1, Infinity],
        [0.5, 0.5, 0.4, 0.6],
        [0, 0, 1],
      ]),
    ).toEqual([])
    const copy = evidenceRects(rects)
    copy[0]![0] = 0
    expect(rects[0]![0]).toBe(0.2)
  })
})
