/** Display only: original evidence is kept for PDF search and confirmation. */
export function evidenceText(raw: string): string {
  return String(raw ?? '')
    .replace(/<\/(?:td|th)>\s*<(?:td|th)\b[^>]*>/gi, ' | ')
    .replace(/<li\b[^>]*>/gi, '\n• ')
    .replace(/<\/?(?:p|div|h[1-6]|tr|ul|ol)\b[^>]*>/gi, '\n')
    .replace(/<br\s*\/?\s*>/gi, '\n')
    .replace(/<[^>]*>/g, '')
    .replace(
      /&(?:nbsp|amp|lt|gt|quot|apos);/gi,
      (entity) =>
        ({ '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'" })[
          entity.toLowerCase()
        ] ?? entity,
    )
    .replace(/(?<![\p{L}\p{N}])(?:\*\*|__)([\s\S]*?\S)(?:\*\*|__)(?![\p{L}\p{N}])/gu, '$1')
    .replace(/(?<![\p{L}\p{N}])_([^_\n]*?\S)_(?![\p{L}\p{N}])/gu, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/\t+/g, ' | ')
    .replace(/[ \u00a0]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

export const EVIDENCE_PREVIEW_LENGTH = 420

export function evidenceExcerpt(raw: string): string {
  const text = evidenceText(raw)
  if (text.length <= EVIDENCE_PREVIEW_LENGTH) return text
  const prefix = text.slice(0, EVIDENCE_PREVIEW_LENGTH)
  const end = Math.max(prefix.lastIndexOf(' '), prefix.lastIndexOf('\n'))
  return (end > EVIDENCE_PREVIEW_LENGTH * 0.7 ? prefix.slice(0, end) : prefix).trimEnd() + '…'
}
