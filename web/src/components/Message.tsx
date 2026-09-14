import type { AssistantMessage, ContentBlock, Message, Usage } from '../lib/protocol'
import { asRecord, isTextBlock, isThinkingBlock, isToolCallBlock } from '../lib/protocol'
import { renderMarkdown } from '../lib/markdown'
import type { ToolExecution } from '../lib/useAgent'
import { ToolCard } from './ToolCard'

interface MessageViewProps {
  message: Message
  streaming: boolean
  toolExecutions: Record<string, ToolExecution>
}

function formatTime(timestamp: number | undefined): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour12: false })
}

function formatUsage(usage: Usage | undefined): string {
  if (!usage) return ''
  const tokens = usage.totalTokens ?? (usage.input ?? 0) + (usage.output ?? 0)
  const parts = [`${tokens} tokens`]
  if (typeof usage.input === 'number') parts.push(`↑${usage.input}`)
  if (typeof usage.output === 'number') parts.push(`↓${usage.output}`)
  const cost = usage.cost?.total
  if (typeof cost === 'number' && cost > 0) parts.push(`$${cost.toFixed(4)}`)
  return parts.join(' · ')
}

/** One assistant block: prose, reasoning, or a tool call card. */
function AssistantBlock({
  block,
  toolExecutions,
}: {
  block: ContentBlock
  toolExecutions: Record<string, ToolExecution>
}) {
  if (isTextBlock(block)) {
    if (!block.text.trim()) return null
    // The HTML is sanitized in renderMarkdown before it reaches the DOM.
    return <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(block.text) }} />
  }

  if (isThinkingBlock(block)) {
    // Reasoning is collapsed by default: it is useful, but it is not the answer.
    return (
      <details className="thinking">
        <summary>思考过程</summary>
        <div className="thinking__body">{block.thinking}</div>
      </details>
    )
  }

  if (isToolCallBlock(block)) {
    return (
      <ToolCard
        toolCallId={block.id}
        toolName={block.name}
        args={block.arguments}
        argumentsRaw={block.argumentsRaw}
        execution={toolExecutions[block.id]}
      />
    )
  }

  const loose = asRecord(block)
  return <pre className="block-unknown">{JSON.stringify(loose, null, 2)}</pre>
}

export function MessageView({ message, streaming, toolExecutions }: MessageViewProps) {
  const time = formatTime(message.timestamp)

  if (message.role === 'user') {
    const text =
      typeof message.content === 'string'
        ? message.content
        : message.content
            .filter(isTextBlock)
            .map((part) => part.text)
            .join('\n')
    return (
      <article className="message message--user">
        <div className="message__meta">
          <span>你</span>
          {time ? <span className="message__time">{time}</span> : null}
        </div>
        <div className="message__user-body">{text}</div>
      </article>
    )
  }

  if (message.role === 'assistant') {
    const assistant = message as AssistantMessage
    const usageText = formatUsage(assistant.usage)
    return (
      <article className={`message message--assistant${streaming ? ' message--streaming' : ''}`}>
        <div className="message__meta">
          <span>助手</span>
          {time ? <span className="message__time">{time}</span> : null}
        </div>
        <div className="message__body">
          {assistant.content.map((block, index) => (
            <AssistantBlock key={`${block.type}-${index}`} block={block} toolExecutions={toolExecutions} />
          ))}
          {assistant.content.length === 0 && streaming ? <span className="caret" /> : null}
        </div>
        {assistant.errorMessage ? <div className="message__error">{assistant.errorMessage}</div> : null}
        {usageText ? <div className="message__usage">{usageText}</div> : null}
      </article>
    )
  }

  if (message.role === 'toolResult') {
    // Reached only when the matching tool call is not in the transcript.
    return (
      <article className="message message--tool">
        <ToolCard
          toolCallId={message.toolCallId}
          toolName={message.toolName ?? '工具'}
          execution={toolExecutions[message.toolCallId]}
          fallbackText={message.content.filter(isTextBlock).map((part) => part.text).join('\n')}
          fallbackIsError={message.isError}
        />
      </article>
    )
  }

  const loose = asRecord(message)
  const summary = typeof loose.summary === 'string' ? loose.summary : ''
  const command = typeof loose.command === 'string' ? loose.command : ''
  const output = typeof loose.output === 'string' ? loose.output : ''
  return (
    <article className="message message--system">
      <div className="message__meta">
        <span>{message.role === 'bashExecution' ? '命令执行' : '系统记录'}</span>
        <span className="message__time">{message.role}</span>
      </div>
      {command ? <pre className="block-unknown">$ {command}</pre> : null}
      {output ? <pre className="block-unknown">{output}</pre> : null}
      {summary ? <div className="md">{summary}</div> : null}
    </article>
  )
}
