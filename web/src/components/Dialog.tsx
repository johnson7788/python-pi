import { useEffect, useRef, useState } from 'react'
import type { ExtensionDialog } from '../lib/useAgent'

interface DialogHostProps {
  dialogs: ExtensionDialog[]
  onRespond: (id: string, response: { value?: string; confirmed?: boolean; cancelled?: true }) => void
}

/**
 * Extension UI dialogs.
 *
 * Rendered as an overlay that never blocks the transcript or the composer: the
 * agent is parked waiting for the answer, so the user must be able to read the
 * conversation (and even answer while a turn streams).
 */
function DialogCard({
  dialog,
  onRespond,
}: {
  dialog: ExtensionDialog
  onRespond: DialogHostProps['onRespond']
}) {
  const [value, setValue] = useState(dialog.prefill ?? '')
  const inputRef = useRef<HTMLInputElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    // Focus the primary control so the dialog is answerable from the keyboard.
    inputRef.current?.focus()
    textareaRef.current?.focus()
  }, [])

  const title = dialog.title || '扩展请求'

  return (
    <div className="dialog" role="dialog" aria-modal="false" aria-label={title}>
      <div className="dialog__title">{title}</div>
      {dialog.message ? <div className="dialog__message">{dialog.message}</div> : null}

      {dialog.method === 'select' ? (
        <div className="dialog__options">
          {(dialog.options ?? []).map((option) => (
            <button
              type="button"
              key={option}
              className="dialog__option"
              onClick={() => onRespond(dialog.id, { value: option })}
            >
              {option}
            </button>
          ))}
        </div>
      ) : null}

      {dialog.method === 'confirm' ? (
        <div className="dialog__actions">
          <button type="button" className="button" onClick={() => onRespond(dialog.id, { cancelled: true })}>
            取消
          </button>
          <button
            type="button"
            className="button button--primary"
            onClick={() => onRespond(dialog.id, { confirmed: true })}
          >
            确定
          </button>
        </div>
      ) : null}

      {dialog.method === 'input' ? (
        <div className="dialog__field">
          <input
            ref={inputRef}
            className="input"
            value={value}
            placeholder={dialog.placeholder ?? ''}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                onRespond(dialog.id, { value })
              }
            }}
          />
        </div>
      ) : null}

      {dialog.method === 'editor' ? (
        <div className="dialog__field">
          <textarea
            ref={textareaRef}
            className="input dialog__editor"
            rows={8}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </div>
      ) : null}

      {dialog.method === 'input' || dialog.method === 'editor' ? (
        <div className="dialog__actions">
          <button type="button" className="button" onClick={() => onRespond(dialog.id, { cancelled: true })}>
            取消
          </button>
          <button type="button" className="button button--primary" onClick={() => onRespond(dialog.id, { value })}>
            确定
          </button>
        </div>
      ) : null}

      {dialog.method === 'select' ? (
        <div className="dialog__actions">
          <button type="button" className="button" onClick={() => onRespond(dialog.id, { cancelled: true })}>
            取消
          </button>
        </div>
      ) : null}
    </div>
  )
}

export function DialogHost({ dialogs, onRespond }: DialogHostProps) {
  if (dialogs.length === 0) return null
  return (
    <div className="dialog-host">
      {dialogs.length > 1 ? <div className="dialog-host__count">还有 {dialogs.length - 1} 个待处理请求</div> : null}
      {dialogs.map((dialog) => (
        <DialogCard key={dialog.id} dialog={dialog} onRespond={onRespond} />
      ))}
    </div>
  )
}
