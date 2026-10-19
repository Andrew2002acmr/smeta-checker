export function quantityText(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') return '—'
  return String(value).replace(/(?<=\d)\.(?=\d)/, ',')
}
export function quantityWithUnit(
  value: string | number | null | undefined,
  unit: string | null | undefined,
) {
  if (value === null || value === undefined || value === '') return '—'
  const text = quantityText(value),
    suffix = unit?.trim() || ''
  return !suffix ||
    String(value).trim().toLocaleLowerCase('ru-RU').endsWith(suffix.toLocaleLowerCase('ru-RU'))
    ? text
    : text + ' ' + suffix
}
export function queueLabel(queue: string) {
  return (
    {
      fix: 'Расхождение',
      review: 'Проверить вручную',
      missing: 'Проверка не выполнена',
      ok: 'Подтверждено',
    }[queue] || 'Не проверено'
  )
}
export function queueColor(queue: string): 'error' | 'warning' | 'success' | 'neutral' {
  return queue === 'fix'
    ? 'error'
    : queue === 'ok'
      ? 'success'
      : queue === 'review'
        ? 'warning'
        : 'neutral'
}
export function runLabel(status: string) {
  return { running: 'Выполняется', done: 'Завершена', failed: 'Сбой проверки' }[status] || status
}
