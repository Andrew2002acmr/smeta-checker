import { describe, it, expect } from 'vitest'
import { backendError } from '../../app/composables/useBackend'
import { zResultDto } from '../../app/generated/zod.gen'
import { quantityWithUnit } from '../../app/utils/presentation'

describe('workspace boundary', () => {
  it('displays a unit once and keeps unknown quantities unknown', () => {
    expect(quantityWithUnit('45 m', 'm')).toBe('45 m')
    expect(quantityWithUnit('45.25', 'm')).toBe('45,25 m')
    expect(quantityWithUnit(null, 'm')).toBe('—')
  })
  it('shows the upstream nested error instead of a generic success state', () => {
    expect(
      backendError({ error: { code: 'llm_not_configured', message: 'Модель не подключена' } }),
    ).toBe('Модель не подключена')
  })
  it('rejects unknown audit outcomes and invented queues', () => {
    const row = {
      id: 'run:0',
      position_index: 0,
      number: '1',
      name: 'Cable',
      unit: 'm',
      declared_quantity: '45',
      found_quantity: null,
      found_unit: null,
      formula: '',
      sheet: '',
      source_row: null,
      cells: {},
      audit_status: 'Неполная проверка',
      report_status: 'Требуется ручная проверка',
      queue: 'review',
      problem: 'Нет подтверждения',
      action: 'Проверить вручную',
      work_found: false,
      volume_confirmed: false,
      unit_confirmed: false,
      comment_confirmed: false,
      details: '',
    }
    expect(zResultDto.safeParse(row).success).toBe(true)
    expect(zResultDto.safeParse({ ...row, audit_status: 'success' }).success).toBe(false)
    expect(zResultDto.safeParse({ ...row, queue: 'completed' }).success).toBe(false)
  })
})
