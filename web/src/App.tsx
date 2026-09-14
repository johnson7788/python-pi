import { Sidebar } from './components/Sidebar'
import { Transcript } from './components/Transcript'
import { Composer } from './components/Composer'
import { DialogHost } from './components/Dialog'
import { Toasts } from './components/Toasts'
import { useAgent } from './lib/useAgent'

export function App() {
  const agent = useAgent()
  const {
    connection,
    hasConnected,
    state,
    transcript,
    toolExecutions,
    isStreaming,
    isCompacting,
    commands,
    errors,
    dialogs,
  } = agent

  const disconnected = connection !== 'open'
  const sessionName = state?.sessionName || (state?.sessionId ? state.sessionId.slice(0, 8) : '未命名会话')
  const autoCompaction = Boolean(state?.autoCompactionEnabled)

  return (
    <div className="app">
      <Sidebar
        connection={connection}
        serverVersion={agent.serverVersion}
        sessions={agent.sessions}
        sessionsError={agent.sessionsError}
        currentSessionFile={state?.sessionFile}
        onSelectSession={agent.switchSession}
        onNewSession={agent.newSession}
        onForkSession={agent.fork}
        onRefresh={agent.refreshAll}
        forkMessages={agent.forkMessages}
        onRequestForkMessages={() => agent.send({ type: 'get_fork_messages' })}
        models={agent.models}
        state={state}
        onSelectModel={agent.setModel}
        onCycleModel={agent.cycleModel}
        thinkingLevels={agent.thinkingLevels}
        onSelectThinkingLevel={agent.setThinkingLevel}
        stats={agent.stats}
        statusTexts={agent.statusTexts}
        widgets={agent.widgets}
      />

      <main className="main">
        {disconnected && hasConnected ? (
          <div className="banner banner--warn">
            <span>连接已断开，正在尝试重连…</span>
            <button type="button" className="button button--ghost" onClick={agent.reconnectNow}>
              立即重连
            </button>
          </div>
        ) : null}

        {connection === 'connecting' ? <div className="banner banner--info">正在连接服务器…</div> : null}

        <header className="topbar">
          <div className="topbar__title" title={state?.sessionFile ?? ''}>
            {sessionName}
          </div>
          <div className="topbar__actions">
            {isCompacting ? <span className="topbar__badge">正在压缩上下文…</span> : null}
            <label className="checkbox">
              <input
                type="checkbox"
                checked={autoCompaction}
                onChange={(event) => agent.setAutoCompaction(event.target.checked)}
              />
              自动压缩
            </label>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => agent.compact()}
              disabled={disconnected || isStreaming}
              title="立即压缩当前会话上下文"
            >
              压缩上下文
            </button>
          </div>
        </header>

        <Transcript transcript={transcript} toolExecutions={toolExecutions} streaming={isStreaming} />

        {errors.length > 0 ? (
          <div className="errors">
            {errors.map((error) => (
              <div className="error-line" key={error.id}>
                <span className="error-line__source">{error.source}</span>
                <span className="error-line__text">{error.message}</span>
                <button
                  type="button"
                  className="error-line__close"
                  onClick={() => agent.dismissError(error.id)}
                  aria-label="关闭"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <Composer
          streaming={isStreaming}
          commands={commands}
          onSend={agent.sendPrompt}
          onAbort={agent.abort}
          injectedText={agent.editorText}
          onInjectedTextConsumed={agent.consumeEditorText}
          disabled={disconnected}
        />
      </main>

      <DialogHost dialogs={dialogs} onRespond={agent.respondDialog} />
      <Toasts toasts={agent.toasts} onDismiss={agent.dismissToast} />
    </div>
  )
}
