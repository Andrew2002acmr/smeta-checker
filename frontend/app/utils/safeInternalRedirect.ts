export function safeInternalRedirect(value: unknown): string {
  if (
    typeof value !== 'string' ||
    !value.startsWith('/') ||
    value.startsWith('//') ||
    value.includes('\\')
  )
    return '/'
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i)
    if (code <= 32 || code === 127) return '/'
  }
  try {
    const url = new URL(value, 'https://workspace.invalid')
    if (url.origin !== 'https://workspace.invalid' || url.pathname === '/login') return '/'
    return url.pathname + url.search + url.hash
  } catch {
    return '/'
  }
}
