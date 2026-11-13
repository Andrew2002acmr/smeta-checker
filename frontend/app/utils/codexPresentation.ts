import type { DirectionConclusion, PositionConclusion } from '~/generated/types.gen'

export const codexDirections = [
  { key: 'project', label: 'Проектная документация', shortLabel: 'ПД' },
  { key: 'normative', label: 'Нормативная применимость', shortLabel: 'Нормативы' },
  { key: 'quantities', label: 'Взаимосвязи и расчёты', shortLabel: 'Расчёты' },
] as const satisfies { key: keyof PositionConclusion; label: string; shortLabel: string }[]

export const codexStatuses = {
  confirmed: { label: 'Подтверждено', color: 'success' },
  mismatch: { label: 'Расхождение', color: 'error' },
  needs_facts: { label: 'Нужны основания', color: 'warning' },
  not_checked: { label: 'Не проверено', color: 'neutral' },
  not_applicable: { label: 'Неприменимо', color: 'neutral' },
} as const satisfies Record<DirectionConclusion['status'], { label: string; color: string }>

export function codexFindings(
  result: PositionConclusion,
  key: (typeof codexDirections)[number]['key'],
) {
  return [
    result[key],
    ...(result.parameter_findings ?? [])
      .filter((item) => item.direction === key)
      .map((item) => item.finding),
  ]
}

export function codexDirectionStatus(
  result: PositionConclusion,
  key: (typeof codexDirections)[number]['key'],
) {
  const statuses = new Set(codexFindings(result, key).map((finding) => finding.status))
  return (
    (['mismatch', 'needs_facts', 'not_checked', 'confirmed'] as const).find((status) =>
      statuses.has(status),
    ) ?? 'not_applicable'
  )
}

export const codexStageLabels: Record<string, string> = {
  queued: 'В очереди на Codex',
  preparing: 'Готовим закреплённые документы',
  running: 'Codex анализирует ведомость',
  validating: 'Проверяем структуру, расчёты и ссылки',
  persisting: 'Сохраняем результаты',
}
