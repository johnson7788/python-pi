import { useEffect, useRef, useState } from 'react'

/**
 * In-app replacement for `window.prompt` / `window.confirm`.
 *
 * The native dialogs are unstyleable, block the whole browser tab, and look nothing
 * like the rest of the workbench, so every prompt in the app goes through one host
 * that renders the same modal the other workbench dialogs use.
 *
 * Usage is still imperative, which keeps the call sites short:
 *
 *   const name = (await askText({ title: '新建工作区', label: '工作区名称' }))?.trim()
 *   if (!(await askConfirm({ title: '删除应用', message: `删除「${name}」？`, danger: true }))) return
 */

export interface TextPromptOptions {
  title: string
  label: string
  placeholder?: string
  confirmText?: string
}

export interface ConfirmOptions {
  title: string
  message: string
  confirmText?: string
  danger?: boolean
}

interface PendingRequest {
  kind: 'text' | 'confirm'
  title: string
  label?: string
  message?: string
  placeholder?: string
  confirmText?: string
  danger?: boolean
  resolve: (value: string | boolean | null) => void
}

let present: ((request: PendingRequest) => void) | null = null

/** Ask for one line of text; resolves `null` when the user cancels. */
export function askText(options: TextPromptOptions): Promise<string | null> {
  return new Promise((resolve) => {
    if (!present) {
      resolve(null)
      return
    }
    present({
      kind: 'text',
      ...options,
      resolve: (value) => resolve(typeof value === 'string' ? value : null),
    })
  })
}

/** Ask for confirmation; resolves `false` when the user cancels. */
export function askConfirm(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!present) {
      resolve(false)
      return
    }
    present({
      kind: 'confirm',
      ...options,
      resolve: (value) => resolve(value === true),
    })
  })
}

/** Mount once; every `ask*` call renders through it. */
export function PromptHost() {
  const [request, setRequest] = useState<PendingRequest | null>(null)
  const [value, setValue] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    present = (next) => {
      setValue('')
      setRequest(next)
    }
    return () => {
      present = null
    }
  }, [])

  useEffect(() => {
    if (request?.kind === 'text') inputRef.current?.focus()
  }, [request])

  useEffect(() => {
    if (!request) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      request.resolve(null)
      setRequest(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [request])

  if (!request) return null

  const close = (result: string | boolean | null) => {
    request.resolve(result)
    setRequest(null)
  }

  const confirm = () => close(request.kind === 'text' ? value.trim() : true)

  return (
    <div className="wb-modal-scrim" onClick={() => close(null)}>
      <div
        className="wb-modal wb-modal-sm"
        role="dialog"
        aria-modal="true"
        aria-label={request.title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="wb-modal-head">
          <b>{request.title}</b>
        </header>
        <div className="wb-modal-body">
          {request.kind === 'text' ? (
            <label className="wb-field">
              <span>{request.label}</span>
              <input
                ref={inputRef}
                value={value}
                placeholder={request.placeholder}
                onChange={(event) => setValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.nativeEvent.isComposing) confirm()
                }}
              />
            </label>
          ) : (
            <p className="wb-modal-text">{request.message}</p>
          )}
        </div>
        <footer className="wb-modal-foot is-end">
          <button type="button" className="wb-button" onClick={() => close(null)}>
            取消
          </button>
          <button
            type="button"
            className={`wb-button ${request.danger ? 'wb-button--danger' : 'wb-button--primary'}`}
            disabled={request.kind === 'text' && value.trim().length === 0}
            onClick={confirm}
          >
            {request.confirmText ?? '确定'}
          </button>
        </footer>
      </div>
    </div>
  )
}
