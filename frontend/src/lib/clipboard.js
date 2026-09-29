export async function copyTextToClipboard(text, environment = {}) {
  const clipboard = environment.clipboard ?? (typeof navigator !== 'undefined' ? navigator.clipboard : undefined)
  const doc = environment.document ?? (typeof document !== 'undefined' ? document : undefined)

  if (clipboard) {
    try {
      await clipboard.writeText(text)
      return true
    } catch {
      // Try the legacy DOM copy path when permission or secure-context rules block the API.
    }
  }

  if (!doc?.body || typeof doc.execCommand !== 'function') return false

  const textarea = doc.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  doc.body.appendChild(textarea)
  try {
    textarea.select()
    return doc.execCommand('copy')
  } catch {
    return false
  } finally {
    doc.body.removeChild(textarea)
  }
}
