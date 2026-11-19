import { useMemo, useState } from 'react'
import {
  collectQueries,
  collectSources,
  hostOf,
  injectCitationMarkers,
  toCitationRanges,
  type CitationRange,
} from '../lib/citations'
import { renderMarkdown } from '../lib/markdown'
import type { ServerToolRound, UrlCitation } from '../lib/protocol'

/**
 * Answer text with inline citation badges.
 *
 * Badges are injected into the markdown *source* (see `injectCitationMarkers`)
 * rather than rendered as React children, because the text arrives as one
 * string and splitting it at offsets would break inline markdown.  The tradeoff
 * is that hover handling has to find the badge in the sanitized HTML by its
 * `data-cite-idx` attribute.
 */
export function CitedText({
  text,
  citations,
}: {
  text: string
  citations?: UrlCitation[]
}) {
  const { html, badges } = useMemo(() => {
    const ranges = toCitationRanges(citations)
    const injected = injectCitationMarkers(text, ranges)
    return { html: renderMarkdown(injected.html), badges: injected.badges }
  }, [text, citations])

  const [hovered, setHovered] = useState<{ badge: CitationRange; x: number; y: number } | null>(null)

  if (!badges.length) {
    return <div className="wb-md" dangerouslySetInnerHTML={{ __html: html }} />
  }

  return (
    <div className="wb-cited">
      <div
        className="wb-md"
        dangerouslySetInnerHTML={{ __html: html }}
        onMouseOver={(event) => {
          const target = event.target as HTMLElement | null
          const badge = target?.closest?.('[data-cite-idx]') as HTMLElement | null
          if (!badge) return
          const index = Number(badge.dataset.citeIdx)
          const citation = badges[index]
          if (!citation) return
          const rect = badge.getBoundingClientRect()
          setHovered({ badge: citation, x: rect.left, y: rect.bottom })
        }}
        onMouseOut={(event) => {
          const target = event.target as HTMLElement | null
          if (target?.closest?.('[data-cite-idx]')) setHovered(null)
        }}
      />
      {hovered ? (
        <div className="wb-cite-card" style={{ left: hovered.x, top: hovered.y }}>
          <span className="wb-cite-card__ordinal">{hovered.badge.ordinal}</span>
          <div className="wb-cite-card__body">
            <b>{hovered.badge.title ?? hostOf(hovered.badge.url)}</b>
            <span className="wb-cite-card__host">{hostOf(hovered.badge.url)}</span>
            <a href={hovered.badge.url} target="_blank" rel="noreferrer noopener">
              {hovered.badge.url}
            </a>
          </div>
        </div>
      ) : null}
    </div>
  )
}

/**
 * Sources behind a server-side search.
 *
 * Collapsed by default: the answer is the point, and a search can return dozens
 * of hits.  Nothing here was executed locally — these calls already ran on the
 * server — so the panel is read-only.
 */
export function SourcesPanel({ rounds }: { rounds?: ServerToolRound[] }) {
  const sources = useMemo(() => collectSources(rounds), [rounds])
  const queries = useMemo(() => collectQueries(rounds), [rounds])
  const calls = useMemo(
    () => (rounds ?? []).reduce((total, round) => total + (round.calls?.length ?? 0), 0),
    [rounds],
  )

  if (!sources.length && !queries.length) return null

  return (
    <details className="wb-sources">
      <summary className="wb-sources__summary">
        <span className="wb-sources__glyph" aria-hidden="true">
          ◎
        </span>
        联网来源
        <span className="wb-sources__count">
          {calls > 0 ? `${calls} 次调用` : ''}
          {sources.length > 0 ? ` · ${sources.length} 条` : ''}
        </span>
      </summary>
      {queries.length > 0 ? (
        <div className="wb-sources__queries">
          {queries.map((query) => (
            <span key={query} className="wb-sources__query">
              {query}
            </span>
          ))}
        </div>
      ) : null}
      {sources.length > 0 ? (
        <ul className="wb-sources__list">
          {sources.map((source, index) => (
            <li key={`${source.url}-${index}`} className="wb-sources__item">
              <a
                className="wb-sources__title"
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
              >
                {source.title || hostOf(source.url || '')}
              </a>
              <span className="wb-sources__host">{hostOf(source.url || '')}</span>
              {source.snippet ? <p className="wb-sources__snippet">{source.snippet}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  )
}
