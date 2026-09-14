import { useEffect, useRef, useState } from 'react'
import type { AgentState, ForkMessageInfo, ModelInfo, SessionInfo, SessionStats } from '../lib/protocol'
import type { ConnectionStatus } from '../lib/useAgent'

interface SidebarProps {
  connection: ConnectionStatus
  serverVersion: string | null
  sessions: SessionInfo[]
  sessionsError: string | null
  currentSessionFile: string | undefined
  onSelectSession: (sessionPath: string) => void
  onNewSession: () => void
  onForkSession: (entryId: string) => void
  onRefresh: () => void
  /** Fork picker data, supplied by the hook from `get_fork_messages`. */
  forkMessages: ForkMessageInfo[]
  onRequestForkMessages: () => void
  models: ModelInfo[]
  state: AgentState | null
  onSelectModel: (model: ModelInfo) => void
  onCycleModel: () => void
  thinkingLevels: string[]
  onSelectThinkingLevel: (level: string) => void
  stats: SessionStats | null
  statusTexts: Record<string, string>
  widgets: Record<string, string[]>
}

function formatCount(value: number | undefined): string {
  return typeof value === 'number' ? value.toLocaleString('zh-CN') : '—'
}

/** `modified` arrives as epoch millis from pi and as ISO-8601 from python-pi. */
function formatModified(modified: number | string | undefined): string {
  if (modified === undefined || modified === '') return ''
  const date = new Date(modified)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}

export function Sidebar({
  connection,
  serverVersion,
  sessions,
  sessionsError,
  currentSessionFile,
  onSelectSession,
  onNewSession,
  onForkSession,
  onRefresh,
  forkMessages,
  onRequestForkMessages,
  models,
  state,
  onSelectModel,
  onCycleModel,
  thinkingLevels,
  onSelectThinkingLevel,
  stats,
  statusTexts,
  widgets,
}: SidebarProps) {
  const [forkOpen, setForkOpen] = useState(false)
  // Held in a ref so the fetch effect depends only on `forkOpen`: the parent's
  // callback identity changes on every state update, which would otherwise loop.
  const requestRef = useRef(onRequestForkMessages)
  requestRef.current = onRequestForkMessages

  useEffect(() => {
    if (forkOpen) requestRef.current()
  }, [forkOpen])

  const model = state?.model ?? null
  const modelValue = model ? `${model.provider}/${model.id}` : ''
  const connectionLabel =
    connection === 'open' ? '已连接' : connection === 'connecting' ? '连接中…' : '连接已断开'

  const tokens = stats?.tokens
  const tokenTotal = typeof tokens?.total === 'number' ? tokens.total : undefined

  return (
    <aside className="sidebar">
      <header className="sidebar__header">
        <div className="sidebar__brand">
          <span className="sidebar__logo" aria-hidden="true">
            π
          </span>
          <div>
            <div className="sidebar__title">Pi 编码助手</div>
            <div className={`sidebar__conn sidebar__conn--${connection}`}>
              <span className="dot" aria-hidden="true" />
              {connectionLabel}
              {serverVersion ? ` · v${serverVersion}` : ''}
            </div>
          </div>
        </div>
        <button type="button" className="button button--block button--primary" onClick={onNewSession}>
          + 新会话
        </button>
      </header>

      <section className="sidebar__section sidebar__section--grow">
        <div className="sidebar__section-head">
          <h2>会话</h2>
          <button type="button" className="link-button" onClick={onRefresh}>
            刷新
          </button>
        </div>
        {sessionsError ? <div className="sidebar__error">会话列表加载失败：{sessionsError}</div> : null}
        <ul className="session-list">
          {sessions.map((session) => {
            const active = Boolean(currentSessionFile && session.path === currentSessionFile)
            return (
              <li key={session.path || session.id}>
                <button
                  type="button"
                  className={`session-list__item${active ? ' is-active' : ''}`}
                  onClick={() => onSelectSession(session.path)}
                  title={session.path}
                >
                  <span className="session-list__name">
                    {session.name || session.firstMessage || session.id || session.path}
                  </span>
                  <span className="session-list__meta">
                    {session.messageCount ?? 0} 条
                    {formatModified(session.modified) ? ` · ${formatModified(session.modified)}` : ''}
                  </span>
                </button>
              </li>
            )
          })}
          {sessions.length === 0 && !sessionsError ? (
            <li className="session-list__empty">没有已保存的会话</li>
          ) : null}
        </ul>
      </section>

      <section className="sidebar__section">
        <div className="sidebar__section-head">
          <h2>模型</h2>
          <button type="button" className="link-button" onClick={onCycleModel} title="切换到下一个可用模型">
            切换
          </button>
        </div>
        <select
          className="select"
          value={modelValue}
          onChange={(event) => {
            const picked = models.find((item) => `${item.provider}/${item.id}` === event.target.value)
            if (picked) onSelectModel(picked)
          }}
        >
          {modelValue === '' ? <option value="">（未知）</option> : null}
          {models.map((item) => (
            <option key={`${item.provider}/${item.id}`} value={`${item.provider}/${item.id}`}>
              {item.name || item.id} · {item.provider}
            </option>
          ))}
        </select>

        <label className="field">
          <span className="field__label">思考级别</span>
          <select
            className="select"
            value={state?.thinkingLevel ?? ''}
            onChange={(event) => onSelectThinkingLevel(event.target.value)}
          >
            {(state?.thinkingLevel && !thinkingLevels.includes(state.thinkingLevel)
              ? [state.thinkingLevel, ...thinkingLevels]
              : thinkingLevels
            ).map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          className="button button--ghost button--block"
          onClick={() => setForkOpen((open) => !open)}
        >
          {forkOpen ? '收起分叉' : '从历史消息分叉'}
        </button>
        {forkOpen ? (
          <div className="fork-list">
            {forkMessages.length === 0 ? (
              <div className="fork-list__empty">没有可分叉的用户消息</div>
            ) : (
              forkMessages.map((item) => (
                <button
                  type="button"
                  key={item.entryId}
                  className="fork-list__item"
                  onClick={() => {
                    onForkSession(item.entryId)
                    setForkOpen(false)
                  }}
                >
                  {item.text.slice(0, 60) || item.entryId}
                </button>
              ))
            )}
          </div>
        ) : null}
      </section>

      <section className="sidebar__section">
        <div className="sidebar__section-head">
          <h2>会话统计</h2>
        </div>
        <dl className="stats">
          <div>
            <dt>消息</dt>
            <dd>{formatCount(stats?.totalMessages ?? state?.messageCount)}</dd>
          </div>
          <div>
            <dt>用户 / 助手</dt>
            <dd>
              {formatCount(stats?.userMessages)} / {formatCount(stats?.assistantMessages)}
            </dd>
          </div>
          <div>
            <dt>工具调用</dt>
            <dd>{formatCount(stats?.toolCalls)}</dd>
          </div>
          <div>
            <dt>Token</dt>
            <dd>{formatCount(tokenTotal)}</dd>
          </div>
          <div>
            <dt>费用</dt>
            <dd>{typeof stats?.cost === 'number' ? `$${stats.cost.toFixed(4)}` : '—'}</dd>
          </div>
          {typeof state?.pendingMessageCount === 'number' && state.pendingMessageCount > 0 ? (
            <div>
              <dt>待处理</dt>
              <dd>{state.pendingMessageCount}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      {Object.keys(statusTexts).length > 0 || Object.keys(widgets).length > 0 ? (
        <section className="sidebar__section sidebar__status">
          {Object.entries(statusTexts).map(([key, text]) => (
            <div className="status-line" key={key}>
              <span className="status-line__key">{key}</span>
              <span className="status-line__text">{text}</span>
            </div>
          ))}
          {Object.entries(widgets).map(([key, lines]) => (
            <div className="widget" key={key}>
              <div className="widget__key">{key}</div>
              {lines.map((line, index) => (
                <div className="widget__line" key={`${key}-${index}`}>
                  {line}
                </div>
              ))}
            </div>
          ))}
        </section>
      ) : null}
    </aside>
  )
}
