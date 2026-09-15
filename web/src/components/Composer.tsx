import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import type { CommandInfo, StreamingBehavior } from '../lib/protocol'

interface ComposerProps {
  streaming: boolean
  commands: CommandInfo[]
  onSend: (text: string, behavior?: StreamingBehavior) => void
  onAbort: () => void
  /** Text pushed in by an extension through `set_editor_text`. */
  injectedText: string | null
  onInjectedTextConsumed: () => void
  /** Shown when the socket is down so the user knows why send is disabled. */
  disabled: boolean
}

const SOURCE_LABEL: Record<string, string> = {
  prompt: '模板',
  skill: '技能',
  extension: '扩展',
}

export function Composer({
  streaming,
  commands,
  onSend,
  onAbort,
  injectedText,
  onInjectedTextConsumed,
  disabled,
}: ComposerProps) {
  const [draft, setDraft] = useState('')
  const [behavior, setBehavior] = useState<StreamingBehavior>('steer')
  const [highlighted, setHighlighted] = useState(0)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // An extension can replace the editor contents; consume the one-shot value so
  // re-renders do not keep resetting what the user is typing.
  useEffect(() => {
    if (injectedText === null) return
    setDraft(injectedText)
    onInjectedTextConsumed()
  }, [injectedText, onInjectedTextConsumed])

  // Auto-grow: reset to auto first, otherwise the box can only ever get taller.
  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 260)}px`
  }, [draft])

  // The popup only opens for a slash token at the very start of the message,
  // which is the only place pi treats it as a command.
  const slashMatch = /^\/([^\s/]*)$/.exec(draft)
  const suggestions = slashMatch
    ? commands.filter((command) => command.name.toLowerCase().startsWith(slashMatch[1].toLowerCase()))
    : []
  const popupOpen = suggestions.length > 0
  // Guard against a stale highlight when the filtered list shrinks.
  const activeIndex = popupOpen ? Math.min(highlighted, suggestions.length - 1) : 0

  useEffect(() => {
    setHighlighted(0)
  }, [draft])

  const applySuggestion = (command: CommandInfo) => {
    setDraft(`/${command.name} `)
    textareaRef.current?.focus()
  }

  const submit = () => {
    const text = draft.trim()
    if (!text) return
    // While streaming the server needs to know whether this steers the running
    // turn or queues a follow-up; the choice is surfaced in the toolbar.
    onSend(text, streaming ? behavior : undefined)
    setDraft('')
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (popupOpen) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setHighlighted((index) => (index + 1) % suggestions.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setHighlighted((index) => (index - 1 + suggestions.length) % suggestions.length)
        return
      }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
        event.preventDefault()
        applySuggestion(suggestions[activeIndex])
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setDraft('')
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="composer">
      {popupOpen ? (
        <div className="slash-popup" role="listbox" aria-label="斜杠命令">
          {suggestions.map((command, index) => (
            <button
              type="button"
              key={command.name}
              role="option"
              aria-selected={index === activeIndex}
              className={`slash-popup__item${index === activeIndex ? ' is-active' : ''}`}
              onMouseEnter={() => setHighlighted(index)}
              onClick={() => applySuggestion(command)}
            >
              <span className="slash-popup__name">/{command.name}</span>
              <span className="slash-popup__source">
                {SOURCE_LABEL[command.source ?? ''] ?? command.source ?? ''}
              </span>
              <span className="slash-popup__desc">{command.description ?? ''}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="composer__box">
        <textarea
          ref={textareaRef}
          className="composer__input"
          value={draft}
          rows={1}
          placeholder={streaming ? '输入消息（将按下方方式处理）…' : '输入消息，Enter 发送，Shift+Enter 换行'}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
        />

        <div className="composer__toolbar">
          {streaming ? (
            <div className="segmented" role="group" aria-label="流式发送方式">
              <button
                type="button"
                className={behavior === 'steer' ? 'is-active' : ''}
                onClick={() => setBehavior('steer')}
                title="打断当前回合，立刻把手上的消息交给模型"
              >
                插入引导
              </button>
              <button
                type="button"
                className={behavior === 'followUp' ? 'is-active' : ''}
                onClick={() => setBehavior('followUp')}
                title="等当前回合结束后再处理这条消息"
              >
                排队跟进
              </button>
            </div>
          ) : (
            <span className="composer__hint">Enter 发送 · Shift+Enter 换行 · / 唤起命令</span>
          )}

          <div className="composer__actions">
            {streaming ? <span className="composer__streaming">正在生成…</span> : null}
            {/* 一个圆钮承担发送/停止两件事：流式时它变成停止键。 */}
            <button
              type="button"
              className={`button button--primary button--send${streaming ? ' is-stop' : ''}`}
              onClick={streaming ? onAbort : submit}
              disabled={!streaming && (disabled || !draft.trim())}
              aria-label={streaming ? '停止生成' : '发送'}
              title={streaming ? '停止生成' : '发送'}
            >
              {streaming ? (
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="4.5" y="4.5" width="7" height="7" rx="1.6" fill="currentColor" stroke="none" />
                </svg>
              ) : (
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M8 13V3.6" />
                  <path d="M4 7.4 8 3.4l4 4" />
                </svg>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
