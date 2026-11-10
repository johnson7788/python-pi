import { useEffect, useState } from 'react'
import { prettyJson } from '../lib/markdown'
import { isImageBlock } from '../lib/protocol'
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
  const [argsOpen, setArgsOpen] = useState(false)

  // A running tool should reveal its output automatically, but only once: after
  // that the user's own toggle wins. Errors open too; seeing the failure is the
  // reason the row exists.
  useEffect(() => {
    if (status === 'running' && (execution?.partialResult || execution?.result)) setOutputOpen(true)
    if (isError) setOutputOpen(true)
  }, [status, execution?.partialResult, execution?.result, isError])

  const argsText =
    args !== undefined && args !== null
      ? prettyJson(args)
      : argumentsRaw
        ? prettyJson(argumentsRaw)
        : ''

  const resultText =
    toolResultText(execution?.result) || toolResultText(execution?.partialResult) || fallbackText || ''
  const resultContent = execution?.result?.content ?? execution?.partialResult?.content
  const resultImages = Array.isArray(resultContent)
    ? resultContent.filter(isImageBlock).filter((block) => Boolean(block.data))
    : []

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
          <button
            type="button"
            className="tool-card__toggle"
            onClick={() => setArgsOpen((open) => !open)}
            aria-expanded={argsOpen}
          >
            {argsOpen ? '▾' : '▸'} 参数
          </button>
          {argsOpen ? <pre className="tool-card__pre">{argsText}</pre> : null}
        </div>
      ) : null}

      {resultImages.length > 0 ? (
        <div className="tool-card__images">
          {resultImages.map((image, index) => (
            <img
              key={index}
              src={`data:${image.mimeType || 'image/png'};base64,${image.data ?? ''}`}
              alt={`工具输出图片 ${index + 1}`}
            />
          ))}
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
