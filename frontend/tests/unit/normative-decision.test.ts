import { describe, it, expect } from 'vitest'
import type { NormativePositionCheck } from '../../app/generated/types.gen'
import { normativeDecision } from '../../app/utils/normativeDecision'

const result = (state = 'needs_facts', required = true) =>
  ({
    summary: 'Технический разбор',
    state: 'confirmed',
    items: [],
    parameter_cards: [
      {
        id: 'quantity',
        label: 'Количество',
        role: 'project_value',
        declared_value: '431.61',
        unit: 'м2',
        state,
        reason: 'Нет структурированного подтверждения',
        required,
      },
    ],
  }) as NormativePositionCheck

describe('engineer decision', () => {
  it('never promotes a confirmed aspect to a published OK', () => {
    const d = normativeDecision(result('confirmed'), { queue: 'review', problem: '' })
    expect(d.state).toBe('review')
    expect(d.explanation).toContain('Ошибка пока не доказана')
    expect(normativeDecision(result('confirmed')).state).toBe('review')
  })
  it('explains what to do for missing quantity and excludes child material checks', () => {
    expect(normativeDecision(result()).issues[0]?.detail).toContain('строку таблицы ПД')
    expect(normativeDecision(result()).issues[0]?.action).toBe('basis')
    expect(normativeDecision(result('needs_facts', false)).issues).toEqual([])
  })
  it('keeps correct, wrong and still processing outcomes distinct', () => {
    const assessment = {
      revision: 'a'.repeat(64),
      run_id: 'run-1',
      position_index: 0,
      execution_revision: 0,
      normative: { state: 'confirmed', obligations: [] },
      completion: 'complete',
    }
    expect(
      normativeDecision(result('confirmed'), { queue: 'ok', problem: '', assessment }).title,
    ).toBe('Можно оставить без изменений')
    const incomplete = {
      ...assessment,
      normative: { state: 'needs_facts', obligations: [] },
      completion: 'incomplete',
    }
    expect(
      normativeDecision(result('confirmed'), { queue: 'ok', problem: '', assessment: incomplete })
        .title,
    ).toBe('Не хватает подтверждения')
    const wrong = result('possible_mismatch')
    wrong.parameter_cards![0]!.found_value = '400'
    expect(
      normativeDecision(wrong, { queue: 'fix', problem: 'Различаются площади.' }).issues[0]?.detail,
    ).toContain('400')
    expect(normativeDecision(result(), { queue: 'pending', problem: '' }).state).toBe('pending')
  })
  it('asks for a normative basis instead of changing an unproved coefficient', () => {
    const r = result()
    r.parameter_cards![0]!.role = 'coefficient'
    const d = normativeDecision(r, { queue: 'review', problem: '' })
    expect(d.issues[0]?.action).toBe('norm')
    expect(d.issues[0]?.detail).toContain('условия')
  })
  it('compares full thickness and never recommends assigning it to each sublayer', () => {
    const r = result('possible_mismatch')
    Object.assign(r.parameter_cards![0]!, {
      id: 'thickness',
      declared_value: '12',
      compared_value: '24',
      found_value: '28',
      unit: 'см',
      found_unit: 'см',
    })
    const issue = normativeDecision(r, { queue: 'fix', problem: '' }).issues[0]!
    expect(issue.detail).toContain('24 см → 28 см')
    expect(issue.detail).toContain('сумму толщин')
  })
})

it('requests the real missing input for typed workbook operands', () => {
  const r = result()
  Object.assign(r.parameter_cards![0]!, {
    id: 'factor:7:1000',
    label: 'Перевод литров и масса',
    role: 'calculation',
    reason: 'Для получения тонн нужна плотность эмульсии.',
  })
  const issue = normativeDecision(r).issues[0]!
  expect(issue.title).toBe('Перевод литров и масса')
  expect(issue.detail).toContain('плотность')
  expect(issue.detail).not.toContain('строку таблицы ПД')
})
