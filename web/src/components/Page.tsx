/**
 * Small shared pieces every non-chat page needs.
 *
 * The chat view is hand-built around the transcript, but the settings-style pages are all
 * "a titled card holding a table, a form or nothing at all", so those four shapes live
 * here instead of being re-invented per page.
 */

import type { ReactNode } from 'react'

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page__head">
      <div>
        <h1 className="page__title">{title}</h1>
        {description ? <p className="page__desc">{description}</p> : null}
      </div>
      {actions ? <div className="page__actions">{actions}</div> : null}
    </header>
  )
}

export function Card({
  title,
  actions,
  children,
  scroll = false,
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  scroll?: boolean
}) {
  return (
    <section className="card">
      {title || actions ? (
        <div className="card__head">
          {title ? <h2 className="card__title">{title}</h2> : <span />}
          {actions ? <div className="card__actions">{actions}</div> : null}
        </div>
      ) : null}
      <div className={scroll ? 'card__body card__body--scroll' : 'card__body'}>{children}</div>
    </section>
  )
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="note note--error" role="alert">
      {message}
    </div>
  )
}

export function NoticeNote({ message }: { message: string | null }) {
  if (!message) return null
  return <div className="note note--ok">{message}</div>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  )
}

/** `1234` → `1,234`; `null` means "no limit" and is rendered as such by the caller. */
export function formatCount(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'
}

export function formatCost(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—'
  return value === 0 ? '0' : value.toFixed(4)
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
