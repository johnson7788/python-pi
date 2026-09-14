/**
 * The app shell: who may see what, and which page they see.
 *
 * The gate order matters and is easy to get wrong:
 *
 * 1. **checking** — the answer to "are there accounts?" has not arrived. Rendering either
 *    the login form or the workspace here would flash the wrong one on every load.
 * 2. **no session on a team server** — the login page, and nothing else.
 * 3. **a session that must change its password** — the change form, full screen. The server
 *    would keep working, but letting someone use a shared initial password is the thing the
 *    flag exists to prevent.
 * 4. **authenticated** — the shell, with one page inside it.
 *
 * The agent socket is opened *inside* the shell, so a browser that is logged out never
 * opens one. It stays open on the file and usage pages too: a turn that is still streaming
 * should not be interrupted by someone looking at their quota.
 */

import { Sidebar } from './components/Sidebar'
import { TopBar } from './components/TopBar'
import { Transcript } from './components/Transcript'
import { Composer } from './components/Composer'
import { DialogHost } from './components/Dialog'
import { Toasts } from './components/Toasts'
import { useAgent } from './lib/useAgent'
import type { AgentApi } from './lib/useAgent'
import { useAuth } from './lib/useAuth'
import type { AuthApi } from './lib/useAuth'
import { routeSection, useRoute } from './lib/router'
import { Admin } from './pages/Admin'
import { Files } from './pages/Files'
import { ChangePassword, Login } from './pages/Login'
import { Usage } from './pages/Usage'

export function App() {
  const auth = useAuth()
  const route = useRoute()

  if (auth.status === 'checking') {
    return (
      <div className="auth">
        <div className="auth__panel auth__panel--plain">正在检查登录状态…</div>
      </div>
    )
  }
  if (!auth.authenticated) return <Login auth={auth} />
  if (auth.mustChangePassword) return <ChangePassword auth={auth} forced />

  return <Shell auth={auth} route={route} />
}

function Shell({ auth, route }: { auth: AuthApi; route: string }) {
  const agent = useAgent({ enabled: true, onUnauthorized: auth.onUnauthorized })
  const section = routeSection(route)

  return (
    <>
      <div className="app">
        <Sidebar
          connection={agent.connection}
          serverVersion={agent.serverVersion}
          sessions={agent.sessions}
          sessionsError={agent.sessionsError}
          currentSessionFile={agent.state?.sessionFile}
          onSelectSession={(path) => {
            agent.switchSession(path)
            if (section !== '/') window.location.hash = '/'
          }}
          onNewSession={() => {
            agent.newSession()
            if (section !== '/') window.location.hash = '/'
          }}
          onForkSession={agent.fork}
          onRefresh={agent.refreshAll}
          forkMessages={agent.forkMessages}
          onRequestForkMessages={() => agent.send({ type: 'get_fork_messages' })}
          models={agent.models}
          state={agent.state}
          onSelectModel={agent.setModel}
          onCycleModel={agent.cycleModel}
          thinkingLevels={agent.thinkingLevels}
          onSelectThinkingLevel={agent.setThinkingLevel}
          stats={agent.stats}
          statusTexts={agent.statusTexts}
          widgets={agent.widgets}
        />

        <main className="main">
          <TopBar auth={auth} route={route} streaming={agent.isStreaming} />
          {section === '/files' ? (
            <Files />
          ) : section === '/usage' ? (
            <Usage />
          ) : section === '/admin' ? (
            auth.user?.isAdmin ? (
              <Admin />
            ) : (
              <div className="page">
                <div className="note note--error">需要管理员权限。</div>
              </div>
            )
          ) : section === '/account' ? (
            <ChangePassword auth={auth} forced={false} inline />
          ) : (
            <SessionView agent={agent} />
          )}
        </main>
      </div>

      <DialogHost dialogs={agent.dialogs} onRespond={agent.respondDialog} />
      <Toasts toasts={agent.toasts} onDismiss={agent.dismissToast} />
    </>
  )
}

/** The original single-page view: transcript, errors and the composer. */
function SessionView({ agent }: { agent: AgentApi }) {
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
  } = agent

  const disconnected = connection !== 'open'
  const sessionName = state?.sessionName || (state?.sessionId ? state.sessionId.slice(0, 8) : '未命名会话')
  const autoCompaction = Boolean(state?.autoCompactionEnabled)

  return (
    <div className="session">
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
    </div>
  )
}
