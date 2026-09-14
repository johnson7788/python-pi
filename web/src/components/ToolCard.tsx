import { useEffect, useState } from 'react'
import { prettyJson } from '../lib/markdown'
import type { ToolExecution } from '../lib/useAgent'
import { toolResultText } from '../lib/useAgent'

interface ToolCardProps {
  toolCallId: string
  toolName: string
  /** Parsed arguments when known (preferred), otherwise the raw streamed JSON. */
  args?: unknown
  argumentsRaw?: string
  execution?: ToolExecution
  /** Set for a standalone toolResult message that has no inline tool call. */
  fallbackText?: string
  fallbackIsError?: boolean
}

const STATUS_LABEL: Record<ToolExecution['status'], string> = {
  running: '运行中',
  done: '完成',
  error: '失败',
}

export function ToolCard({
  toolCallId,
  toolName,
  args,
  argumentsRaw,
  execution,
  fallbackText,
  fallbackIsError,
}: ToolCardProps) {
  const status = execution?.status ?? (fallbackText !== undefined ? 'done' : 'running')
  const isError = execution?.isError ?? Boolean(fallbackIsError)

  // Streamed output can be long; collapsing keeps the transcript scannable.
  const [outputOpen, setOutputOpen] = useState(false)

  // A running tool should reveal its output automatically, but only once: after
  // that the user's own toggle wins.
  useEffect(() => {
    if (status === 'running' && (execution?.partialResult || execution?.result)) setOutputOpen(true)
  }, [status, execution?.partialResult, execution?.result])

  const argsText =
    args !== undefined && args !== null
      ? prettyJson(args)
      : argumentsRaw
        ? prettyJson(argumentsRaw)
        : ''

  const resultText =
    toolResultText(execution?.result) || toolResultText(execution?.partialResult) || fallbackText || ''

  const effectiveStatus = isError ? 'error' : status

  return (
    <div className={`tool-card tool-card--${effectiveStatus}`} data-tool-call-id={toolCallId}>
      <div className="tool-card__head">
        <span className="tool-card__glyph" aria-hidden="true">
          {effectiveStatus === 'running' ? <span className="spinner" /> : effectiveStatus === 'error' ? '✗' : '✓'}
        </span>
        <span className="tool-card__name">{toolName || '工具调用'}</span>
        <span className="tool-card__status">{isError ? STATUS_LABEL.error : STATUS_LABEL[status]}</span>
      </div>

      {argsText ? (
        <div className="tool-card__section">
          <div className="tool-card__label">参数</div>
          <pre className="tool-card__pre">{argsText}</pre>
        </div>
      ) : null}

      {resultText ? (
        <div className="tool-card__section">
          <button
            type="button"
            className="tool-card__toggle"
            onClick={() => setOutputOpen((open) => !open)}
            aria-expanded={outputOpen}
          >
            {outputOpen ? '▾' : '▸'} 结果
            {status === 'running' ? '（实时）' : ''}
          </button>
          {outputOpen ? <pre className="tool-card__pre">{resultText}</pre> : null}
        </div>
      ) : effectiveStatus === 'running' ? (
        <div className="tool-card__section tool-card__hint">等待输出…</div>
      ) : null}
    </div>
  )
}
