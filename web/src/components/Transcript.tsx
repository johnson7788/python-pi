import { useEffect, useMemo, useRef } from 'react'
import { isToolCallBlock } from '../lib/protocol'
import type { ToolExecution, TranscriptEntry } from '../lib/useAgent'
import { MessageView } from './Message'

interface TranscriptProps {
  transcript: TranscriptEntry[]
  toolExecutions: Record<string, ToolExecution>
  streaming: boolean
}

export function Transcript({ transcript, toolExecutions, streaming }: TranscriptProps) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)

  // Tool results are rendered inside their tool call card, so a standalone
  // toolResult entry is dropped whenever that card exists.
  const inlineToolCallIds = useMemo(() => {
    const ids = new Set<string>()
    for (const entry of transcript) {
      if (entry.message.role !== 'assistant') continue
      for (const block of entry.message.content) {
        if (isToolCallBlock(block) && block.id) ids.add(block.id)
      }
    }
    return ids
  }, [transcript])

  const visible = useMemo(
    () =>
      transcript.filter(
        (entry) =>
          !(entry.message.role === 'toolResult' && inlineToolCallIds.has(entry.message.toolCallId)),
      ),
    [transcript, inlineToolCallIds],
  )

  // Follow the stream only while the user is already at the bottom; scrolling up
  // to read must not be yanked back by every delta.
  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller || !pinnedRef.current) return
    scroller.scrollTop = scroller.scrollHeight
  }, [visible, toolExecutions])

  const onScroll = () => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
    pinnedRef.current = distance < 80
  }

  return (
    <div className="transcript" ref={scrollerRef} onScroll={onScroll}>
      {visible.length === 0 ? (
        <div className="transcript__empty">
          <p>还没有对话。</p>
          <p className="transcript__empty-hint">在下方输入消息，或输入 / 选择斜杠命令开始。</p>
        </div>
      ) : (
        <div className="transcript__inner">
          {visible.map((entry) => (
            <MessageView
              key={entry.key}
              message={entry.message}
              streaming={entry.streaming && streaming}
              toolExecutions={toolExecutions}
            />
          ))}
        </div>
      )}
    </div>
  )
}
