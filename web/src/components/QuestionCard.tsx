import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PendingQuestion, QuestionAnswer, QuestionItem } from '../lib/protocol'

/**
 * The question card takes over the composer instead of floating above it.
 *
 * A floating dialog can be missed, covered, or dismissed by accident; the agent
 * is parked while this card exists, so it gets the one screen region the user is
 * already looking at.  The transcript stays readable above it.
 *
 * Interaction rules (deliberately the same shape as DeepSeek Harness'):
 *  - one question visible at a time, `1 / N` with arrows, because four questions
 *    in one column stops being scannable;
 *  - a single-select choice auto-advances; multi-select and free text need an
 *    explicit "next";
 *  - "skip" is an answer meaning "decide without me", never permission;
 *  - unfinished input is kept per session in localStorage, so navigating away
 *    does not throw away what was typed.
 */

interface Draft {
  selected: string[]
  custom: string
  skipped: boolean
}

const EMPTY_DRAFT: Draft = { selected: [], custom: '', skipped: false }

interface QuestionCardProps {
  question: PendingQuestion
  sessionId?: string
  onAnswer: (callId: string, answers: QuestionAnswer[], cancelled?: boolean) => void
  onClaim: (callId: string) => void
  onTimeout: (callId: string) => void
  /** Collapse a continued question without sending anything. */
  onCollapse: () => void
}

function draftKey(sessionId: string | undefined, callId: string): string {
  return `pi.question-draft.v1.${sessionId ?? 'session'}.${callId}`
}

function loadDraft(key: string, questions: QuestionItem[]): { index: number; drafts: Draft[] } | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { index?: unknown; drafts?: unknown }
    if (!Array.isArray(parsed.drafts) || parsed.drafts.length !== questions.length) return null
    const drafts = parsed.drafts.map((item) => {
      const loose = (item ?? {}) as Partial<Draft>
      return {
        selected: Array.isArray(loose.selected) ? loose.selected.filter((value) => typeof value === 'string') : [],
        custom: typeof loose.custom === 'string' ? loose.custom : '',
        skipped: Boolean(loose.skipped),
      }
    })
    const index = typeof parsed.index === 'number' ? Math.max(0, Math.min(questions.length - 1, parsed.index)) : 0
    return { index, drafts }
  } catch {
    return null
  }
}

function answered(draft: Draft): boolean {
  return draft.skipped || draft.selected.length > 0 || draft.custom.trim().length > 0
}

function secondsLeft(ms: number | undefined): number | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return null
  return Math.max(0, Math.ceil(ms / 1000))
}

export function QuestionCard({
  question,
  sessionId,
  onAnswer,
  onClaim,
  onTimeout,
  onCollapse,
}: QuestionCardProps) {
  const questions = question.questions
  const key = draftKey(sessionId, question.callId)
  const stored = useMemo(() => loadDraft(key, questions), [key, questions.length])
  const [index, setIndex] = useState(stored?.index ?? 0)
  const [drafts, setDrafts] = useState<Draft[]>(stored?.drafts ?? questions.map(() => ({ ...EMPTY_DRAFT })))
  const [remaining, setRemaining] = useState<number | null>(
    question.state === 'open' ? secondsLeft(question.remainingMs) : null,
  )
  const [expired, setExpired] = useState(false)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const advanceTimer = useRef<number | null>(null)
  // Callbacks are recreated on every store update; keeping the latest one in a
  // ref lets the timer effects depend only on the question, not on identity.
  const claimRef = useRef(onClaim)
  const timeoutRef = useRef(onTimeout)
  claimRef.current = onClaim
  timeoutRef.current = onTimeout

  const current = questions[index]
  const draft = drafts[index] ?? EMPTY_DRAFT
  const multiple = questions.length > 1
  const isLast = index === questions.length - 1

  // Claim the foreground wait so the host stops counting while the user reads.
  useEffect(() => {
    if (question.state === 'open') claimRef.current(question.callId)
  }, [question.callId, question.state])

  // Countdown is the client's clock: the server froze its own deadline on claim.
  // A blocking question (`-1`) has no model-facing deadline, so it gets none here
  // either — its ceiling is a safety net, not something to count down to.
  useEffect(() => {
    if (question.state !== 'open' || question.blocking) {
      setRemaining(null)
      return
    }
    const start = secondsLeft(question.remainingMs)
    setRemaining(start)
    setExpired(false)
    if (start === null) return
    let left = start
    const timer = window.setInterval(() => {
      left -= 1
      setRemaining(Math.max(0, left))
      if (left <= 0) {
        window.clearInterval(timer)
        setExpired(true)
        timeoutRef.current(question.callId)
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [question.callId, question.state, question.remainingMs])

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify({ index, drafts }))
    } catch {
      // Private mode or a full quota: drafts are a convenience, not a contract.
    }
  }, [key, index, drafts])

  useEffect(
    () => () => {
      if (advanceTimer.current !== null) window.clearTimeout(advanceTimer.current)
    },
    [],
  )

  const clearDraft = useCallback(() => {
    try {
      localStorage.removeItem(key)
    } catch {
      // ignore
    }
  }, [key])

  const submit = useCallback(() => {
    const answers: QuestionAnswer[] = questions.map((item, itemIndex) => {
      const value = drafts[itemIndex] ?? EMPTY_DRAFT
      if (value.skipped) return { id: item.id, selected: [] }
      const custom = value.custom.trim()
      return {
        id: item.id,
        selected: value.selected,
        ...(custom ? { custom } : {}),
      }
    })
    clearDraft()
    onAnswer(question.callId, answers)
  }, [clearDraft, drafts, onAnswer, question.callId, questions])

  const goNext = useCallback(() => {
    if (!answered(drafts[index] ?? EMPTY_DRAFT)) return
    if (!isLast) setIndex((value) => Math.min(questions.length - 1, value + 1))
    else submit()
  }, [drafts, index, isLast, questions.length, submit])

  const patch = useCallback(
    (change: Partial<Draft>) => {
      setDrafts((previous) =>
        previous.map((item, itemIndex) => (itemIndex === index ? { ...item, ...change } : item)),
      )
    },
    [index],
  )

  const choose = useCallback(
    (label: string) => {
      const value = drafts[index] ?? EMPTY_DRAFT
      if (current?.multiSelect) {
        const selected = value.selected.includes(label)
          ? value.selected.filter((item) => item !== label)
          : [...value.selected, label]
        patch({ selected, skipped: false })
        return
      }
      patch({ selected: [label], custom: '', skipped: false })
      // Single-select auto-advances (not on the last question, where the user
      // may still want to type a custom answer or submit deliberately).
      if (!isLast) {
        if (advanceTimer.current !== null) window.clearTimeout(advanceTimer.current)
        advanceTimer.current = window.setTimeout(() => setIndex((value2) => value2 + 1), 180)
      }
    },
    [current?.multiSelect, drafts, index, isLast, patch],
  )

  const setCustom = useCallback(
    (value: string) => {
      // For a single-select question the typed answer replaces the choice; for a
      // multi-select one it supplements the labels, which is what the protocol
      // documents.
      patch({ custom: value, ...(current?.multiSelect ? {} : { selected: [] }), skipped: false })
    },
    [current?.multiSelect, patch],
  )

  const skip = useCallback(() => {
    setDrafts((previous) =>
      previous.map((item, itemIndex) => (itemIndex === index ? { ...EMPTY_DRAFT, skipped: true } : item)),
    )
    if (!isLast) setIndex((value) => Math.min(questions.length - 1, value + 1))
    else {
      // The skip must land in the same batch as the other answers.
      const answers: QuestionAnswer[] = questions.map((item, itemIndex) => {
        if (itemIndex === index) return { id: item.id, selected: [] }
        const value = drafts[itemIndex] ?? EMPTY_DRAFT
        if (value.skipped) return { id: item.id, selected: [] }
        const custom = value.custom.trim()
        return { id: item.id, selected: value.selected, ...(custom ? { custom } : {}) }
      })
      clearDraft()
      onAnswer(question.callId, answers)
    }
  }, [clearDraft, drafts, index, isLast, onAnswer, question.callId, questions])

  if (!current) return null

  const cancel = () => {
    clearDraft()
    if (question.state === 'open') onAnswer(question.callId, [], true)
    else onCollapse()
  }

  const header = current.header || '需要你的确认'
  const countdown =
    question.state === 'continued'
      ? '已超时，仍可回答'
      : question.blocking
        ? '等待你的回答'
        : remaining !== null && question.state === 'open'
          ? expired
            ? '已超时，可稍后回答'
            : `约 ${remaining} 秒后自动继续`
          : '等待你的回答'

  return (
    <div className="question-card" role="dialog" aria-label={header} data-call-id={question.callId}>
      <div className="question-card__head">
        <span className="question-card__eyebrow">{header}</span>
        {current.intent ? (
          <span className="question-card__badge">
            {current.intent.kind === 'approval' ? '权限确认' : '方案审阅'}
          </span>
        ) : null}
        <span className="question-card__countdown">{countdown}</span>
        <button type="button" className="question-card__close" onClick={cancel} aria-label="关闭">
          ✕
        </button>
      </div>

      {multiple ? (
        <div className="question-card__pager">
          <span className="question-card__progress">
            {index + 1} / {questions.length}
          </span>
        </div>
      ) : null}

      <div className="question-card__question">{current.question}</div>
      {current.detail ? <div className="question-card__detail">{current.detail}</div> : null}

      {current.options?.length ? (
        <div className="question-card__options">
          {current.options.map((option, optionIndex) => {
            const on = draft.selected.includes(option.label)
            const approve = current.intent?.approve === option.label
            return (
              <button
                type="button"
                key={option.label}
                className={`question-option${on ? ' is-on' : ''}${approve ? ' is-approve' : ''}`}
                onClick={() => choose(option.label)}
                aria-pressed={on}
              >
                <span className="question-option__index">{approve ? '✓' : optionIndex + 1}</span>
                <span className="question-option__body">
                  <span className="question-option__label">{option.label}</span>
                  {option.description ? (
                    <span className="question-option__desc">{option.description}</span>
                  ) : null}
                </span>
              </button>
            )
          })}
        </div>
      ) : null}

      <label className="question-card__custom">
        <span className="question-card__custom-icon" aria-hidden>
          ✎
        </span>
        <input
          ref={inputRef}
          className="question-card__input"
          value={draft.custom}
          placeholder="输入你的答案"
          onChange={(event) => setCustom(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
              event.preventDefault()
              goNext()
            }
          }}
        />
      </label>

      <div className="question-card__actions">
        <button type="button" className="question-card__skip" onClick={skip}>
          跳过
        </button>
        <div className="question-card__nav">
          {index > 0 ? (
            <button type="button" className="button" onClick={() => setIndex((value) => value - 1)}>
              上一题
            </button>
          ) : null}
          <button
            type="button"
            className="button button--primary"
            onClick={goNext}
            disabled={!answered(draft)}
          >
            {isLast ? '提交' : '下一题'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Compact entry point for a question whose foreground wait already expired. */
export function QuestionBanner({ questions, onOpen }: { questions: PendingQuestion[]; onOpen: (callId: string) => void }) {
  if (questions.length === 0) return null
  return (
    <div className="question-banner" role="status">
      <span className="question-banner__label">
        {questions.length === 1 ? '还有 1 个问题没有回答' : `还有 ${questions.length} 个问题没有回答`}
      </span>
      <button
        type="button"
        className="question-banner__open"
        onClick={() => onOpen(questions[0].callId)}
      >
        现在回答
      </button>
    </div>
  )
}

/** Question/answer pairs rendered in the transcript (tool row and late reply). */
export function QuestionAnswers({
  questions,
  answers,
}: {
  questions: { id: string; question: string; header?: string }[]
  answers: QuestionAnswer[]
}) {
  const byId = new Map(answers.map((answer) => [answer.id, answer]))
  return (
    <div className="question-answers">
      {questions.map((question) => {
        const answer = byId.get(question.id)
        const values = [...(answer?.selected ?? [])]
        const custom = answer?.custom?.trim() ?? ''
        if (custom) values.push(custom)
        return (
          <div className="question-answers__row" key={question.id}>
            <div className="question-answers__q">
              {question.header ? <span className="question-answers__header">{question.header}</span> : null}
              {question.question}
            </div>
            <div className={`question-answers__a${values.length === 0 ? ' is-skip' : ''}`}>
              {values.length === 0 ? '已跳过' : values.join('、')}
            </div>
          </div>
        )
      })}
    </div>
  )
}
