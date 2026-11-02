export function quantityText(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') return '—'
  let text = String(value).trim().replace(/\s/g, '')
  // Normalize decimal mark first so 231.0 and 231,0 compare equal.
  const num = Number(text.replace(',', '.'))
  if (Number.isFinite(num)) {
    // Strip insignificant zeros: 231.0 -> 231, 78.90 -> 78,9.
    text = String(Math.round(num * 1000) / 1000).replace('.', ',')
    return text
  }
  return String(value).replace(/(?<=\d)\.(?=\d)/, ',')
}
export function unitText(unit: string | null | undefined) {
  // шт. and шт are the same unit; trailing dots vary by author.
  return (unit || '').trim().replace(/\.+$/, '')
}
export function quantityWithUnit(
  value: string | number | null | undefined,
  unit: string | null | undefined,
) {
  if (value === null || value === undefined || value === '') return '—'
  const text = quantityText(value),
    suffix = unitText(unit)
  return !suffix ||
    String(value).trim().toLocaleLowerCase('ru-RU').endsWith(suffix.toLocaleLowerCase('ru-RU'))
    ? text
    : text + ' ' + suffix
}
export function quantityDelta(
  declared: string | number | null | undefined,
  found: string | number | null | undefined,
): string | null {
  const parse = (value: string | number | null | undefined): number | null => {
    if (value === null || value === undefined || value === '') return null
    const num = Number(String(value).replace(/\s/g, '').replace(',', '.'))
    return Number.isFinite(num) ? num : null
  }
  const a = parse(declared)
  const b = parse(found)
  if (a === null || b === null) return null
  // Round to 3 decimals to avoid float artifacts (0.1 + 0.2).
  const diff = Math.round((b - a) * 1000) / 1000
  if (diff === 0) return '±0'
  const text = quantityText(String(Math.abs(diff)))
  return (diff > 0 ? '+' : '−') + text
}
export function queueLabel(queue: string) {
  return (
    {
      fix: 'Расхождение',
      review: 'Проверить вручную',
      missing: 'Проверка не выполнена',
      ok: 'Подтверждено',
      pending: 'Проверяется по нормативам',
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
  return (
    {
      running: 'Выполняется',
      done: 'Завершена',
      failed: 'Сбой проверки',
      cancelled: 'Остановлена',
    }[status] || status
  )
}
export function clampPage(page: number, count: number) {
  return Math.max(1, Math.min(count, page || 1))
}
export function formatFileName(value: string | null | undefined): string {
  if (value === null || value === undefined) return ''
  const raw = String(value).trim()
  if (!raw) return ''
  const dot = raw.lastIndexOf('.')
  let stem = dot > 0 ? raw.slice(0, dot) : raw
  const ext = dot > 0 ? raw.slice(dot) : ''
  // Drop OS duplicate-copy suffixes like ' (1)' before the extension.
  stem = stem.replace(/(\s\(\d+\))+$/, '').trim()
  // Underscores separate words in exported filenames, not readable text.
  stem = stem.replace(/_+/g, ' ').replace(/\s+/g, ' ').trim()
  return stem + ext || raw
}
export function truncateMiddle(value: string, max = 64): string {
  const text = String(value ?? '')
  if (max < 8 || text.length <= max) return text
  const head = Math.ceil((max - 1) / 2),
    tail = Math.floor((max - 1) / 2)
  return (
    text.slice(0, head).replace(/\s+$/, '') +
    '…' +
    text.slice(text.length - tail).replace(/^\s+/, '')
  )
}
export function runStatusColor(status: string): 'success' | 'error' | 'warning' | 'neutral' {
  return status === 'done'
    ? 'success'
    : status === 'failed'
      ? 'error'
      : status === 'running'
        ? 'warning'
        : 'neutral'
}
export function firstSentence(value: string): string {
  const text = String(value ?? '').trim()
  if (!text) return ''
  const sentences = new Intl.Segmenter('ru', { granularity: 'sentence' }).segment(text)
  return sentences[Symbol.iterator]().next().value?.segment.trim() || text
}
