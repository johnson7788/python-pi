import { useEffect, useRef } from 'react'
import type { Toast } from '../lib/useAgent'

interface ToastsProps {
  toasts: Toast[]
  onDismiss: (id: string) => void
}

/** Notifications wear the colour of their type; errors stay until dismissed. */
const LEVEL_CLASS: Record<string, string> = {
  info: 'toast--info',
  success: 'toast--success',
  warning: 'toast--warning',
  error: 'toast--error',
}

export function Toasts({ toasts, onDismiss }: ToastsProps) {
  // The callback identity changes on every parent render, so it is held in a ref:
  // otherwise each streaming delta would restart every toast's timer.
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  const ids = toasts.map((toast) => `${toast.id}:${toast.level}`).join('|')

  useEffect(() => {
    if (toasts.length === 0) return
    const timers = toasts
      .filter((toast) => (toast.level ?? 'info') !== 'error')
      .map((toast) => window.setTimeout(() => dismissRef.current(toast.id), 6000))
    return () => {
      for (const timer of timers) window.clearTimeout(timer)
    }
    // Keyed on the toast set only, so a re-render does not restart the timers.
  }, [ids, toasts])

  if (toasts.length === 0) return null

  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast ${LEVEL_CLASS[toast.level] ?? 'toast--info'}`}>
          <span className="toast__level">{toast.level}</span>
          <span className="toast__message">{toast.message}</span>
          <button type="button" className="toast__close" onClick={() => onDismiss(toast.id)} aria-label="关闭">
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
