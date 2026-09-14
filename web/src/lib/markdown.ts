import DOMPurify from 'dompurify'
import { marked } from 'marked'

/**
 * Markdown -> sanitized HTML.
 *
 * Assistant output is untrusted text: the model can be steered into emitting a
 * `<script>` or an `onerror` attribute, so the HTML string is always filtered
 * before it reaches `dangerouslySetInnerHTML`. DOMPurify is configured to allow
 * nothing that can execute.
 */

marked.setOptions({
  gfm: true,
  breaks: true,
})

// Sanitizing on every keystroke of a streamed answer is wasteful; the same text
// always maps to the same HTML, so a small module-level cache pays for itself.
const CACHE_LIMIT = 200
const cache = new Map<string, string>()

export function renderMarkdown(source: string): string {
  const hit = cache.get(source)
  if (hit !== undefined) return hit

  // `marked.parse` can return a Promise when async extensions are used; with the
  // default synchronous options it is always a string.
  const raw = marked.parse(source) as string
  const clean = DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target', 'rel'],
  })

  if (cache.size >= CACHE_LIMIT) {
    // Cheap FIFO eviction: enough to bound memory during a long stream.
    const oldest = cache.keys().next()
    if (!oldest.done) cache.delete(oldest.value)
  }
  cache.set(source, clean)
  return clean
}

/** Pretty-print tool arguments, tolerating a half-streamed JSON string. */
export function prettyJson(value: unknown): string {
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      // Still arriving, or genuinely not JSON: show the raw text.
      return value
    }
  }
  if (value === undefined || value === null) return ''
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
