import { Fragment, type ReactNode } from 'react'

/**
 * Minimal dependency-free markdown for chat bubbles: paragraphs, ul/ol,
 * headings, **bold**, *italic*, `inline code`, and [text](href) links. Tolerates
 * partial (streaming) input — anything unrecognized renders as plain text.
 */

const INLINE_RE = /(\[([^\]]+)\]\(([^)\s]+)\)|`[^`\n]+`|\*\*[^*\n]+?\*\*|\*[^*\n]+?\*)/g

type RenderLink = (href: string, text: string) => ReactNode

function renderInline(text: string, renderLink?: RenderLink): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  for (const m of text.matchAll(INLINE_RE)) {
    const i = m.index ?? 0
    if (i > last) out.push(text.slice(last, i))
    const tok = m[0] ?? ''
    if (tok.startsWith('[')) {
      const linkText = m[2] ?? ''
      const href = m[3] ?? ''
      out.push(
        renderLink ? (
          <Fragment key={key++}>{renderLink(href, linkText)}</Fragment>
        ) : (
          <a key={key++} href={href} target="_blank" rel="noreferrer">
            {linkText}
          </a>
        ),
      )
    } else if (tok.startsWith('`')) {
      out.push(<code key={key++}>{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('**')) {
      out.push(<strong key={key++}>{tok.slice(2, -2)}</strong>)
    } else {
      out.push(<em key={key++}>{tok.slice(1, -1)}</em>)
    }
    last = i + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

type MdBlock =
  | { kind: 'p'; lines: string[] }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'h'; text: string }

function parseBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = []
  let cur: MdBlock | null = null
  const flush = (): void => {
    if (cur) {
      blocks.push(cur)
      cur = null
    }
  }
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd()
    if (!line.trim()) {
      flush()
      continue
    }
    const h = /^#{1,6}\s+(.*)$/.exec(line)
    if (h) {
      flush()
      blocks.push({ kind: 'h', text: h[1] ?? '' })
      continue
    }
    const ul = /^\s*[-*•]\s+(.*)$/.exec(line)
    if (ul) {
      if (cur?.kind !== 'ul') {
        flush()
        cur = { kind: 'ul', items: [] }
      }
      cur.items.push(ul[1] ?? '')
      continue
    }
    const ol = /^\s*\d+[.、)]\s+(.*)$/.exec(line)
    if (ol) {
      if (cur?.kind !== 'ol') {
        flush()
        cur = { kind: 'ol', items: [] }
      }
      cur.items.push(ol[1] ?? '')
      continue
    }
    if (cur?.kind !== 'p') {
      flush()
      cur = { kind: 'p', lines: [] }
    }
    cur.lines.push(line)
  }
  flush()
  return blocks
}

export function Markdown({
  text,
  renderLink,
}: {
  text: string
  renderLink?: RenderLink
}): React.JSX.Element {
  return (
    <div className="ai-md">
      {parseBlocks(text).map((b, i) => {
        if (b.kind === 'h') {
          return (
            <p key={i} className="ai-md-h">
              {renderInline(b.text, renderLink)}
            </p>
          )
        }
        if (b.kind === 'ul' || b.kind === 'ol') {
          const items = b.items.map((it, j) => (
            <li key={j}>{renderInline(it, renderLink)}</li>
          ))
          return b.kind === 'ul' ? <ul key={i}>{items}</ul> : <ol key={i}>{items}</ol>
        }
        return (
          <p key={i}>
            {b.lines.map((ln, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {renderInline(ln, renderLink)}
              </Fragment>
            ))}
          </p>
        )
      })}
    </div>
  )
}