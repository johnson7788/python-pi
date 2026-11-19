import type { ServerToolRound, ServerToolSource, UrlCitation } from './protocol'

/**
 * Citation placement.
 *
 * The provider reports each cited span as character offsets into the answer
 * text, so badges are placed by *editing the markdown source* before it is
 * rendered.  Everything here is therefore deliberately conservative: an offset
 * that lands inside a code span or a fenced block is skipped, because injecting
 * markup there would render literally and look broken.
 */

export interface CitationRange {
  /** Badge number, 1-based and shared by every span citing the same URL. */
  ordinal: number
  url: string
  title: string | null
  start: number
  end: number
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '')

/** Flatten `annotations` into usable ranges, dropping anything malformed. */
export function toCitationRanges(annotations: UrlCitation[] | undefined): CitationRange[] {
  if (!Array.isArray(annotations)) return []
  const ordinals = new Map<string, number>()
  const ranges: CitationRange[] = []
  for (const annotation of annotations) {
    const citation = annotation?.url_citation
    if (!citation) continue
    const url = asString(citation.url)
    if (!url) continue
    const start = Number.isFinite(citation.start_index) ? Number(citation.start_index) : 0
    const end = Number.isFinite(citation.end_index) ? Number(citation.end_index) : start
    if (end <= start) continue
    let ordinal = ordinals.get(url)
    if (ordinal === undefined) {
      ordinal = ordinals.size + 1
      ordinals.set(url, ordinal)
    }
    ranges.push({
      ordinal,
      url,
      title: typeof citation.title === 'string' && citation.title ? citation.title : null,
      start: Math.max(0, start),
      end: Math.max(0, end),
    })
  }
  return ranges.sort((a, b) => a.end - b.end)
}

/**
 * True when `offset` sits in code, where injected markup would be rendered as
 * text.  Counts fences and inline backticks rather than parsing markdown: the
 * only thing that matters is avoiding an obviously broken badge.
 */
function insideCode(text: string, offset: number): boolean {
  const upto = text.slice(0, offset)
  if ((upto.match(/```/g) ?? []).length % 2 === 1) return true
  const line = upto.slice(upto.lastIndexOf('\n') + 1)
  return (line.match(/`/g) ?? []).length % 2 === 1
}

/**
 * Insert `<sup data-cite-idx="n">n</sup>` after each cited span.
 *
 * The result is markdown plus inline HTML; `renderMarkdown` sanitizes it with
 * `data-cite-idx` explicitly allowed so hover handling can find the badge again.
 */
export function injectCitationMarkers(
  text: string,
  ranges: CitationRange[],
): { html: string; badges: CitationRange[] } {
  if (!ranges.length || !text) return { html: text, badges: [] }

  const usable = ranges.filter((range) => range.end <= text.length && !insideCode(text, range.end))
  if (!usable.length) return { html: text, badges: [] }

  let output = ''
  let cursor = 0
  const seen = new Set<number>()
  const badges: CitationRange[] = []

  for (const range of usable) {
    if (range.end < cursor) continue
    output += text.slice(cursor, range.end)
    // Several spans can cite the same URL; only the first gets a badge so the
    // numbering stays stable and the next one does not jump.
    if (!seen.has(range.ordinal)) {
      seen.add(range.ordinal)
      badges.push(range)
      output += `<sup class="wb-cite" data-cite-idx="${badges.length - 1}">${range.ordinal}</sup>`
    }
    cursor = range.end
  }
  output += text.slice(cursor)
  return { html: output, badges }
}

/**
 * Re-base citations onto one text block.
 *
 * Offsets are relative to the whole answer, but a message can hold several text
 * blocks (thinking, tools and text interleave), so each block only receives the
 * spans that overlap it, shifted to block-local offsets.
 */
export function sliceCitations(
  annotations: UrlCitation[] | undefined,
  start: number,
  length: number,
): UrlCitation[] {
  if (!Array.isArray(annotations) || length <= 0) return []
  const end = start + length
  const sliced: UrlCitation[] = []
  for (const annotation of annotations) {
    const citation = annotation?.url_citation
    if (!citation) continue
    const from = Number.isFinite(citation.start_index) ? Number(citation.start_index) : 0
    const to = Number.isFinite(citation.end_index) ? Number(citation.end_index) : from
    if (to <= start || from >= end) continue
    const localStart = Math.max(0, from - start)
    const localEnd = Math.min(length, to - start)
    // A span clipped to nothing carries no information and would render a
    // stray badge, so it is dropped rather than kept as a zero-length range.
    if (localEnd <= localStart) continue
    sliced.push({
      ...annotation,
      url_citation: {
        ...citation,
        start_index: localStart,
        end_index: localEnd,
      },
    })
  }
  return sliced
}

/** Distinct sources across every server tool round, in first-seen order. */export function collectSources(rounds: ServerToolRound[] | undefined): ServerToolSource[] {
  if (!Array.isArray(rounds)) return []
  const seen = new Set<string>()
  const sources: ServerToolSource[] = []
  for (const round of rounds) {
    for (const call of round?.calls ?? []) {
      for (const source of call?.sources ?? []) {
        const url = asString(source?.url)
        if (!url || seen.has(url)) continue
        seen.add(url)
        sources.push(source)
      }
    }
  }
  return sources
}

/** The queries the model actually ran, deduplicated, for the panel header. */
export function collectQueries(rounds: ServerToolRound[] | undefined): string[] {
  if (!Array.isArray(rounds)) return []
  const seen = new Set<string>()
  const queries: string[] = []
  for (const round of rounds) {
    for (const call of round?.calls ?? []) {
      for (const query of call?.action?.queries ?? []) {
        if (!query || seen.has(query)) continue
        seen.add(query)
        queries.push(query)
      }
    }
  }
  return queries
}

/** `example.com` — enough to tell sources apart in a dense list. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}
