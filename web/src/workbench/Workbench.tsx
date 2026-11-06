/**
 * The Workbench shell — a parallel implementation of the product prototype at
 * `/workbench`. The existing app stays the default while this catches up.
 *
 * M0-M4: shell, real sessions, skills registry, workspaces, library grid, and the
 * app/mode/capsule composition. Plugins, schedules and artifacts are still empty
 * states until their backends exist.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'

import { DialogHost } from '../components/Dialog'
import { Toasts } from '../components/Toasts'
import { ToolCard } from '../components/ToolCard'
import { api } from '../lib/api'
import { toolResultText } from '../lib/useAgent'
import { resolveApiBase } from '../lib/endpoints'
import { renderMarkdown } from '../lib/markdown'
import { asRecord, isTextBlock, isThinkingBlock, isToolCallBlock } from '../lib/protocol'
import type {
  AssistantMessage,
  ContentBlock,
  CustomMessage,
  Message,
  Usage,
} from '../lib/protocol'
import { navigate } from '../lib/router'
import { useAgent } from '../lib/useAgent'
import type { AgentApi, ToolExecution, TranscriptEntry } from '../lib/useAgent'
import type { AuthApi } from '../lib/useAuth'
import './workbench.css'

type View = 'home' | 'chat' | 'apps' | 'skills' | 'plugins' | 'scheduled' | 'files'

interface WorkspaceRow {
  id: string
  name: string
  isDefault: boolean
  path?: string
}

interface AppCapsule {
  id: string
  name: string
  prompt: string
}

interface AppModeRow {
  id: string
  name: string
  description: string
  skillBinding: 'all' | string[]
  prompt?: string
  capsules: AppCapsule[]
}

interface AppRow {
  id: string
  name: string
  description: string
  accent: string
  builtin?: boolean
  modes: AppModeRow[]
}

interface AppAuditRow {
  id: string
  action: string
  actor: string
  createdAt: string
}

const APP_ACTION_LABEL: Record<string, string> = {
  create: '创建',
  update: '修改',
  delete: '删除',
  restore: '恢复默认',
}

interface NotificationRow {
  id: string
  kind: string
  title: string
  body: string
  link: string
  read: boolean
  createdAt: string
}

const NAV: { view: View; label: string; icon: IconName }[] = [
  { view: 'apps', label: '应用', icon: 'apps' },
  { view: 'skills', label: '技能', icon: 'skill' },
  { view: 'plugins', label: '插件', icon: 'plugin' },
  { view: 'scheduled', label: '定时任务', icon: 'clock' },
  { view: 'files', label: '资料库', icon: 'book' },
]

const MODES = {
  office: {
    label: '办公',
    icon: 'spark' as IconName,
    placeholder: '/调用技能与指令',
    quick: ['制作 PPT', '深度研究', '策划方案', '定时任务'],
    examples: [
      '这个周末，附近有什么值得去',
      '最近 7 天，短视频都在流行什么',
      '每月定投 1000 元，5 年后能有多少钱',
      '最近 AI 行业变了什么，给我一份变更日志',
    ],
  },
  dev: {
    label: '开发',
    icon: 'code' as IconName,
    placeholder: '描述要改的功能，或贴一段报错',
    quick: ['读仓库结构', '定位一个 bug', '补测试', '准备发布'],
    examples: [
      '把服务端的排队消息改成持久化',
      '这个 traceback 是什么原因，帮我修掉',
      '给会话模块补 5 个边界测试',
      '看看这个仓库最该先补的 3 个能力',
    ],
  },
  design: {
    label: '设计',
    icon: 'palette' as IconName,
    placeholder: '描述你想做的界面或视觉方向',
    quick: ['做原型', '配色方案', '竞品分析', '图标'],
    examples: [
      '把首页做成新版那样，先出 HTML',
      '给这个产品定一套克制的浅色配色',
      '拆解 3 个同类产品的首屏差异',
      '这套界面还缺什么状态，列给我',
    ],
  },
}
type Mode = keyof typeof MODES

const FALLBACK_APP: AppRow = {
  id: 'general',
  name: '通用工作台',
  description: '日常办公、开发与设计任务。',
  accent: '#3b5fba',
  modes: (Object.keys(MODES) as Mode[]).map((key) => ({
    id: key,
    name: MODES[key].label,
    description: '',
    skillBinding: 'all' as const,
    capsules: MODES[key].quick.map((name) => ({ id: name, name, prompt: name })),
  })),
}

const ICON_PATHS: Record<string, ReactNode> = {
  apps: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
    </>
  ),
  skill: (
    <>
      <rect x="3" y="4" width="8" height="7" rx="1" />
      <rect x="13" y="4" width="8" height="4" rx="1" />
      <rect x="13" y="10" width="8" height="10" rx="1" />
      <rect x="3" y="13" width="8" height="7" rx="1" />
    </>
  ),
  plugin: (
    <>
      <path d="M9 7V3M15 7V3M7.5 7h9v4a4.5 4.5 0 0 1-9 0V7Z" />
      <path d="M12 15.5V21" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  book: (
    <>
      <path d="M3.5 5.5A2 2 0 0 1 5.5 3.5H20v15H5.5a2 2 0 0 0-2 2v-15Z" />
      <path d="M3.5 20.5a2 2 0 0 1 2-2H20" />
    </>
  ),
  more: (
    <>
      <circle cx="5.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="18.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </>
  ),
  house: <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-5h-4v5H5a1 1 0 0 1-1-1v-9.5Z" />,
  folder: <path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2h7.3a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-10Z" />,
  file: (
    <>
      <path d="M6 3h8l4 4v14H6V3Z" />
      <path d="M14 3v5h5" />
    </>
  ),
  image: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="8.5" cy="9.5" r="1.5" />
      <path d="m4 18 5-5 3 3 3-3 5 5" />
    </>
  ),
  slides: (
    <>
      <rect x="3" y="4" width="18" height="13" rx="1.5" />
      <path d="M12 17v3M8 21h8M8 8h8M8 11h5" />
    </>
  ),
  sheet: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 9h18M3 14h18M9 4v16M15 4v16" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  arrowUp: <path d="M12 19V5M6 11l6-6 6 6" />,
  stop: <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none" />,
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
    </>
  ),
  chevronLeft: <path d="m14.5 6-6 6 6 6" />,
  chevronDown: <path d="m6 9.5 6 6 6-6" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.6-3.6" />
    </>
  ),
  bell: (
    <>
      <path d="M6.5 10a5.5 5.5 0 0 1 11 0c0 4 1.5 5.5 1.5 5.5H5S6.5 14 6.5 10Z" />
      <path d="M10 19a2.2 2.2 0 0 0 4 0" />
    </>
  ),
  panel: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9.5 4v16" />
    </>
  ),
  spark: (
    <>
      <path d="M12 3.5 13.9 9l5.6 1.9-5.6 1.9L12 18.4l-1.9-5.6L4.5 10.9 10.1 9 12 3.5Z" />
      <path d="M18.5 16.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7.7-2Z" />
    </>
  ),
  code: <path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4M13.5 5l-3 14" />,
  palette: (
    <>
      <path d="M12 3.5a8.5 8.5 0 0 0 0 17c1.5 0 2-1 1.5-2-.7-1.4.3-2.5 1.8-2.5H18a3 3 0 0 0 3-3c0-5-4-9.5-9-9.5Z" />
      <circle cx="8" cy="11" r="1" fill="currentColor" stroke="none" />
      <circle cx="11.5" cy="7.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="15.5" cy="9" r="1" fill="currentColor" stroke="none" />
    </>
  ),
  logout: <path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M16 8l4 4-4 4M20 12H9" />,
}

type IconName = keyof typeof ICON_PATHS

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="wb-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICON_PATHS[name]}
    </svg>
  )
}

function viewFromRoute(route: string): View {
  const rest = route.replace(/^\/workbench\/?/, '')
  const section = rest.split('/')[0]
  if (section === 'chat') return 'chat'
  if (section === 'apps') return 'apps'
  if (section === 'skills') return 'skills'
  if (section === 'plugins') return 'plugins'
  if (section === 'scheduled') return 'scheduled'
  if (section === 'files') return 'files'
  return 'home'
}

function go(path: string): void {
  navigate(path)
}

function formatWhen(value: number | string | undefined): string {
  if (value === undefined || value === '') return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  return sameDay
    ? `今天 ${date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })}`
    : date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function sessionTitle(session: { name?: string | null; firstMessage?: string | null; id: string }): string {
  return session.name?.trim() || session.firstMessage?.trim() || session.id.slice(0, 8)
}

function userText(message: Message): string {
  const content = (message as { content?: string | ContentBlock[] }).content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter(isTextBlock).map((block) => block.text).join('\n')
}

function usageLine(usage: Usage | undefined): string {
  if (!usage) return ''
  const tokens = usage.totalTokens ?? (usage.input ?? 0) + (usage.output ?? 0)
  const cost = usage.cost?.total
  return [`${tokens} tokens`, typeof cost === 'number' && cost > 0 ? `$${cost.toFixed(4)}` : '']
    .filter(Boolean)
    .join(' · ')
}

interface WorkspaceSession {
  path: string
  id: string
  name?: string | null
  firstMessage?: string | null
  modified?: number | string
}

export function Workbench({ auth, route }: { auth: AuthApi; route: string }) {
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([
    { id: 'default', name: '默认工作空间', isDefault: true },
  ])
  const [activeWorkspace, setActiveWorkspace] = useState('default')
  const [canCreateWorkspace, setCanCreateWorkspace] = useState(false)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [apps, setApps] = useState<AppRow[]>([])
  const [canManageApps, setCanManageApps] = useState(false)
  const [activeAppId, setActiveAppId] = useState('general')
  const [activeModeId, setActiveModeId] = useState('office')
  const agent = useAgent({
    enabled: true,
    workspace: activeWorkspace,
    appId: activeAppId,
    modeId: activeModeId,
    onUnauthorized: auth.onUnauthorized,
  })
  const view = viewFromRoute(route)
  const [folded, setFolded] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [notifications, setNotifications] = useState<NotificationRow[]>([])
  const [unread, setUnread] = useState(0)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [tasksOpen, setTasksOpen] = useState(true)
  const [spacesOpen, setSpacesOpen] = useState(true)
  const [expandedSpaces, setExpandedSpaces] = useState<Record<string, boolean>>({})
  const [workspaceSessions, setWorkspaceSessions] = useState<Record<string, WorkspaceSession[]>>({})
  const [draft, setDraft] = useState('')
  const [chatDraft, setChatDraft] = useState('')

  const connectionOpen = agent.connection === 'open'
  const sessions = agent.sessions.slice(0, 5)
  const workspaceName =
    workspaces.find((workspace) => workspace.id === activeWorkspace)?.name || '默认工作空间'
  const availableApps = apps.length > 0 ? apps : [FALLBACK_APP]
  const activeApp = availableApps.find((app) => app.id === activeAppId) || availableApps[0]
  const activeMode = activeApp.modes.find((item) => item.id === activeModeId) || activeApp.modes[0]
  const userName = auth.user?.displayName || auth.user?.username || '本地用户'

  useEffect(() => {
    api
      .get<{ workspaces: WorkspaceRow[]; canCreate: boolean }>('/api/workspaces')
      .then((payload) => {
        setWorkspaces(payload.workspaces)
        setCanCreateWorkspace(payload.canCreate)
        setWorkspaceError(null)
        if (!payload.workspaces.some((workspace) => workspace.id === activeWorkspace)) {
          setActiveWorkspace(payload.workspaces[0]?.id || 'default')
        }
      })
      .catch((failure: unknown) => {
        setWorkspaceError(failure instanceof Error ? failure.message : '读取空间失败')
      })
  }, [activeWorkspace])

  useEffect(() => {
    setWorkspaceSessions((current) => ({ ...current, [activeWorkspace]: agent.sessions }))
  }, [agent.sessions, activeWorkspace])

  const loadWorkspaceSessions = async (workspace: string) => {
    if (workspaceSessions[workspace]) return
    try {
      const payload = await api.get<{ sessions: WorkspaceSession[] }>(
        `/api/sessions?workspace=${encodeURIComponent(workspace)}`,
      )
      setWorkspaceSessions((current) => ({ ...current, [workspace]: payload.sessions }))
    } catch {
      setWorkspaceSessions((current) => ({ ...current, [workspace]: [] }))
    }
  }

  const toggleWorkspace = (workspace: string) => {
    const opening = !expandedSpaces[workspace]
    setExpandedSpaces((current) => ({ ...current, [workspace]: opening }))
    if (opening) void loadWorkspaceSessions(workspace)
  }

  const loadApps = () => {
    api
      .get<{ apps: AppRow[]; canManage?: boolean }>('/api/apps')
      .then((payload) => {
        setApps(payload.apps)
        setCanManageApps(Boolean(payload.canManage))
        if (payload.apps.length > 0) {
          const current = payload.apps.find((app) => app.id === activeAppId)
          if (!current) {
            setActiveAppId(payload.apps[0].id)
            setActiveModeId(payload.apps[0].modes[0]?.id || 'office')
          }
        }
      })
      .catch(() => {
        // The fallback catalog keeps the workbench usable when the endpoint is absent.
      })
  }

  useEffect(loadApps, [activeAppId])

  useEffect(() => {
    let alive = true
    const load = () => {
      api
        .get<{ notifications: NotificationRow[]; unread: number }>('/api/notifications')
        .then((payload) => {
          if (!alive) return
          setNotifications(payload.notifications)
          setUnread(payload.unread)
        })
        .catch(() => {
          // Single mode (or a server without notifications) just hides the badge.
        })
    }
    load()
    const timer = window.setInterval(load, 30_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    if (!notificationsOpen) return
    const close = () => setNotificationsOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [notificationsOpen])

  useEffect(() => {
    document.body.classList.toggle('wb-nav-open', navOpen)
    return () => document.body.classList.remove('wb-nav-open')
  }, [navOpen])

  useEffect(() => {
    if (!moreOpen) return
    const close = () => setMoreOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [moreOpen])

  const goView = (next: View) => {
    setNavOpen(false)
    if (next === 'home') go('/workbench')
    else if (next === 'chat') go('/workbench/chat')
    else go(`/workbench/${next}`)
  }

  const newTask = () => {
    agent.newSession()
    setDraft('')
    goView('home')
  }

  const switchWorkspace = (workspace: string) => {
    if (workspace === activeWorkspace) return
    setDraft('')
    setChatDraft('')
    setActiveWorkspace(workspace)
    setNavOpen(false)
    goView('home')
  }

  const createWorkspace = async () => {
    const name = window.prompt('新空间名称')?.trim()
    if (!name) return
    try {
      const created = await api.post<WorkspaceRow>('/api/workspaces', { name })
      setWorkspaces((current) => [...current, created])
      setWorkspaceError(null)
      switchWorkspace(created.id)
    } catch (failure) {
      setWorkspaceError(failure instanceof Error ? failure.message : '创建空间失败')
    }
  }

  const openSession = (path: string) => {
    agent.switchSession(path)
    setNavOpen(false)
    go('/workbench/chat')
  }

  const sendHome = () => {
    const text = draft.trim()
    if (!text || !connectionOpen) return
    agent.sendPrompt(text)
    setDraft('')
    go('/workbench/chat')
  }

  const readNotification = async (notification: NotificationRow) => {
    if (!notification.read) {
      try {
        await api.post(`/api/notifications/${notification.id}/read`)
        setNotifications((current) =>
          current.map((item) => (item.id === notification.id ? { ...item, read: true } : item)),
        )
        setUnread((value) => Math.max(0, value - 1))
      } catch {
        // Leave it unread; the next poll will retry.
      }
    }
    setNotificationsOpen(false)
    if (notification.link) go(notification.link)
  }

  const readAllNotifications = async () => {
    try {
      await api.post('/api/notifications/read-all')
      setNotifications((current) => current.map((item) => ({ ...item, read: true })))
      setUnread(0)
    } catch {
      // Ignore; the badge will correct itself on the next poll.
    }
  }

  const sendChat = () => {
    const text = chatDraft.trim()
    if (!text || !connectionOpen) return
    agent.sendPrompt(text)
    setChatDraft('')
  }

  return (
    <>
      <div className={`wb${folded ? ' is-folded' : ''}${navOpen ? ' is-nav-open' : ''}`}>
        <aside className="wb-sidebar">
          <div className="wb-side-head">
            <button type="button" className="wb-logo" onClick={() => goView('home')} aria-label="回到首页">
              <img src="/pi-icon.png" alt="" />
              <span>Pi</span>
            </button>
            <button
              type="button"
              className="wb-fold"
              onClick={() => setFolded((value) => !value)}
              aria-label={folded ? '展开侧栏' : '收起侧栏'}
            >
              <Icon name="panel" size={17} />
            </button>
          </div>
          <div className="wb-version">V0.1.0</div>

          <button type="button" className="wb-new" onClick={newTask}>
            <Icon name="plus" size={16} />
            <span>新建任务</span>
          </button>

          <nav className="wb-nav" aria-label="主导航">
            {NAV.map((item) => (
              <button
                key={item.view}
                type="button"
                className={`wb-nav-item${view === item.view ? ' is-active' : ''}`}
                onClick={() => goView(item.view)}
              >
                <Icon name={item.icon} size={19} />
                <span>{item.label}</span>
                {item.view === 'scheduled' ? <i className="wb-dot" /> : null}
              </button>
            ))}
            <button
              type="button"
              className="wb-nav-item"
              onClick={(event) => {
                event.stopPropagation()
                setMoreOpen((value) => !value)
              }}
            >
              <Icon name="more" size={19} />
              <span>更多</span>
            </button>
          </nav>

          <div className="wb-scroll">
            <div className="wb-section-head">
              <button
                type="button"
                className="wb-section-toggle"
                onClick={() => setTasksOpen((value) => !value)}
                aria-expanded={tasksOpen}
              >
                <span>任务</span>
                <b>({agent.sessions.length})</b>
                <Icon name="chevronDown" size={12} />
              </button>
            </div>
            {tasksOpen ? (
              <div className="wb-session-list">
                {sessions.length === 0 ? <p className="wb-side-empty">还没有任务</p> : null}
                {sessions.map((session) => {
                  const active = Boolean(agent.state?.sessionFile && session.path === agent.state.sessionFile)
                  return (
                    <button
                      key={session.path || session.id}
                      type="button"
                      className={`wb-session${active ? ' is-active' : ''}`}
                      title={sessionTitle(session)}
                      onClick={() => openSession(session.path)}
                    >
                      <span>{sessionTitle(session)}</span>
                      <time>{formatWhen(session.modified)}</time>
                    </button>
                  )
                })}
              </div>
            ) : null}

            <div className="wb-section-head">
              <button
                type="button"
                className="wb-section-toggle"
                onClick={() => setSpacesOpen((value) => !value)}
                aria-expanded={spacesOpen}
              >
                <span>空间</span>
                <b>({workspaces.length})</b>
                <Icon name="chevronDown" size={12} />
              </button>
              {canCreateWorkspace ? (
                <button
                  type="button"
                  className="wb-section-add"
                  onClick={(event) => {
                    event.stopPropagation()
                    void createWorkspace()
                  }}
                  aria-label="新建空间"
                >
                  <Icon name="plus" size={13} />
                </button>
              ) : null}
            </div>
            {spacesOpen ? (
              <>
                {workspaceError ? <p className="wb-side-empty">{workspaceError}</p> : null}
                {workspaces.map((workspace) => {
                  const expanded = Boolean(expandedSpaces[workspace.id])
                  const listed = workspaceSessions[workspace.id]
                  const count = workspace.id === activeWorkspace ? agent.sessions.length : listed?.length
                  return (
                    <div className="wb-space-block" key={workspace.id}>
                      <div className={`wb-space-row${workspace.id === activeWorkspace ? ' is-active' : ''}`}>
                        <button
                          type="button"
                          className="wb-space"
                          onClick={() => {
                            switchWorkspace(workspace.id)
                            if (!expanded) void loadWorkspaceSessions(workspace.id)
                          }}
                          title={workspace.path || workspace.name}
                        >
                          <Icon name="house" size={17} />
                          <span>{workspace.name}</span>
                          {typeof count === 'number' ? <i>{count}</i> : null}
                        </button>
                        <button
                          type="button"
                          className="wb-space-toggle"
                          aria-label={expanded ? '收起对话' : '展开对话'}
                          aria-expanded={expanded}
                          onClick={() => toggleWorkspace(workspace.id)}
                        >
                          <Icon name="chevronDown" size={12} />
                        </button>
                      </div>
                      {expanded ? (
                        <div className="wb-space-sessions">
                          {listed === undefined ? (
                            <p className="wb-side-empty">读取对话…</p>
                          ) : listed.length === 0 ? (
                            <p className="wb-side-empty">还没有对话</p>
                          ) : (
                            listed.map((session) => (
                              <button
                                key={session.path || session.id}
                                type="button"
                                className="wb-space-session"
                                title={sessionTitle(session)}
                                onClick={() => openSession(session.path)}
                              >
                                <span>{sessionTitle(session)}</span>
                                <time>{formatWhen(session.modified)}</time>
                              </button>
                            ))
                          )}
                        </div>
                      ) : null}
                    </div>
                  )
                })}
              </>
            ) : null}
          </div>

          <div className="wb-foot">
            <span className="wb-avatar">{userName.slice(0, 1)}</span>
            <span className="wb-who">{userName}</span>
            <button
              type="button"
              className="wb-bell"
              title={unread ? `${unread} 条未读通知` : '通知'}
              onClick={(event) => {
                event.stopPropagation()
                setNotificationsOpen((value) => !value)
              }}
            >
              <Icon name="bell" size={17} />
              {unread > 0 ? <i>{unread > 9 ? '9+' : unread}</i> : null}
            </button>
          </div>

          {notificationsOpen ? (
            <div className="wb-notifications" onClick={(event) => event.stopPropagation()}>
              <header>
                <b>通知</b>
                <button type="button" className="wb-link" onClick={() => void readAllNotifications()}>
                  全部已读
                </button>
              </header>
              <div className="wb-notification-list">
                {notifications.length === 0 ? (
                  <p className="wb-muted">还没有通知。</p>
                ) : (
                  notifications.map((notification) => (
                    <button
                      key={notification.id}
                      type="button"
                      className={`wb-notification${notification.read ? '' : ' is-unread'}`}
                      onClick={() => void readNotification(notification)}
                    >
                      <span className={`wb-run-dot is-${notification.kind.endsWith('failed') ? 'failed' : 'success'}`} />
                      <span>
                        <b>{notification.title}</b>
                        <i>{notification.body}</i>
                        <time>{formatDateTime(notification.createdAt)}</time>
                      </span>
                    </button>
                  ))
                )}
              </div>
            </div>
          ) : null}

          {moreOpen ? (
            <div className="wb-more" onClick={(event) => event.stopPropagation()}>
              {auth.authMode === 'team' ? (
                <button type="button" onClick={() => go('/account')}>
                  修改密码
                </button>
              ) : null}
              <div className="wb-more-line" />
              {auth.authMode === 'team' ? (
                <button type="button" onClick={() => void auth.logout()}>
                  退出登录
                </button>
              ) : (
                <button type="button" onClick={() => setMoreOpen(false)}>
                  帮助
                </button>
              )}
            </div>
          ) : null}
        </aside>

        <div className="wb-scrim" onClick={() => setNavOpen(false)} />

        <main className="wb-main">
          <button type="button" className="wb-mobile-menu" onClick={() => setNavOpen(true)} aria-label="打开导航">
            <Icon name="panel" size={18} />
          </button>

          {view === 'home' ? (
            <HomeView
              app={activeApp}
              modeId={activeMode.id}
              onChangeMode={setActiveModeId}
              draft={draft}
              onDraft={setDraft}
              onSend={sendHome}
              disabled={!connectionOpen}
              streaming={agent.isStreaming}
              onAbort={agent.abort}
              workspaceName={workspaceName}
            />
          ) : null}

          {view === 'chat' ? (
            <ChatView
              agent={agent}
              draft={chatDraft}
              onDraft={setChatDraft}
              onSend={sendChat}
              disabled={!connectionOpen}
              onHome={() => goView('home')}
              workspaceId={activeWorkspace}
              workspaceName={workspaceName}
              appName={activeApp.name}
              modeName={activeMode.name}
            />
          ) : null}

          {view === 'skills' ? <SkillsView workspace={activeWorkspace} /> : null}
          {view === 'apps' ? (
            <AppsView
              apps={availableApps}
              activeAppId={activeApp.id}
              canManage={canManageApps}
              onSaved={loadApps}
              onSelect={(app) => {
                setActiveAppId(app.id)
                setActiveModeId(
                  app.id === activeApp.id ? activeMode.id : app.modes[0]?.id || 'office',
                )
                goView('home')
              }}
            />
          ) : null}
          {view === 'plugins' ? <PluginsView appId={activeAppId} /> : null}
          {view === 'scheduled' ? (
            <ScheduledView
              workspaces={workspaces}
              apps={availableApps}
              defaultWorkspace={activeWorkspace}
              defaultAppId={activeApp.id}
              defaultModeId={activeMode.id}
            />
          ) : null}
          {view === 'files' ? <LibraryView workspace={activeWorkspace} /> : null}
        </main>
      </div>

      <DialogHost dialogs={agent.dialogs} onRespond={agent.respondDialog} />
      <Toasts toasts={agent.toasts} onDismiss={agent.dismissToast} />
    </>
  )
}

function HomeView({
  app,
  modeId,
  onChangeMode,
  draft,
  onDraft,
  onSend,
  disabled,
  streaming,
  onAbort,
  workspaceName,
}: {
  app: AppRow
  modeId: string
  onChangeMode: (mode: string) => void
  draft: string
  onDraft: (value: string) => void
  onSend: () => void
  disabled: boolean
  streaming: boolean
  onAbort: () => void
  workspaceName: string
}) {
  const mode = app.modes.find((item) => item.id === modeId) || app.modes[0]
  const fallback = MODES[(mode.id as Mode)] || MODES.office
  const quick = mode.capsules.length
    ? mode.capsules
    : fallback.quick.map((label) => ({ id: label, name: label, prompt: label }))

  return (
    <div className="wb-home">
      <div className="wb-home-inner">
        <img className="wb-hero-icon" src="/pi-icon.png" alt="" />
        <h1>Pi，我帮你</h1>
        <div className="wb-app-line">
          <span style={{ background: app.accent }} />
          {app.name} · {mode.name}
        </div>

        <div className="wb-modes" role="tablist" aria-label="工作模式">
          {app.modes.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={mode.id === item.id}
              className={`wb-mode${mode.id === item.id ? ' is-active' : ''}`}
              onClick={() => onChangeMode(item.id)}
              title={item.description}
            >
              <Icon name={MODES[(item.id as Mode)]?.icon || 'spark'} size={15} />
              {item.name}
            </button>
          ))}
        </div>

        <div className="wb-quick">
          {quick.map((capsule) => (
            <button key={capsule.id} type="button" onClick={() => onDraft(capsule.prompt)}>
              {capsule.name}
            </button>
          ))}
        </div>

        <WbComposer
          value={draft}
          onChange={onDraft}
          onSend={onSend}
          onAbort={onAbort}
          streaming={streaming}
          disabled={disabled}
          placeholder={fallback.placeholder}
          workspaceName={workspaceName}
        />

        <div className="wb-examples">
          {fallback.examples.map((example) => (
            <button key={example} type="button" onClick={() => onDraft(example)}>
              <span>{example}</span>
              <Icon name="arrowUp" size={14} />
            </button>
          ))}
        </div>

        <p className="wb-declare">以上内容均由 AI 生成，仅供参考和借鉴</p>
      </div>
    </div>
  )
}

function ChatView({
  agent,
  draft,
  onDraft,
  onSend,
  disabled,
  onHome,
  workspaceId,
  workspaceName,
  appName,
  modeName,
}: {
  agent: AgentApi
  draft: string
  onDraft: (value: string) => void
  onSend: () => void
  disabled: boolean
  onHome: () => void
  workspaceId: string
  workspaceName: string
  appName: string
  modeName: string
}) {
  const title = agent.state?.sessionName?.trim() || '新任务'
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [presentedOfficePath, setPresentedOfficePath] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<number, number>>({})
  const sessionFile = agent.state?.sessionFile

  useEffect(() => {
    if (!sessionFile) return
    api
      .get<{ feedback: { timestamp: number; rating: number }[] }>(
        `/api/feedback?session=${encodeURIComponent(sessionFile)}`,
      )
      .then((payload) => {
        const next: Record<number, number> = {}
        for (const item of payload.feedback) next[item.timestamp] = item.rating
        setFeedback(next)
      })
      .catch(() => {
        // Feedback is optional; a server without the endpoint just hides the row.
      })
  }, [sessionFile])

  const rate = async (timestamp: number, rating: number) => {
    if (!sessionFile) return
    try {
      await api.post('/api/feedback', { sessionFile, timestamp, rating })
      setFeedback((current) => ({ ...current, [timestamp]: rating }))
    } catch {
      // Keep the previous state; the user can try again.
    }
  }

  return (
    <div className="wb-chat">
      <header className="wb-chat-head">
        <button type="button" className="wb-icon-button" onClick={onHome} aria-label="返回首页">
          <Icon name="chevronLeft" size={19} />
        </button>
        <div className="wb-chat-title">
          <b>{title}</b>
          <span>
            <Icon name="house" size={12} />
            {workspaceName} · {appName} / {modeName}
          </span>
        </div>
        <span className="wb-spacer" />
        <button
          type="button"
          className={`wb-artifact-toggle${artifactsOpen ? ' is-active' : ''}`}
          onClick={() => setArtifactsOpen((value) => !value)}
          title="产物"
        >
          <Icon name="book" size={16} />
          产物
        </button>
        <button type="button" className="wb-icon-button" title="搜索（即将支持）" disabled>
          <Icon name="search" size={18} />
        </button>
      </header>

      <div className="wb-chat-body">
        <div className="wb-chat-main">
          <WbTranscript
            agent={agent}
            workspaceId={workspaceId}
            sessionFile={sessionFile}
            feedback={feedback}
            onFeedback={(timestamp, rating) => void rate(timestamp, rating)}
            onOpenPresented={(path) => {
              if (/\.(docx|xlsx|pptx)$/i.test(path)) setPresentedOfficePath(path)
              else setArtifactsOpen(true)
            }}
          />
          <div className="wb-chat-composer">
            <WbComposer
              value={draft}
              onChange={onDraft}
              onSend={onSend}
              onAbort={agent.abort}
              streaming={agent.isStreaming}
              disabled={disabled}
              placeholder="今天帮你做些什么？@ 添加上下文，/调用技能与指令"
              workspaceName={workspaceName}
              chat
            />
          </div>
        </div>
        {artifactsOpen ? (
          <ArtifactPane workspaceId={workspaceId} onClose={() => setArtifactsOpen(false)} />
        ) : null}
      </div>
      {presentedOfficePath ? (
        <OfficeEditor
          workspaceId={workspaceId}
          path={presentedOfficePath}
          onClose={() => setPresentedOfficePath(null)}
          onSaved={() => undefined}
        />
      ) : null}
    </div>
  )
}

interface ArtifactRow {
  path: string
  name: string
  kind: 'document' | 'sheet' | 'slides' | 'data' | 'image' | string
  extension: string
  size: number
  modified: number
  editable: boolean
}

function artifactIcon(kind: string): IconName {
  if (kind === 'image') return 'image'
  if (kind === 'slides') return 'slides'
  if (kind === 'sheet') return 'sheet'
  return 'file'
}

function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          field += '"'
          index += 1
        } else {
          quoted = false
        }
      } else {
        field += char
      }
      continue
    }
    if (char === '"') {
      quoted = true
    } else if (char === delimiter) {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (char !== '\r') {
      field += char
    }
  }
  row.push(field)
  if (row.length > 1 || row[0] !== '') rows.push(row)
  return rows
}

function serializeDelimited(rows: string[][], delimiter: string): string {
  return rows
    .map((row) =>
      row
        .map((field) =>
          field.includes(delimiter) || field.includes('"') || field.includes('\n')
            ? `"${field.replace(/"/g, '""')}"`
            : field,
        )
        .join(delimiter),
    )
    .join('\n')
}

function delimitedKind(extension: string): { delimiter: string; label: string } | null {
  if (extension === 'csv') return { delimiter: ',', label: 'CSV' }
  if (extension === 'tsv') return { delimiter: '\t', label: 'TSV' }
  return null
}

interface SlidePreviewRow {
  number: number
  texts: string[]
}

interface SheetPreviewRow {
  name: string
  rows: string[][]
}

function officeDocId(workspace: string, path: string): string {
  const bytes = new TextEncoder().encode(JSON.stringify({ w: workspace, p: path }))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function OfficeEditor({
  workspaceId,
  path,
  onClose,
  onSaved,
}: {
  workspaceId: string
  path: string
  onClose: () => void
  onSaved: () => void
}) {
  const docId = officeDocId(workspaceId, path)
  const lower = path.toLowerCase()
  const kind = lower.endsWith('.pptx') ? 'slides' : lower.endsWith('.xlsx') ? 'sheets' : ''
  const editorUrl = kind
    ? `/office/${kind}/?embed=1&open=${encodeURIComponent(docId)}`
    : `/office/?embed=1&doc=${encodeURIComponent(docId)}`
  const [phase, setPhase] = useState<'locking' | 'ready' | 'locked' | 'error'>('locking')
  const [message, setMessage] = useState('')
  const [saved, setSaved] = useState(false)
  const [versions, setVersions] = useState<{ id: string; size: number; created: number }[] | null>(null)
  const [showVersions, setShowVersions] = useState(false)
  const [nonce, setNonce] = useState(0)
  const tokenRef = useRef('')

  useEffect(() => {
    let cancelled = false
    let heartbeat = 0
    const release = () => {
      if (!tokenRef.current) return
      void fetch(`/api/office/documents/${docId}/lock`, {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: { 'X-Office-Token': tokenRef.current },
      })
      tokenRef.current = ''
    }
    const lock = async () => {
      const response = await fetch(`/api/office/documents/${docId}/lock`, {
        method: 'POST',
        credentials: 'same-origin',
      })
      if (cancelled) return
      if (response.status === 409) {
        setPhase('locked')
        setMessage('这份文档正在被另一位用户编辑。请稍后重试，或让管理员接管。')
        return
      }
      if (!response.ok) {
        setPhase('error')
        setMessage(`无法获取编辑锁（HTTP ${response.status}）。`)
        return
      }
      const data = (await response.json()) as { token: string }
      tokenRef.current = data.token
      setPhase('ready')
      heartbeat = window.setInterval(async () => {
        const response = await fetch(`/api/office/documents/${docId}/lock`, {
          method: 'POST',
          credentials: 'same-origin',
        })
        if (response.status === 409) {
          setPhase('locked')
          setMessage('编辑锁已过期或被接管，请重新打开。')
        } else if (response.ok) {
          tokenRef.current = ((await response.json()) as { token: string }).token
        }
      }, 60_000)
    }
    void lock()
    return () => {
      cancelled = true
      window.clearInterval(heartbeat)
      release()
    }
  }, [docId])

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return
      if (event.data?.type === 'office:saved') {
        setSaved(true)
        onSaved()
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [onSaved])

  const loadVersions = async () => {
    const response = await fetch(`/api/office/documents/${docId}/versions`, { credentials: 'same-origin' })
    if (!response.ok) return
    setVersions(((await response.json()) as { versions: { id: string; size: number; created: number }[] }).versions)
    setShowVersions(true)
  }

  const restore = async (version: string) => {
    const response = await fetch(`/api/office/documents/${docId}/versions/${encodeURIComponent(version)}/restore`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-Office-Token': tokenRef.current },
    })
    if (!response.ok) {
      setMessage(`恢复失败（HTTP ${response.status}）。`)
      return
    }
    setShowVersions(false)
    setVersions(null)
    setNonce((value) => value + 1)
    setSaved(false)
    onSaved()
  }

  return (
    <div className="wb-office-editor">
      <header>
        <b>在线编辑</b>
        {phase === 'ready' && saved ? <span>已保存</span> : null}
        {phase === 'ready' ? (
          <button type="button" className="wb-link" onClick={() => void loadVersions()}>
            版本
          </button>
        ) : null}
        <button type="button" className="wb-button" onClick={onClose}>关闭</button>
      </header>
      {phase === 'ready' ? (
        <iframe
          key={nonce}
          title="Office editor"
          src={editorUrl}
          sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-modals"
        />
      ) : (
        <div className="wb-office-status">
          <p>{phase === 'locking' ? '正在获取编辑锁…' : message}</p>
          {phase !== 'locking' ? <button type="button" className="wb-button" onClick={onClose}>关闭</button> : null}
        </div>
      )}
      {showVersions ? (
        <div className="wb-office-versions">
          <header>
            <b>历史版本</b>
            <button type="button" className="wb-link" onClick={() => setShowVersions(false)}>收起</button>
          </header>
          {versions?.length ? (
            <ul>
              {versions.map((version) => (
                <li key={version.id}>
                  <span>{new Date(version.created * 1000).toLocaleString()}</span>
                  <small>{(version.size / 1024).toFixed(0)} KB</small>
                  <button type="button" className="wb-link" onClick={() => void restore(version.id)}>恢复</button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="wb-muted">还没有历史版本；保存一次后会在这里出现。</p>
          )}
        </div>
      ) : null}
    </div>
  )
}

function ArtifactPane({ workspaceId, onClose }: { workspaceId: string; onClose: () => void }) {
  const [items, setItems] = useState<ArtifactRow[] | null>(null)
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [preview, setPreview] = useState<FilePreview | null>(null)
  const [text, setText] = useState('')
  const [savedText, setSavedText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tableRows, setTableRows] = useState<string[][]>([])
  const [tableMode, setTableMode] = useState(false)
  const [markdownMode, setMarkdownMode] = useState(false)
  const [slides, setSlides] = useState<SlidePreviewRow[] | null>(null)
  const [sheets, setSheets] = useState<SheetPreviewRow[] | null>(null)
  const [officePath, setOfficePath] = useState<string | null>(null)
  const [activeSheet, setActiveSheet] = useState(0)

  const load = () => {
    api
      .get<{ artifacts: ArtifactRow[] }>(
        `/api/artifacts?workspace=${encodeURIComponent(workspaceId)}`,
      )
      .then((payload) => {
        setItems(payload.artifacts)
        setSelectedPath((current) => {
          if (current && payload.artifacts.some((item) => item.path === current)) return current
          return payload.artifacts[0]?.path ?? null
        })
      })
      .catch((failure: unknown) => {
        setItems([])
        setError(failure instanceof Error ? failure.message : '读取产物失败')
      })
  }

  useEffect(load, [workspaceId])

  useEffect(() => {
    if (!selectedPath) {
      setPreview(null)
      return
    }
    setError(null)
    api
      .get<FilePreview>(
        `/api/workspace/file?path=${encodeURIComponent(selectedPath)}&workspace=${encodeURIComponent(workspaceId)}`,
      )
      .then((payload) => {
        setPreview(payload)
        const content = payload.text ?? ''
        setText(content)
        setSavedText(content)
        const extension = selectedPath.split('.').pop()?.toLowerCase() ?? ''
        const kind = delimitedKind(extension)
        setTableRows(kind ? parseDelimited(content, kind.delimiter) : [])
        setTableMode(Boolean(kind))
        setMarkdownMode(false)
      })
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : '预览失败'))
  }, [selectedPath, workspaceId])

  useEffect(() => {
    const item = items?.find((entry) => entry.path === selectedPath)
    setSlides(null)
    if (!item || item.kind !== 'slides' || item.extension !== 'pptx') return
    api
      .get<{ slides: SlidePreviewRow[] }>(
        `/api/artifacts/pptx?path=${encodeURIComponent(item.path)}&workspace=${encodeURIComponent(workspaceId)}`,
      )
      .then((payload) => setSlides(payload.slides))
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : '幻灯片预览失败'))
  }, [selectedPath, items, workspaceId])

  useEffect(() => {
    const item = items?.find((entry) => entry.path === selectedPath)
    setSheets(null)
    setActiveSheet(0)
    if (!item || item.kind !== 'sheet' || item.extension !== 'xlsx') return
    api
      .get<{ sheets: SheetPreviewRow[] }>(
        `/api/artifacts/xlsx?path=${encodeURIComponent(item.path)}&workspace=${encodeURIComponent(workspaceId)}`,
      )
      .then((payload) => setSheets(payload.sheets))
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : '表格预览失败'))
  }, [selectedPath, items, workspaceId])

  const selected = items?.find((item) => item.path === selectedPath) ?? null
  const dirty = text !== savedText
  const delimiter = selected ? delimitedKind(selected.extension)?.delimiter ?? ',' : ','

  const applyRows = (next: string[][]) => {
    setTableRows(next)
    setText(serializeDelimited(next, delimiter))
  }

  const updateCell = (rowIndex: number, columnIndex: number, value: string) => {
    applyRows(
      tableRows.map((row, currentRow) =>
        currentRow === rowIndex
          ? row.map((cell, currentColumn) => (currentColumn === columnIndex ? value : cell))
          : row,
      ),
    )
  }

  const save = async () => {
    if (!selected || !selected.editable) return
    setBusy(true)
    setError(null)
    try {
      await api.put(
        `/api/workspace/file?path=${encodeURIComponent(selected.path)}&workspace=${encodeURIComponent(workspaceId)}`,
        { content: text },
      )
      setSavedText(text)
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const download = selected
    ? `${resolveApiBase()}/api/workspace/download?path=${encodeURIComponent(selected.path)}&workspace=${encodeURIComponent(workspaceId)}`
    : ''

  return (
    <aside className="wb-artifacts">
      <header className="wb-artifacts-head">
        <b>产物</b>
        <span>{items?.length ?? 0}</span>
        <button type="button" className="wb-icon-button" onClick={onClose} aria-label="关闭产物">
          ×
        </button>
      </header>

      <div className="wb-artifact-list">
        {items === null ? (
          <p className="wb-muted">正在读取…</p>
        ) : items.length === 0 ? (
          <p className="wb-muted">这个空间还没有可预览的产物。生成文件后会自动出现在这里。</p>
        ) : (
          items.map((item) => (
            <button
              key={item.path}
              type="button"
              className={`wb-artifact-item${item.path === selectedPath ? ' is-active' : ''}`}
              onClick={() => setSelectedPath(item.path)}
              title={item.path}
            >
              <span className={`wb-file-icon is-${item.kind === 'sheet' ? 'data' : item.kind === 'slides' ? 'doc' : 'file'}`}>
                <Icon name={artifactIcon(item.kind)} size={16} />
              </span>
              <span>
                <b>{item.name}</b>
                <i>{item.kind === 'slides' ? '幻灯片' : item.kind === 'sheet' ? '表格' : item.extension.toUpperCase()}</i>
              </span>
            </button>
          ))
        )}
      </div>

      {selected ? (
        <div className="wb-artifact-editor">
          <div className="wb-artifact-editor-head">
            <b>{selected.name}</b>
            <a className="wb-link" href={download}>下载</a>
          </div>
          {error ? <div className="wb-alert">{error}</div> : null}
          {selected.kind === 'image' && preview ? (
            <div className="wb-artifact-image">
              <img src={download} alt={selected.name} />
            </div>
          ) : selected.kind === 'slides' && selected.extension === 'pptx' ? (
            <div className="wb-slide-preview">
              <button type="button" className="wb-button" onClick={() => setOfficePath(selected.path)}>
                在线编辑
              </button>
              {slides === null ? (
                <p className="wb-muted">正在读取幻灯片…</p>
              ) : (
                slides.map((slide) => (
                  <article key={slide.number} className="wb-slide-card">
                    <b>第 {slide.number} 页</b>
                    {slide.texts.length === 0 ? (
                      <p className="wb-muted">这一页没有文本。</p>
                    ) : (
                      <p>{slide.texts.join(' · ')}</p>
                    )}
                  </article>
                ))
              )}
            </div>
          ) : selected.kind === 'sheet' && selected.extension === 'xlsx' ? (
            <div className="wb-xlsx-preview">
              <button type="button" className="wb-button" onClick={() => setOfficePath(selected.path)}>
                在线编辑
              </button>
              {sheets === null ? (
                <p className="wb-muted">正在读取工作表…</p>
              ) : sheets.length === 0 ? (
                <p className="wb-muted">没有可读取的工作表。</p>
              ) : (
                <>
                  {sheets.length > 1 ? (
                    <div className="wb-sheet-tabs">
                      {sheets.map((sheet, index) => (
                        <button
                          key={`${sheet.name}-${index}`}
                          type="button"
                          className={index === activeSheet ? 'is-active' : ''}
                          onClick={() => setActiveSheet(index)}
                        >
                          {sheet.name}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <div className="wb-table-wrap">
                    <table className="wb-sheet is-readonly">
                      <tbody>
                        {(sheets[activeSheet]?.rows || []).map((row, rowIndex) => (
                          <tr key={rowIndex} className={rowIndex === 0 ? 'is-head' : ''}>
                            {row.map((cell, columnIndex) => (
                              <td key={columnIndex}>{cell || ' '}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {(sheets[activeSheet]?.rows || []).length === 0 ? (
                      <p className="wb-muted">这个工作表是空的。</p>
                    ) : null}
                  </div>
                </>
              )}
            </div>
          ) : selected.editable ? (
            <div className="wb-artifact-edit">
              <div className="wb-artifact-toolbar">
                {delimitedKind(selected.extension) ? (
                  <>
                    <button
                      type="button"
                      className={tableMode ? 'is-active' : ''}
                      onClick={() => setTableMode(true)}
                    >
                      表格
                    </button>
                    <button
                      type="button"
                      className={!tableMode ? 'is-active' : ''}
                      onClick={() => setTableMode(false)}
                    >
                      源码
                    </button>
                  </>
                ) : null}
                {selected.extension === 'md' || selected.extension === 'mdx' ? (
                  <button
                    type="button"
                    className={markdownMode ? 'is-active' : ''}
                    onClick={() => setMarkdownMode((value) => !value)}
                  >
                    {markdownMode ? '编辑' : '预览'}
                  </button>
                ) : null}
                {tableMode ? (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        const width = Math.max(1, ...tableRows.map((row) => row.length))
                        applyRows([...tableRows, Array.from({ length: width }, () => '')])
                      }}
                    >
                      + 行
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        const width = Math.max(0, ...tableRows.map((row) => row.length))
                        applyRows(tableRows.map((row) => [...row, '']).length ? tableRows.map((row) => [...row, '']) : [['']])
                        void width
                      }}
                    >
                      + 列
                    </button>
                    <button
                      type="button"
                      onClick={() => applyRows(tableRows.slice(0, -1))}
                      disabled={tableRows.length === 0}
                    >
                      − 行
                    </button>
                  </>
                ) : null}
              </div>

              {markdownMode ? (
                <div
                  className="wb-md wb-artifact-markdown"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
                />
              ) : tableMode ? (
                <div className="wb-table-wrap">
                  <table className="wb-sheet">
                    <tbody>
                      {tableRows.map((row, rowIndex) => (
                        <tr key={rowIndex} className={rowIndex === 0 ? 'is-head' : ''}>
                          {row.map((cell, columnIndex) => (
                            <td key={columnIndex}>
                              <input
                                value={cell}
                                onChange={(event) => updateCell(rowIndex, columnIndex, event.target.value)}
                                aria-label={`第 ${rowIndex + 1} 行第 ${columnIndex + 1} 列`}
                              />
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {tableRows.length === 0 ? <p className="wb-muted">空表格，点“+ 行”开始。</p> : null}
                </div>
              ) : (
                <textarea
                  className="wb-artifact-text"
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  spellCheck={false}
                  aria-label={`编辑 ${selected.name}`}
                />
              )}
            </div>
          ) : (
            <div className="wb-artifact-binary">
              <p className="wb-muted">
                {['docx', 'pptx'].includes(selected.extension)
                  ? '文档可以在线编辑；保存会写回当前工作空间。'
                  : '这个产物暂不支持在线预览，可下载后用对应工具打开。'}
              </p>
              {['docx', 'pptx'].includes(selected.extension) ? (
                <button type="button" className="wb-button" onClick={() => setOfficePath(selected.path)}>
                  在线编辑
                </button>
              ) : null}
              <a className="wb-button" href={download}>下载文件</a>
            </div>
          )}
          {selected.editable ? (
            <div className="wb-artifact-foot">
              <span>{dirty ? '有未保存修改' : '已保存'}</span>
              <button type="button" className="wb-button" onClick={() => void save()} disabled={!dirty || busy}>
                {busy ? '保存中…' : '保存'}
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="wb-artifact-editor wb-artifact-empty">
          <p className="wb-muted">选择一个产物查看。</p>
        </div>
      )}
      {officePath ? (
        <OfficeEditor
          workspaceId={workspaceId}
          path={officePath}
          onClose={() => setOfficePath(null)}
          onSaved={load}
        />
      ) : null}
    </aside>
  )
}

interface PresentedFile {
  path: string
  description: string
}

function parsePresentFiles(block: ContentBlock): PresentedFile[] {
  const record = asRecord(block)
  let args: unknown = record.arguments
  if (typeof record.argumentsRaw === 'string' && !args) {
    try {
      args = JSON.parse(record.argumentsRaw) as unknown
    } catch {
      args = null
    }
  }
  const rawFiles = asRecord(args).files
  if (!Array.isArray(rawFiles)) return []
  return rawFiles
    .map((item) => {
      const file = asRecord(item)
      return {
        path: typeof file.path === 'string' ? file.path : '',
        description: typeof file.description === 'string' ? file.description : '',
      }
    })
    .filter((file) => file.path.length > 0)
}

function collectPresented(transcript: TranscriptEntry[]): PresentedFile[] {
  const files = new Map<string, PresentedFile>()
  for (const entry of transcript) {
    if (entry.message.role !== 'assistant') continue
    for (const block of entry.message.content) {
      if (!isToolCallBlock(block) || block.name !== 'present') continue
      for (const file of parsePresentFiles(block)) {
        files.set(file.path, file)
      }
    }
  }
  return [...files.values()]
}

function PresentedFiles({
  files,
  workspaceId,
  onOpen,
}: {
  files: PresentedFile[]
  workspaceId: string
  onOpen: (path: string) => void
}) {
  return (
    <div className="wb-presented">
      <div className="wb-presented-title">
        <Icon name="book" size={14} />
        <span>交付文件</span>
      </div>
      {files.map((file) => {
        const name = file.path.split('/').filter(Boolean).pop() || file.path
        const extension = name.includes('.') ? name.split('.').pop()!.toUpperCase() : 'FILE'
        const office = /\.(docx|xlsx|pptx)$/i.test(name)
        const href = `/api/workspace/download?workspace=${encodeURIComponent(workspaceId)}&path=${encodeURIComponent(file.path)}`
        return (
          <div className="wb-presented-card" key={file.path}>
            <span className="wb-presented-icon"><Icon name={extension === 'PPTX' ? 'slides' : extension === 'XLSX' ? 'sheet' : 'file'} size={20} /></span>
            <span className="wb-presented-body">
              <b>{name}</b>
              <small>{file.description || extension}</small>
            </span>
            <button type="button" className="wb-button" onClick={() => onOpen(file.path)}>
              {office ? '在线编辑' : '查看'}
            </button>
            <a className="wb-link" href={href} download>下载</a>
          </div>
        )
      })}
    </div>
  )
}

function WbThinking({ text, running }: { text: string; running: boolean }) {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!running) setOpen(false)
  }, [running])
  const summary =
    text
      .split(/\n+/)
      .map((line) => line.replace(/[*#`>]/g, '').trim())
      .filter(Boolean)[0] || '思考过程'
  return (
    <div className={`wb-thinking${open ? ' is-open' : ''}`} data-running={running || undefined}>
      <button
        type="button"
        className="wb-thinking-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="spark" size={14} />
        <span>思考</span>
        <em>{running ? '思考中…' : '已完成'}</em>
        {!open ? <small>{summary}</small> : null}
        <Icon name="chevronDown" size={12} />
      </button>
      {open ? (
        <div className="wb-thinking-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />
      ) : null}
    </div>
  )
}

type ChatTurn = {
  key: string
  user?: TranscriptEntry
  entries: TranscriptEntry[]
  final?: TranscriptEntry
  start: number
  end: number
  running: boolean
}

function messageTime(entry: TranscriptEntry | undefined): number | undefined {
  const value = asRecord(entry?.message).timestamp
  return typeof value === 'number' ? value : undefined
}

function formatElapsed(milliseconds: number): string {
  const seconds = Math.max(1, Math.round(milliseconds / 1000))
  if (seconds >= 3600) {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    return `${hours}小时${minutes}分`
  }
  if (seconds >= 60) {
    return `${Math.floor(seconds / 60)}分${seconds % 60}秒`
  }
  return `${seconds}秒`
}

function buildTurns(entries: TranscriptEntry[], streaming: boolean): ChatTurn[] {
  const turns: ChatTurn[] = []
  let current: ChatTurn | null = null
  const push = (entry: TranscriptEntry) => {
    if (entry.message.role === 'user') {
      current = {
        key: entry.key,
        user: entry,
        entries: [],
        start: messageTime(entry) ?? Date.now(),
        end: messageTime(entry) ?? Date.now(),
        running: false,
      }
      turns.push(current)
      return
    }
    if (current === null) {
      current = {
        key: `turn-${entry.key}`,
        entries: [],
        start: messageTime(entry) ?? Date.now(),
        end: messageTime(entry) ?? Date.now(),
        running: false,
      }
      turns.push(current)
    }
    current.entries.push(entry)
    const time = messageTime(entry)
    if (time !== undefined) current.end = Math.max(current.end, time)
  }
  for (const entry of entries) push(entry)
  for (const [index, turn] of turns.entries()) {
    const finalIndex = [...turn.entries]
      .map((entry, position) => ({ entry, position }))
      .reverse()
      .find(({ entry }) =>
        entry.message.role === 'assistant'
        && entry.message.content.some((block) => isTextBlock(block) && block.text.trim().length > 0),
      )?.position
    if (finalIndex !== undefined) turn.final = turn.entries[finalIndex]
    turn.running = streaming && index === turns.length - 1
    if (turn.end < turn.start) turn.end = turn.start
  }
  return turns
}

function TurnProcess({
  turn,
  children,
  now,
}: {
  turn: ChatTurn
  children: ReactNode
  now: number
}) {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    setOpen(turn.running)
  }, [turn.running])
  const elapsed = formatElapsed((turn.running ? now : turn.end) - turn.start)
  const label = turn.running ? `处理中，用时 ${elapsed}` : `已完成，用时 ${elapsed}`
  return (
    <div className={`wb-turn-process${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="wb-turn-process-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{label}</span>
        <Icon name="chevronDown" size={14} />
      </button>
      {open ? <div className="wb-turn-process-body">{children}</div> : null}
    </div>
  )
}

function toolArgs(block: ContentBlock): Record<string, unknown> {
  const record = asRecord(block)
  if (record.arguments !== undefined && record.arguments !== null) return asRecord(record.arguments)
  if (typeof record.argumentsRaw === 'string') {
    try {
      return asRecord(JSON.parse(record.argumentsRaw) as unknown)
    } catch {
      return {}
    }
  }
  return {}
}

function toolDisplay(block: ContentBlock): { title: string; summary: string } {
  const args = toolArgs(block)
  const name = (block as { name?: string }).name || ''
  const value = (key: string) => (typeof args[key] === 'string' ? String(args[key]) : '')
  switch (name) {
    case 'bash':
      return { title: '运行命令', summary: value('command').split(/\n/)[0] || '...' }
    case 'read':
      return { title: '读取文件', summary: value('path') || '...' }
    case 'write':
      return { title: '写入文件', summary: value('path') || '...' }
    case 'edit':
      return { title: '编辑文件', summary: value('path') || '...' }
    case 'grep':
      return { title: '搜索', summary: value('pattern') || '...' }
    case 'find':
      return { title: '查找', summary: value('pattern') || '...' }
    case 'ls':
      return { title: '列出目录', summary: value('path') || '.' }
    case 'present':
      return { title: '交付文件', summary: '文件已在聊天末尾展示' }
    default:
      return { title: name || '工具', summary: '' }
  }
}

function toolStatus(execution?: ToolExecution): string {
  if (execution?.status === 'running') return '运行中'
  if (execution?.status === 'error' || execution?.isError) return '失败'
  return '完成'
}

function ProcessToolRow({
  block,
  execution,
}: {
  block: ContentBlock
  execution?: ToolExecution
}) {
  const { title, summary } = toolDisplay(block)
  const args = toolArgs(block)
  const argsText = Object.keys(args).length > 0 ? JSON.stringify(args, null, 2) : ''
  const resultText = toolResultText(execution?.result) || toolResultText(execution?.partialResult) || ''
  return (
    <details className={`wb-process-row is-${toolStatus(execution) === '失败' ? 'error' : execution?.status || 'done'}`}>
      <summary>
        <span className="wb-process-glyph"><Icon name={(block as { name?: string }).name === 'bash' ? 'code' : (block as { name?: string }).name === 'present' ? 'book' : 'file'} size={14} /></span>
        <b>{title}</b>
        <span className="wb-process-summary">{summary}</span>
        <em>{toolStatus(execution)}</em>
      </summary>
      {argsText ? <pre className="wb-process-pre">{argsText}</pre> : null}
      {resultText ? <pre className="wb-process-pre">{resultText}</pre> : null}
    </details>
  )
}

function ProcessToolResult({
  entry,
}: {
  entry: TranscriptEntry
}) {
  const message = entry.message
  const text = message.role === 'toolResult'
    ? message.content.filter(isTextBlock).map((block) => block.text).join('\n')
    : ''
  const name = message.role === 'toolResult' ? message.toolName || '工具' : '工具'
  if (!text.trim()) return null
  return (
    <details className={`wb-process-row${message.role === 'toolResult' && message.isError ? ' is-error' : ''}`}>
      <summary>
        <span className="wb-process-glyph"><Icon name="code" size={14} /></span>
        <b>{name}</b>
        <span className="wb-process-summary">结果</span>
        <em>{message.role === 'toolResult' && message.isError ? '失败' : '完成'}</em>
      </summary>
      <pre className="wb-process-pre">{text}</pre>
    </details>
  )
}

function ProcessEntries({
  turn,
  toolExecutions,
}: {
  turn: ChatTurn
  toolExecutions: Record<string, ToolExecution>
}) {
  return (
    <>
      {turn.entries.map((entry) => {
        const message = entry.message
        if (message.role === 'assistant') {
          const isFinal = entry.key === turn.final?.key
          return (
            <div className="wb-process-assistant" key={entry.key}>
              {message.content.map((block, index) => {
                if (isFinal && isTextBlock(block)) return null
                if (isThinkingBlock(block)) {
                  return <WbThinking key={`${entry.key}-think-${index}`} text={block.thinking} running={Boolean(entry.streaming)} />
                }
                if (isToolCallBlock(block)) {
                  if (block.name === 'present') return null
                  return <ProcessToolRow key={`${entry.key}-tool-${index}`} block={block} execution={toolExecutions[block.id]} />
                }
                if (isTextBlock(block)) {
                  if (!block.text.trim()) return null
                  return <div className="wb-process-text" key={`${entry.key}-text-${index}`} dangerouslySetInnerHTML={{ __html: renderMarkdown(block.text) }} />
                }
                return null
              })}
            </div>
          )
        }
        if (message.role === 'toolResult') return <ProcessToolResult key={entry.key} entry={entry} />
        if (message.role === 'custom') {
          const custom = message as CustomMessage
          const text = typeof custom.content === 'string' ? custom.content : ''
          return text ? <div className="wb-process-text" key={entry.key}>{text}</div> : null
        }
        return null
      })}
    </>
  )
}

function WbTranscript({
  agent,
  workspaceId,
  sessionFile,
  feedback,
  onFeedback,
  onOpenPresented,
}: {
  agent: AgentApi
  workspaceId: string
  sessionFile?: string
  feedback: Record<number, number>
  onFeedback: (timestamp: number, rating: number) => void
  onOpenPresented: (path: string) => void
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const presented = useMemo(() => {
    let lastUser = -1
    for (let index = agent.transcript.length - 1; index >= 0; index -= 1) {
      if (agent.transcript[index].message.role === 'user') {
        lastUser = index
        break
      }
    }
    return collectPresented(agent.transcript.slice(lastUser + 1))
  }, [agent.transcript])
  const inlineToolCalls = useMemo(() => {
    const ids = new Set<string>()
    for (const entry of agent.transcript) {
      if (entry.message.role !== 'assistant') continue
      for (const block of entry.message.content) {
        if (isToolCallBlock(block) && block.id) ids.add(block.id)
      }
    }
    return ids
  }, [agent.transcript])

  const presentIds = useMemo(() => {
    const ids = new Set<string>()
    for (const entry of agent.transcript) {
      if (entry.message.role !== 'assistant') continue
      for (const block of entry.message.content) {
        if (isToolCallBlock(block) && block.name === 'present' && block.id) ids.add(block.id)
      }
    }
    return ids
  }, [agent.transcript])

  const visible = useMemo(
    () =>
      agent.transcript.filter(
        (entry) =>
          !(entry.message.role === 'toolResult' && (inlineToolCalls.has(entry.message.toolCallId) || presentIds.has(entry.message.toolCallId))),
      ),
    [agent.transcript, inlineToolCalls, presentIds],
  )

  useEffect(() => {
    const node = scroller.current
    if (node && pinned.current) node.scrollTop = node.scrollHeight
  }, [visible, agent.toolExecutions])

  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!agent.isStreaming) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [agent.isStreaming])
  const turns = useMemo(() => buildTurns(visible, agent.isStreaming), [visible, agent.isStreaming])

  return (
    <div
      className="wb-messages"
      ref={scroller}
      onScroll={() => {
        const node = scroller.current
        if (!node) return
        pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80
      }}
    >
      {visible.length === 0 ? (
        <div className="wb-empty-chat">
          <p>还没有对话。</p>
          <p>在下方输入消息，或输入 / 选择技能开始。</p>
        </div>
      ) : (
        <div className="wb-thread">
          {turns.map((turn) => (
            <section className="wb-turn" key={turn.key}>
              {turn.user ? (
                <WbMessage
                  entry={turn.user}
                  toolExecutions={agent.toolExecutions}
                  streaming={false}
                  sessionFile={sessionFile}
                  feedback={feedback}
                  onFeedback={onFeedback}
                />
              ) : null}
              {turn.entries.length > 0 || turn.final ? (
                <TurnProcess turn={turn} now={now}>
                  <ProcessEntries turn={turn} toolExecutions={agent.toolExecutions} />
                </TurnProcess>
              ) : null}
              {turn.final ? (
                <WbMessage
                  key={`${turn.key}-final`}
                  entry={turn.final}
                  toolExecutions={agent.toolExecutions}
                  streaming={agent.isStreaming && turn.final.streaming}
                  sessionFile={sessionFile}
                  feedback={feedback}
                  onFeedback={onFeedback}
                  mode="answer"
                />
              ) : null}
            </section>
          ))}
          {presented.length > 0 ? (
            <PresentedFiles
              files={presented}
              workspaceId={workspaceId}
              onOpen={onOpenPresented}
            />
          ) : null}
        </div>
      )}
    </div>
  )
}

function WbMessage({
  entry,
  toolExecutions,
  streaming,
  sessionFile,
  feedback,
  onFeedback,
  mode = 'all',
  showFeedback = true,
}: {
  entry: TranscriptEntry
  toolExecutions: Record<string, ToolExecution>
  streaming: boolean
  sessionFile?: string
  feedback: Record<number, number>
  onFeedback: (timestamp: number, rating: number) => void
  mode?: 'all' | 'process' | 'answer'
  showFeedback?: boolean
}) {
  const message = entry.message
  if (message.role === 'user') {
    return <div className="wb-msg-user">{userText(message)}</div>
  }
  if (message.role === 'assistant') {
    const assistant = message as AssistantMessage
    const blocks = assistant.content.filter((block) => {
      if (mode === 'process') return !isTextBlock(block)
      if (mode === 'answer') return isTextBlock(block)
      return true
    })
    return (
      <div className={`wb-msg-ai${mode === 'answer' ? ' is-answer' : ''}`}>
        <img className="wb-msg-avatar" src="/pi-icon.png" alt="" />
        <div className="wb-msg-body">
          <div className="wb-msg-name">Pi <span>助手</span></div>
          <div className="wb-msg-content">
            {blocks.map((block, index) => (
              <WbBlock
                key={`${block.type}-${index}`}
                block={block}
                toolExecutions={toolExecutions}
                streaming={streaming}
              />
            ))}
            {blocks.length === 0 && streaming ? <span className="wb-caret" /> : null}
            {assistant.errorMessage ? <div className="wb-msg-error">{assistant.errorMessage}</div> : null}
          </div>
          {mode !== 'process' && usageLine(assistant.usage) ? (
            <div className="wb-msg-usage">{usageLine(assistant.usage)}</div>
          ) : null}
          {showFeedback && mode !== 'process' && sessionFile ? (
            <div className="wb-feedback">
              {[1, -1].map((rating) => (
                <button
                  key={rating}
                  type="button"
                  className={feedback[assistant.timestamp ?? 0] === rating ? 'is-active' : ''}
                  onClick={() => onFeedback(assistant.timestamp ?? 0, rating)}
                  title={rating === 1 ? '有帮助' : '没帮助'}
                  aria-label={rating === 1 ? '有帮助' : '没帮助'}
                >
                  {rating === 1 ? '👍' : '👎'}
                </button>
              ))}
              {feedback[assistant.timestamp ?? 0] ? <span>已反馈</span> : null}
            </div>
          ) : null}
        </div>
      </div>
    )
  }
  if (message.role === 'toolResult') {
    return (
      <div className="wb-msg-tool">
        <ToolCard
          toolCallId={message.toolCallId}
          toolName={message.toolName ?? '工具'}
          execution={toolExecutions[message.toolCallId]}
          fallbackText={message.content.filter(isTextBlock).map((part) => part.text).join('\n')}
          fallbackIsError={message.isError}
        />
      </div>
    )
  }
  if (message.role === 'custom') {
    const custom = message as CustomMessage
    const text =
      typeof custom.content === 'string'
        ? custom.content
        : (custom.content ?? []).filter(isTextBlock).map((part) => part.text).join('\n')
    return (
      <div className="wb-msg-notice">
        <b>{custom.customType === 'subagent' ? '子 agent' : '系统记录'}</b>
        <pre>{text}</pre>
      </div>
    )
  }
  const loose = asRecord(message)
  const summary = typeof loose.summary === 'string' ? loose.summary : ''
  if (!summary) return null
  return <div className="wb-msg-notice"><pre>{summary}</pre></div>
}

function WbBlock({
  block,
  toolExecutions,
  streaming,
}: {
  block: ContentBlock
  toolExecutions: Record<string, ToolExecution>
  streaming?: boolean
}) {
  if (isTextBlock(block)) {
    if (!block.text.trim()) return null
    return <div className="wb-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(block.text) }} />
  }
  if (isThinkingBlock(block)) {
    return <WbThinking text={block.thinking} running={Boolean(streaming)} />
  }
  if (isToolCallBlock(block)) {
    if (block.name === 'present') return null
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
  return <pre className="wb-block-unknown">{JSON.stringify(asRecord(block), null, 2)}</pre>
}

function WbComposer({
  value,
  onChange,
  onSend,
  onAbort,
  streaming,
  disabled,
  placeholder,
  workspaceName,
  chat = false,
}: {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onAbort: () => void
  streaming: boolean
  disabled: boolean
  placeholder: string
  workspaceName: string
  chat?: boolean
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const node = ref.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, 200)}px`
  }, [value])

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (streaming) return
      onSend()
    }
  }

  const ready = value.trim().length > 0

  return (
    <div className={`wb-composer${chat ? ' is-chat' : ''}`}>
      <textarea
        ref={ref}
        value={value}
        rows={1}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        disabled={disabled && !streaming}
      />
      <div className="wb-composer-bar">
        <button type="button" className="wb-plus" title="添加附件（即将支持）">
          <Icon name="plus" size={15} />
        </button>
        <span className="wb-ws-hint">
          <Icon name="house" size={13} />
          {workspaceName}
        </span>
        <span className="wb-spacer" />
        <button type="button" className="wb-icon-button" title="语音输入（即将支持）">
          <Icon name="mic" size={17} />
        </button>
        <button
          type="button"
          className={`wb-send${ready ? ' is-ready' : ''}${streaming ? ' is-stop' : ''}`}
          onClick={streaming ? onAbort : onSend}
          disabled={!streaming && (!ready || disabled)}
          aria-label={streaming ? '停止生成' : '发送'}
        >
          <Icon name={streaming ? 'stop' : 'arrowUp'} size={20} />
        </button>
      </div>
    </div>
  )
}

interface SkillRow {
  name: string
  description: string
  enabled: boolean
  disableModelInvocation: boolean
}

interface MarketItemRow {
  kind: 'skill' | 'plugin'
  id: string
  name: string
  description: string
  version: string
  author: string
  source: string
  tags: string[]
  installed: boolean
  rating: { average: number; count: number; mine: number | null }
}

interface MarketDetailRow extends MarketItemRow {
  readme: string
}

function MarketPanel({ kind, onInstalled }: { kind: 'skill' | 'plugin'; onInstalled: () => void }) {
  const [items, setItems] = useState<MarketItemRow[] | null>(null)
  const [canInstall, setCanInstall] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [tag, setTag] = useState('')
  const [sort, setSort] = useState<'rating' | 'name' | 'source'>('rating')
  const [tags, setTags] = useState<string[]>([])
  const [detail, setDetail] = useState<MarketDetailRow | null>(null)

  const load = () => {
    setItems(null)
    setError(null)
    api
      .get<{ items: MarketItemRow[]; tags: string[]; canInstall: boolean }>(
        `/api/market?kind=${kind}&q=${encodeURIComponent(query)}&tag=${encodeURIComponent(tag)}&sort=${sort}`,
      )
      .then((payload) => {
        setItems(payload.items)
        setTags(payload.tags ?? [])
        setCanInstall(payload.canInstall)
      })
      .catch((failure: unknown) => {
        setItems([])
        setError(failure instanceof Error ? failure.message : '读取市场失败')
      })
  }

  useEffect(load, [kind, query, tag, sort])

  const rate = async (item: MarketItemRow, score: number) => {
    setError(null)
    try {
      const payload = await api.post<{ average: number; count: number; mine: number }>(
        `/api/market/${kind}/${encodeURIComponent(item.id)}/rating`,
        { score },
      )
      setItems((current) =>
        current?.map((entry) =>
          entry.id === item.id
            ? { ...entry, rating: { average: payload.average, count: payload.count, mine: payload.mine } }
            : entry,
        ) ?? current,
      )
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '评分失败')
    }
  }

  const install = async (item: MarketItemRow) => {
    setBusy(item.id)
    setError(null)
    try {
      await api.post(`/api/market/${kind}s/${encodeURIComponent(item.id)}/install`)
      setItems((current) =>
        current?.map((entry) => (entry.id === item.id ? { ...entry, installed: true } : entry)) ?? current,
      )
      onInstalled()
    } catch (failure) {
      if (failure instanceof Error && failure.message.includes('409')) {
        setItems((current) =>
          current?.map((entry) => (entry.id === item.id ? { ...entry, installed: true } : entry)) ?? current,
        )
      } else {
        setError(failure instanceof Error ? failure.message : '安装失败')
      }
    } finally {
      setBusy(null)
    }
  }

  const openDetail = async (item: MarketItemRow) => {
    setError(null)
    try {
      const payload = await api.get<MarketDetailRow>(
        `/api/market/${kind}/${encodeURIComponent(item.id)}`,
      )
      setDetail(payload)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取详情失败')
    }
  }

  return (
    <div className="wb-market">
      <div className="wb-market-search">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={kind === 'skill' ? '搜索技能名称、标签、作者' : '搜索插件名称、标签、作者'}
          aria-label="搜索市场"
        />
        <select
          value={sort}
          onChange={(event) => setSort(event.target.value as 'rating' | 'name' | 'source')}
          aria-label="排序"
        >
          <option value="rating">按评分</option>
          <option value="name">按名称</option>
          <option value="source">按来源</option>
        </select>
      </div>
      {items && items.length > 0 ? (
        <div className="wb-market-tags">
          <button type="button" className={!tag ? 'is-active' : ''} onClick={() => setTag('')}>全部</button>
          {tags.map((name) => (
            <button key={name} type="button" className={tag === name ? 'is-active' : ''} onClick={() => setTag(name)}>
              {name}
            </button>
          ))}
        </div>
      ) : null}
      {error ? <div className="wb-alert">{error}</div> : null}
      {items === null ? (
        <div className="wb-empty"><b>正在读取市场…</b></div>
      ) : items.length === 0 ? (
        <div className="wb-empty"><b>市场暂时没有条目</b></div>
      ) : (
        <div className="wb-market-list">
          {items.map((item) => (
            <article key={item.id} className="wb-market-item">
              <span className="wb-market-icon"><Icon name={kind === 'skill' ? 'skill' : 'plugin'} size={20} /></span>
              <div className="wb-market-body">
                <div className="wb-market-title">
                  <b>{item.name}</b>
                  <span className="wb-source">{item.source}</span>
                  <i>v{item.version}</i>
                </div>
                <p>{item.description}</p>
                <div className="wb-market-meta">
                  <span>{item.author}</span>
                  {item.tags.map((tag) => <span key={tag} className="wb-tag">{tag}</span>)}
                  <button type="button" className="wb-link" onClick={() => void openDetail(item)}>
                    详情
                  </button>
                </div>
                <div className="wb-rating">
                  {[1, 2, 3, 4, 5].map((score) => (
                    <button
                      key={score}
                      type="button"
                      className={(item.rating.mine ?? 0) >= score ? 'is-on' : ''}
                      onClick={() => void rate(item, score)}
                      aria-label={`${score} 星`}
                    >
                      ★
                    </button>
                  ))}
                  <span>
                    {item.rating.average ? item.rating.average.toFixed(1) : '暂无评分'}
                    {item.rating.count ? `（${item.rating.count}）` : ''}
                  </span>
                </div>
              </div>
              <button
                type="button"
                className={`wb-button${item.installed ? ' is-installed' : ''}`}
                onClick={() => void install(item)}
                disabled={item.installed || !canInstall || busy === item.id}
                title={!canInstall ? '需要管理员安装' : undefined}
              >
                {item.installed ? '已安装' : busy === item.id ? '安装中…' : '安装'}
              </button>
            </article>
          ))}
        </div>
      )}

      {detail ? (
        <div className="wb-modal-scrim" onClick={() => setDetail(null)}>
          <div className="wb-modal" onClick={(event) => event.stopPropagation()}>
            <header className="wb-modal-head">
              <b>{detail.name}</b>
              <span className="wb-source">{detail.source}</span>
              <i className="wb-muted">v{detail.version}</i>
              <button type="button" className="wb-icon-button" onClick={() => setDetail(null)}>×</button>
            </header>
            <div className="wb-modal-body">
              <p className="wb-muted">{detail.description}</p>
              <div className="wb-market-meta">
                <span>{detail.author}</span>
                {detail.tags.map((tag) => <span key={tag} className="wb-tag">{tag}</span>)}
                <span>
                  评分 {detail.rating.average ? detail.rating.average.toFixed(1) : '暂无'}
                  {detail.rating.count ? `（${detail.rating.count}）` : ''}
                </span>
              </div>
              <pre className="wb-market-readme">{detail.readme || '暂无详细说明。'}</pre>
            </div>
            <footer className="wb-modal-foot">
              <span className="wb-spacer" />
              <button
                type="button"
                className={`wb-button${detail.installed ? ' is-installed' : ''}`}
                onClick={() => void install(detail)}
                disabled={detail.installed || !canInstall || busy === detail.id}
              >
                {detail.installed ? '已安装' : busy === detail.id ? '安装中…' : '安装'}
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function SkillsView({ workspace }: { workspace: string }) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null)
  const [view, setView] = useState<'installed' | 'market'>('installed')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = () => {
    setError(null)
    setSkills(null)
    api
      .get<{ skills: SkillRow[] }>(`/api/skills?workspace=${encodeURIComponent(workspace)}`)
      .then((payload) => setSkills(payload.skills))
      .catch((failure: unknown) => {
        setSkills([])
        setError(failure instanceof Error ? failure.message : '读取技能失败')
      })
  }

  useEffect(load, [workspace])

  const toggle = async (skill: SkillRow) => {
    setBusy(skill.name)
    setError(null)
    try {
      await api.patch(
        `/api/skills/${encodeURIComponent(skill.name)}?workspace=${encodeURIComponent(workspace)}`,
        { enabled: !skill.enabled },
      )
      setSkills((current) =>
        current?.map((item) =>
          item.name === skill.name ? { ...item, enabled: !item.enabled } : item,
        ) ?? current,
      )
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '切换技能失败')
    } finally {
      setBusy(null)
    }
  }

  const enabled = skills?.filter((skill) => skill.enabled).length ?? 0

  return (
    <div className="wb-page">
      <div className="wb-page-inner">
        <header className="wb-page-head">
          <div>
            <h2>技能</h2>
            <p>技能是 SKILL.md 表达的做事方法；启停只影响本账号后续请求的系统提示词。</p>
          </div>
          {view === 'installed' && skills ? <span className="wb-chip">{enabled} / {skills.length} 启用</span> : null}
          <div className="wb-tabs">
            <button type="button" className={view === 'installed' ? 'is-active' : ''} onClick={() => setView('installed')}>已安装</button>
            <button type="button" className={view === 'market' ? 'is-active' : ''} onClick={() => setView('market')}>市场</button>
          </div>
        </header>

        {view === 'market' ? (
          <MarketPanel kind="skill" onInstalled={load} />
        ) : (
          <>
        {error ? <div className="wb-alert">{error}</div> : null}

        {skills === null ? (
          <div className="wb-empty"><b>正在读取技能…</b></div>
        ) : skills.length === 0 ? (
          <div className="wb-empty">
            <b>还没有发现技能</b>
            <p>把 SKILL.md 放进工作区的 .agents/skills 或全局技能目录，重新连接后就会出现在这里。</p>
          </div>
        ) : (
          <div className="wb-skill-list">
            {skills.map((skill) => (
              <article key={skill.name} className="wb-skill">
                <span className="wb-skill-icon"><Icon name="skill" size={18} /></span>
                <div className="wb-skill-body">
                  <b>{skill.name}</b>
                  <p>{skill.description || '没有描述'}</p>
                </div>
                {skill.disableModelInvocation ? <span className="wb-chip">仅手动</span> : null}
                <button
                  type="button"
                  className={`wb-switch${skill.enabled ? ' is-on' : ''}`}
                  onClick={() => void toggle(skill)}
                  disabled={busy === skill.name}
                  aria-pressed={skill.enabled}
                  aria-label={`${skill.enabled ? '停用' : '启用'} ${skill.name}`}
                >
                  <span />
                </button>
              </article>
            ))}
          </div>
        )}

        <div className="wb-footnote">
          启停会在下一条消息生效；已打开的其他标签页可能需要刷新。
        </div>
          </>
        )}
      </div>
    </div>
  )
}

interface LibraryEntry {
  name: string
  path: string
  isDir: boolean
  size: number
  modified: number
  hidden: boolean
}

interface FilePreview {
  name: string
  path: string
  size: number
  binary: boolean
  truncated: boolean
  text: string | null
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function fileGlyph(entry: LibraryEntry): string {
  if (entry.isDir) return 'folder'
  const extension = entry.name.split('.').pop()?.toLowerCase() ?? ''
  if (['md', 'mdx', 'txt', 'doc', 'docx'].includes(extension)) return 'doc'
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg'].includes(extension)) return 'image'
  if (['json', 'csv', 'xlsx', 'xls'].includes(extension)) return 'data'
  return 'file'
}

function LibraryView({ workspace }: { workspace: string }) {
  const [path, setPath] = useState('/')
  const [entries, setEntries] = useState<LibraryEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<FilePreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [layout, setLayout] = useState<'grid' | 'list'>('grid')
  const uploadRef = useRef<HTMLInputElement>(null)

  const load = () => {
    setError(null)
    setEntries(null)
    api
      .get<{ entries: LibraryEntry[] }>(
        `/api/workspace/tree?path=${encodeURIComponent(path)}&workspace=${encodeURIComponent(workspace)}`,
      )
      .then((payload) => setEntries(payload.entries))
      .catch((failure: unknown) => {
        setEntries([])
        setError(failure instanceof Error ? failure.message : '读取资料库失败')
      })
  }

  useEffect(load, [path, workspace])

  const openEntry = (entry: LibraryEntry) => {
    if (entry.isDir) {
      setSelected(null)
      setPath(entry.path)
      return
    }
    api
      .get<FilePreview>(
        `/api/workspace/file?path=${encodeURIComponent(entry.path)}&workspace=${encodeURIComponent(workspace)}`,
      )
      .then(setSelected)
      .catch((failure: unknown) => setError(failure instanceof Error ? failure.message : '预览失败'))
  }

  const upload = async (file: File) => {
    setBusy(true)
    setError(null)
    try {
      const target = path === '/' ? `/${file.name}` : `${path}/${file.name}`
      await api.upload(
        `/api/workspace/file?path=${encodeURIComponent(target)}&workspace=${encodeURIComponent(workspace)}`,
        await file.arrayBuffer(),
      )
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '上传失败')
    } finally {
      setBusy(false)
    }
  }

  const createFolder = async () => {
    const name = window.prompt('目录名称')?.trim()
    if (!name) return
    try {
      await api.post(
        `/api/workspace/directory?workspace=${encodeURIComponent(workspace)}`,
        { parentPath: path, name },
      )
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '新建目录失败')
    }
  }

  const segments = path.split('/').filter(Boolean)

  return (
    <div className="wb-library">
      <div className="wb-lib-main">
        <header className="wb-page-head wb-lib-head">
          <div>
            <h2>资料库</h2>
            <p>当前空间的文件与产物；上传、预览和目录都在这里。</p>
          </div>
          <div className="wb-lib-actions">
            <button type="button" className="wb-button" onClick={createFolder}>
              新建目录
            </button>
            <button type="button" className="wb-button" onClick={() => uploadRef.current?.click()} disabled={busy}>
              {busy ? '上传中…' : '上传文件'}
            </button>
            <input
              ref={uploadRef}
              type="file"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void upload(file)
              }}
            />
          </div>
        </header>

        <div className="wb-lib-bar">
          <div className="wb-crumbs">
            <button type="button" onClick={() => { setSelected(null); setPath('/') }}>
              资料库
            </button>
            {segments.map((segment, index) => {
              const target = `/${segments.slice(0, index + 1).join('/')}`
              return (
                <span key={target}>
                  <i>/</i>
                  <button type="button" onClick={() => { setSelected(null); setPath(target) }}>
                    {segment}
                  </button>
                </span>
              )
            })}
          </div>
          <div className="wb-lib-layout">
            <button
              type="button"
              className={layout === 'grid' ? 'is-active' : ''}
              onClick={() => setLayout('grid')}
              title="网格"
            >
              <Icon name="apps" size={15} />
            </button>
            <button
              type="button"
              className={layout === 'list' ? 'is-active' : ''}
              onClick={() => setLayout('list')}
              title="列表"
            >
              <Icon name="panel" size={15} />
            </button>
          </div>
        </div>

        {error ? <div className="wb-alert">{error}</div> : null}

        {entries === null ? (
          <div className="wb-empty"><b>正在读取资料库…</b></div>
        ) : entries.length === 0 ? (
          <div className="wb-empty">
            <b>这个目录还是空的</b>
            <p>上传文件，或让 Pi 在工作区里生成内容。</p>
          </div>
        ) : (
          <div className={layout === 'grid' ? 'wb-lib-grid' : 'wb-lib-list'}>
            {entries.map((entry) => (
              <button
                key={entry.path}
                type="button"
                className={`wb-file${selected?.path === entry.path ? ' is-selected' : ''}`}
                onClick={() => openEntry(entry)}
                title={entry.path}
              >
                <span className={`wb-file-icon is-${fileGlyph(entry)}`}>
                  <Icon name={entry.isDir ? 'folder' : 'file'} size={18} />
                </span>
                <span className="wb-file-body">
                  <span className="wb-file-name">{entry.name}</span>
                  <span className="wb-file-meta">
                    {entry.isDir ? '目录' : formatBytes(entry.size)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {selected ? (
        <aside className="wb-lib-preview">
          <header>
            <b>{selected.name}</b>
            <button type="button" className="wb-icon-button" onClick={() => setSelected(null)} aria-label="关闭预览">
              ×
            </button>
          </header>
          <div className="wb-lib-preview-meta">
            {formatBytes(selected.size)} · {selected.path}
          </div>
          <div className="wb-lib-preview-body">
            {selected.binary ? (
              <p className="wb-muted">二进制文件，暂不支持预览。</p>
            ) : selected.truncated ? (
              <p className="wb-muted">文件过大，已截断预览。</p>
            ) : (
              <pre>{selected.text}</pre>
            )}
          </div>
          <a
            className="wb-button"
            href={`${resolveApiBase()}/api/workspace/download?path=${encodeURIComponent(selected.path)}&workspace=${encodeURIComponent(workspace)}`}
          >
            下载
          </a>
        </aside>
      ) : null}
    </div>
  )
}

function AppsView({
  apps,
  activeAppId,
  canManage,
  onSaved,
  onSelect,
}: {
  apps: AppRow[]
  activeAppId: string
  canManage: boolean
  onSaved: () => void
  onSelect: (app: AppRow) => void
}) {
  const [draft, setDraft] = useState<AppRow | null>(null)
  const [auditFor, setAuditFor] = useState<{ app: AppRow; items: AppAuditRow[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const showAudit = async (app: AppRow) => {
    setError(null)
    try {
      const payload = await api.get<{ audit: AppAuditRow[] }>(
        `/api/apps/${encodeURIComponent(app.id)}/audit`,
      )
      setAuditFor({ app, items: payload.audit })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取变更记录失败')
    }
  }

  const restore = async (app: AppRow) => {
    if (!window.confirm(`恢复「${app.name}」的内置默认配置？当前修改会被覆盖。`)) return
    setBusy(true)
    setError(null)
    try {
      await api.post(`/api/apps/${encodeURIComponent(app.id)}/restore`)
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '恢复默认失败')
    } finally {
      setBusy(false)
    }
  }

  const openNew = () => {
    const modeId = `mode-${Math.random().toString(36).slice(2, 7)}`
    setDraft({
      id: '',
      name: '新应用',
      description: '',
      accent: '#3b5fba',
      builtin: false,
      modes: [
        {
          id: modeId,
          name: '默认模式',
          description: '',
          prompt: '',
          skillBinding: 'all',
          capsules: [],
        },
      ],
    })
    setError(null)
  }

  const openEdit = (app: AppRow) => {
    setDraft(JSON.parse(JSON.stringify(app)) as AppRow)
    setError(null)
  }

  const save = async () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      if (draft.id) {
        await api.put(`/api/apps/${encodeURIComponent(draft.id)}`, draft)
      } else {
        await api.post('/api/apps', draft)
      }
      setDraft(null)
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '保存应用失败')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!draft?.id || draft.builtin) return
    if (!window.confirm(`删除应用「${draft.name}」？`)) return
    setBusy(true)
    try {
      await api.del(`/api/apps/${encodeURIComponent(draft.id)}`)
      setDraft(null)
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '删除应用失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wb-page">
      <div className="wb-page-inner">
        <header className="wb-page-head">
          <div>
            <h2>应用</h2>
            <p>应用把工作模式和场景胶囊组合起来；选中后，首页与下一条消息都会使用它的提示词与技能绑定。</p>
          </div>
          {canManage ? (
            <button type="button" className="wb-button" onClick={openNew}>新建应用</button>
          ) : (
            <span className="wb-chip">{apps.length} 个应用</span>
          )}
        </header>

        <div className="wb-apps-grid">
          {apps.map((app) => {
            const active = app.id === activeAppId
            return (
              <article key={app.id} className={`wb-app-card${active ? ' is-active' : ''}`}>
                <div className="wb-app-card-head">
                  <span className="wb-app-icon" style={{ background: app.accent }}>
                    {app.name.slice(0, 1)}
                  </span>
                  <div>
                    <b>{app.name} {app.builtin ? <i className="wb-builtin">内置</i> : null}</b>
                    <p>{app.description || '没有描述'}</p>
                  </div>
                  {active ? <span className="wb-chip">当前</span> : null}
                </div>

                <div className="wb-app-modes">
                  {app.modes.map((mode) => (
                    <span key={mode.id} title={mode.description}>
                      {mode.name}
                      <i>{mode.capsules.length}</i>
                    </span>
                  ))}
                </div>

                <div className="wb-app-card-foot">
                  <span className="wb-muted">
                    {app.modes.length} 个工作模式 ·{' '}
                    {app.modes.reduce((total, mode) => total + mode.capsules.length, 0)} 个场景胶囊
                  </span>
                  {canManage ? (
                    <button type="button" className="wb-button" onClick={() => openEdit(app)}>
                      编辑
                    </button>
                  ) : null}
                  <button type="button" className="wb-button" onClick={() => onSelect(app)}>
                    {active ? '进入工作台' : '使用'}
                  </button>
                </div>
                <div className="wb-app-links">
                  <button type="button" className="wb-link" onClick={() => void showAudit(app)}>
                    变更记录
                  </button>
                  {app.builtin && canManage ? (
                    <button type="button" className="wb-link" onClick={() => void restore(app)} disabled={busy}>
                      恢复默认
                    </button>
                  ) : null}
                </div>
              </article>
            )
          })}
        </div>

        <div className="wb-footnote">
          应用配置保存在当前租户；修改后会重启该应用的 worker，下一条消息使用新提示词。
        </div>

        {auditFor ? (
          <div className="wb-modal-scrim" onClick={() => setAuditFor(null)}>
            <div className="wb-modal wb-audit-modal" onClick={(event) => event.stopPropagation()}>
              <header className="wb-modal-head">
                <b>变更记录：{auditFor.app.name}</b>
                <button type="button" className="wb-icon-button" onClick={() => setAuditFor(null)}>×</button>
              </header>
              <div className="wb-modal-body">
                {auditFor.items.length === 0 ? (
                  <p className="wb-muted">还没有变更记录。</p>
                ) : (
                  <div className="wb-audit-list">
                    {auditFor.items.map((item) => (
                      <div key={item.id} className="wb-audit-row">
                        <span className={`wb-audit-action is-${item.action}`}>{APP_ACTION_LABEL[item.action] || item.action}</span>
                        <b>{item.actor || '未知'}</b>
                        <time>{formatDateTime(item.createdAt)}</time>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        ) : null}

        {draft ? (
          <AppEditor
            draft={draft}
            error={error}
            busy={busy}
            onChange={setDraft}
            onCancel={() => setDraft(null)}
            onSave={() => void save()}
            onDelete={draft.builtin ? undefined : () => void remove()}
          />
        ) : null}
      </div>
    </div>
  )
}

function nextId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 7)}`
}

function AppEditor({
  draft,
  error,
  busy,
  onChange,
  onCancel,
  onSave,
  onDelete,
}: {
  draft: AppRow
  error: string | null
  busy: boolean
  onChange: (app: AppRow) => void
  onCancel: () => void
  onSave: () => void
  onDelete?: () => void
}) {
  const updateMode = (index: number, patch: Partial<AppModeRow>) => {
    onChange({
      ...draft,
      modes: draft.modes.map((mode, current) => (current === index ? { ...mode, ...patch } : mode)),
    })
  }

  const updateCapsule = (modeIndex: number, capsuleIndex: number, patch: Partial<AppCapsule>) => {
    const mode = draft.modes[modeIndex]
    updateMode(modeIndex, {
      capsules: mode.capsules.map((capsule, current) =>
        current === capsuleIndex ? { ...capsule, ...patch } : capsule,
      ),
    })
  }

  return (
    <div className="wb-modal-scrim" onClick={onCancel}>
      <div className="wb-modal" onClick={(event) => event.stopPropagation()}>
        <header className="wb-modal-head">
          <b>{draft.id ? `编辑：${draft.name}` : '新建应用'}</b>
          <button type="button" className="wb-icon-button" onClick={onCancel} aria-label="关闭">×</button>
        </header>

        <div className="wb-modal-body">
          {error ? <div className="wb-alert">{error}</div> : null}
          <div className="wb-form-grid">
            <label className="wb-field">
              <span>名称</span>
              <input value={draft.name} onChange={(event) => onChange({ ...draft, name: event.target.value })} />
            </label>
            <label className="wb-field">
              <span>标识色</span>
              <input
                type="color"
                value={draft.accent}
                onChange={(event) => onChange({ ...draft, accent: event.target.value })}
              />
            </label>
            <label className="wb-field">
              <span>描述</span>
              <input
                value={draft.description}
                onChange={(event) => onChange({ ...draft, description: event.target.value })}
              />
            </label>
          </div>

          <div className="wb-modes-editor">
            {draft.modes.map((mode, modeIndex) => {
              const customBinding = mode.skillBinding !== 'all'
              return (
                <section key={mode.id} className="wb-mode-editor">
                  <header>
                    <input
                      className="wb-mode-name"
                      value={mode.name}
                      onChange={(event) => updateMode(modeIndex, { name: event.target.value })}
                      aria-label="模式名称"
                    />
                    <button
                      type="button"
                      className="wb-link wb-link--danger"
                      onClick={() => onChange({
                        ...draft,
                        modes: draft.modes.filter((_item, current) => current !== modeIndex),
                      })}
                      disabled={draft.modes.length <= 1}
                    >
                      删除模式
                    </button>
                  </header>

                  <label className="wb-field">
                    <span>模式提示词</span>
                    <textarea
                      rows={4}
                      value={mode.prompt || ''}
                      onChange={(event) => updateMode(modeIndex, { prompt: event.target.value })}
                      placeholder="这个模式如何工作、输出要求、边界"
                    />
                  </label>

                  <div className="wb-form-grid">
                    <label className="wb-field">
                      <span>技能绑定</span>
                      <select
                        value={customBinding ? 'custom' : 'all'}
                        onChange={(event) =>
                          updateMode(modeIndex, {
                            skillBinding: event.target.value === 'all' ? 'all' : [],
                          })
                        }
                      >
                        <option value="all">全部技能</option>
                        <option value="custom">只绑定指定技能</option>
                      </select>
                    </label>
                    {customBinding ? (
                      <label className="wb-field">
                        <span>技能名称（逗号分隔）</span>
                        <input
                          value={(mode.skillBinding as string[]).join(', ')}
                          onChange={(event) =>
                            updateMode(modeIndex, {
                              skillBinding: event.target.value.split(',').map((item) => item.trim()).filter(Boolean),
                            })
                          }
                        />
                      </label>
                    ) : null}
                  </div>

                  <div className="wb-capsules-editor">
                    <div className="wb-capsules-head">
                      <span>场景胶囊</span>
                      <button
                        type="button"
                        className="wb-link"
                        onClick={() =>
                          updateMode(modeIndex, {
                            capsules: [
                              ...mode.capsules,
                              { id: nextId('capsule'), name: '新胶囊', prompt: '' },
                            ],
                          })
                        }
                      >
                        + 添加
                      </button>
                    </div>
                    {mode.capsules.map((capsule, capsuleIndex) => (
                      <div key={capsule.id} className="wb-capsule-row">
                        <input
                          value={capsule.name}
                          onChange={(event) => updateCapsule(modeIndex, capsuleIndex, { name: event.target.value })}
                          aria-label="胶囊名称"
                        />
                        <input
                          value={capsule.prompt}
                          onChange={(event) => updateCapsule(modeIndex, capsuleIndex, { prompt: event.target.value })}
                          aria-label="胶囊 prompt"
                          placeholder="点击后填入输入框的完整 prompt"
                        />
                        <button
                          type="button"
                          className="wb-icon-button"
                          onClick={() =>
                            updateMode(modeIndex, {
                              capsules: mode.capsules.filter((_item, current) => current !== capsuleIndex),
                            })
                          }
                          aria-label="删除胶囊"
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                </section>
              )
            })}
          </div>

          <button
            type="button"
            className="wb-button"
            onClick={() =>
              onChange({
                ...draft,
                modes: [
                  ...draft.modes,
                  {
                    id: nextId('mode'),
                    name: `模式 ${draft.modes.length + 1}`,
                    description: '',
                    prompt: '',
                    skillBinding: 'all',
                    capsules: [],
                  },
                ],
              })
            }
          >
            + 添加模式
          </button>
        </div>

        <footer className="wb-modal-foot">
          {onDelete ? (
            <button type="button" className="wb-link wb-link--danger" onClick={onDelete}>
              删除应用
            </button>
          ) : null}
          <span className="wb-spacer" />
          <button type="button" className="wb-button" onClick={onCancel}>取消</button>
          <button type="button" className="wb-button" onClick={onSave} disabled={busy}>
            {busy ? '保存中…' : '保存'}
          </button>
        </footer>
      </div>
    </div>
  )
}

interface PluginRow {
  id: string
  name: string
  version: string
  description: string
  capabilities: string[]
  enabled: boolean
}

function PluginsView({ appId }: { appId: string }) {
  const [plugins, setPlugins] = useState<PluginRow[] | null>(null)
  const [view, setView] = useState<'installed' | 'market'>('installed')
  const [canManage, setCanManage] = useState(false)
  const [canInstall, setCanInstall] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const uploadRef = useRef<HTMLInputElement>(null)

  const load = () => {
    setPlugins(null)
    setError(null)
    api
      .get<{ plugins: PluginRow[]; canManage: boolean; canInstall: boolean }>(
        `/api/plugins?app=${encodeURIComponent(appId)}`,
      )
      .then((payload) => {
        setPlugins(payload.plugins)
        setCanManage(payload.canManage)
        setCanInstall(payload.canInstall)
      })
      .catch((failure: unknown) => {
        setPlugins([])
        setError(failure instanceof Error ? failure.message : '读取插件失败')
      })
  }

  useEffect(load, [appId])

  const install = async (file: File) => {
    setBusy('install')
    setError(null)
    try {
      await api.upload('/api/plugins/install', await file.arrayBuffer())
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '安装失败')
    } finally {
      setBusy(null)
    }
  }

  const uninstall = async (plugin: PluginRow) => {
    if (!window.confirm(`卸载插件「${plugin.name}」？目录和所有租户的启用记录都会删除。`)) return
    setBusy(plugin.id)
    setError(null)
    try {
      await api.del(`/api/plugins/${encodeURIComponent(plugin.id)}`)
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '卸载失败')
    } finally {
      setBusy(null)
    }
  }

  const toggle = async (plugin: PluginRow) => {
    setBusy(plugin.id)
    setError(null)
    try {
      await api.patch(
        `/api/plugins/${encodeURIComponent(plugin.id)}/enable?app=${encodeURIComponent(appId)}`,
        { enabled: !plugin.enabled, appId },
      )
      setPlugins((current) =>
        current?.map((item) =>
          item.id === plugin.id ? { ...item, enabled: !item.enabled } : item,
        ) ?? current,
      )
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '切换插件失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="wb-page">
      <div className="wb-page-inner">
        <header className="wb-page-head">
          <div>
            <h2>插件</h2>
            <p>用 python-pi 自己的 Python SDK 开发的代码插件；按应用启用，新会话加载对应工具。</p>
          </div>
          {view === 'installed' && plugins ? <span className="wb-chip">{plugins.filter((item) => item.enabled).length} / {plugins.length} 启用</span> : null}
          <div className="wb-tabs">
            <button type="button" className={view === 'installed' ? 'is-active' : ''} onClick={() => setView('installed')}>已安装</button>
            <button type="button" className={view === 'market' ? 'is-active' : ''} onClick={() => setView('market')}>市场</button>
          </div>
          {view === 'installed' && canInstall ? (
            <>
              <button type="button" className="wb-button" onClick={() => uploadRef.current?.click()} disabled={busy === 'install'}>
                {busy === 'install' ? '安装中…' : '安装插件'}
              </button>
              <input
                ref={uploadRef}
                type="file"
                accept=".zip,application/zip"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) void install(file)
                }}
              />
            </>
          ) : null}
        </header>

        {view === 'market' ? (
          <MarketPanel kind="plugin" onInstalled={load} />
        ) : (
          <>
        {error ? <div className="wb-alert">{error}</div> : null}

        {plugins === null ? (
          <div className="wb-empty"><b>正在读取插件…</b></div>
        ) : plugins.length === 0 ? (
          <div className="wb-empty">
            <b>还没有安装插件</b>
            <p>
              把插件目录放到部署的 <code>&lt;data_root&gt;/plugins</code> 下，
              结构参考 examples/plugins/hello-pi。
            </p>
            <ul className="wb-plan">
              <li>plugin.json</li>
              <li>main.py · setup(api)</li>
              <li>按应用启停</li>
            </ul>
          </div>
        ) : (
          <div className="wb-plugin-list">
            {plugins.map((plugin) => (
              <article key={plugin.id} className="wb-plugin">
                <span className="wb-plugin-icon"><Icon name="plugin" size={19} /></span>
                <div className="wb-plugin-body">
                  <b>{plugin.name} <i>v{plugin.version}</i></b>
                  <p>{plugin.description || '没有描述'}</p>
                  <div className="wb-plugin-caps">
                    {plugin.capabilities.length === 0 ? <span>tools</span> : plugin.capabilities.map((cap) => <span key={cap}>{cap}</span>)}
                  </div>
                </div>
                {canInstall ? (
                  <button
                    type="button"
                    className="wb-link wb-link--danger"
                    onClick={() => void uninstall(plugin)}
                    disabled={busy === plugin.id}
                  >
                    卸载
                  </button>
                ) : null}
                <button
                  type="button"
                  className={`wb-switch${plugin.enabled ? ' is-on' : ''}`}
                  onClick={() => void toggle(plugin)}
                  disabled={!canManage || busy === plugin.id}
                  aria-pressed={plugin.enabled}
                  title={canManage ? '启用/停用' : '单机模式不支持持久化启停'}
                >
                  <span />
                </button>
              </article>
            ))}
          </div>
        )}

        <div className="wb-footnote">
          插件与 server 同进程执行，只安装可信代码；启停会在下一条消息生效。上传同 id 的 zip 会覆盖更新。
        </div>
          </>
        )}
      </div>
    </div>
  )
}


interface ScheduleRow {
  id: string
  name: string
  prompt: string
  workspace: string
  appId: string
  modeId: string
  kind: 'once' | 'interval' | 'daily' | 'weekly'
  runAt: string | null
  hour: number | null
  minute: number | null
  weekday: number | null
  intervalMinutes: number | null
  enabled: boolean
  nextRunAt: string | null
  lastRunAt: string | null
  lastStatus: string | null
  lastError: string | null
  createdAt: string
}

interface ScheduleRun {
  id: string
  startedAt: string
  finishedAt: string | null
  status: string
  sessionFile: string | null
  error: string | null
}

const SCHEDULE_KIND_LABEL: Record<string, string> = {
  once: '一次性',
  interval: '间隔',
  daily: '每天',
  weekly: '每周',
}

function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', {
        month: 'numeric',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
}

function ScheduledView({
  workspaces,
  apps,
  defaultWorkspace,
  defaultAppId,
  defaultModeId,
}: {
  workspaces: WorkspaceRow[]
  apps: AppRow[]
  defaultWorkspace: string
  defaultAppId: string
  defaultModeId: string
}) {
  const [schedules, setSchedules] = useState<ScheduleRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [runs, setRuns] = useState<Record<string, ScheduleRun[]>>({})
  const [form, setForm] = useState({
    name: '',
    prompt: '',
    kind: 'interval' as ScheduleRow['kind'],
    workspace: defaultWorkspace,
    appId: defaultAppId,
    modeId: defaultModeId,
    intervalMinutes: 60,
    time: '09:00',
    weekday: 1,
    date: '',
  })

  const load = () => {
    setSchedules(null)
    api
      .get<{ schedules: ScheduleRow[] }>('/api/schedules')
      .then((payload) => setSchedules(payload.schedules))
      .catch((failure: unknown) => {
        setSchedules([])
        setError(failure instanceof Error ? failure.message : '读取定时任务失败')
      })
  }

  useEffect(load, [])

  const activeFormApp = apps.find((app) => app.id === form.appId) || apps[0]
  const formMode = activeFormApp?.modes.find((mode) => mode.id === form.modeId) || activeFormApp?.modes[0]

  const create = async () => {
    setError(null)
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      prompt: form.prompt.trim(),
      kind: form.kind,
      workspace: form.workspace,
      appId: form.appId,
      modeId: formMode?.id || form.modeId,
    }
    if (form.kind === 'once') {
      if (!form.date) {
        setError('请选择执行时间')
        return
      }
      payload.runAt = new Date(form.date).toISOString()
    } else if (form.kind === 'interval') {
      payload.intervalMinutes = Number(form.intervalMinutes)
    } else {
      const [hour, minute] = form.time.split(':').map(Number)
      payload.hour = hour
      payload.minute = minute
      if (form.kind === 'weekly') payload.weekday = Number(form.weekday)
    }
    setBusy('create')
    try {
      await api.post('/api/schedules', payload)
      setShowForm(false)
      setForm((current) => ({ ...current, name: '', prompt: '' }))
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '创建定时任务失败')
    } finally {
      setBusy(null)
    }
  }

  const toggle = async (schedule: ScheduleRow) => {
    setBusy(schedule.id)
    try {
      await api.patch(`/api/schedules/${schedule.id}`, { enabled: !schedule.enabled })
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '切换失败')
    } finally {
      setBusy(null)
    }
  }

  const runNow = async (schedule: ScheduleRow) => {
    setBusy(schedule.id)
    try {
      await api.post(`/api/schedules/${schedule.id}/run`)
      setError(null)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '触发失败')
    } finally {
      setBusy(null)
    }
  }

  const remove = async (schedule: ScheduleRow) => {
    if (!window.confirm(`删除「${schedule.name}」？运行历史也会删除。`)) return
    try {
      await api.del(`/api/schedules/${schedule.id}`)
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '删除失败')
    }
  }

  const history = async (schedule: ScheduleRow) => {
    if (runs[schedule.id]) {
      setRuns((current) => {
        const next = { ...current }
        delete next[schedule.id]
        return next
      })
      return
    }
    try {
      const payload = await api.get<{ runs: ScheduleRun[] }>(`/api/schedules/${schedule.id}/runs`)
      setRuns((current) => ({ ...current, [schedule.id]: payload.runs }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取运行历史失败')
    }
  }

  return (
    <div className="wb-page">
      <div className="wb-page-inner">
        <header className="wb-page-head">
          <div>
            <h2>定时任务</h2>
            <p>按计划自动运行 prompt；可以指定空间、应用和模式，结果会记录在运行历史里。</p>
          </div>
          <button type="button" className="wb-button" onClick={() => setShowForm((value) => !value)}>
            {showForm ? '收起' : '新建定时任务'}
          </button>
        </header>

        {error ? <div className="wb-alert">{error}</div> : null}

        {showForm ? (
          <div className="wb-schedule-form">
            <div className="wb-form-grid">
              <label className="wb-field">
                <span>名称</span>
                <input
                  value={form.name}
                  onChange={(event) => setForm({ ...form, name: event.target.value })}
                  placeholder="例如：每天早报"
                />
              </label>
              <label className="wb-field">
                <span>空间</span>
                <select
                  value={form.workspace}
                  onChange={(event) => setForm({ ...form, workspace: event.target.value })}
                >
                  {workspaces.map((workspace) => (
                    <option key={workspace.id} value={workspace.id}>{workspace.name}</option>
                  ))}
                </select>
              </label>
              <label className="wb-field">
                <span>应用</span>
                <select
                  value={form.appId}
                  onChange={(event) => {
                    const app = apps.find((item) => item.id === event.target.value)
                    setForm({
                      ...form,
                      appId: event.target.value,
                      modeId: app?.modes[0]?.id || form.modeId,
                    })
                  }}
                >
                  {apps.map((app) => <option key={app.id} value={app.id}>{app.name}</option>)}
                </select>
              </label>
              <label className="wb-field">
                <span>模式</span>
                <select
                  value={formMode?.id || form.modeId}
                  onChange={(event) => setForm({ ...form, modeId: event.target.value })}
                >
                  {activeFormApp?.modes.map((mode) => (
                    <option key={mode.id} value={mode.id}>{mode.name}</option>
                  ))}
                </select>
              </label>
              <label className="wb-field">
                <span>频率</span>
                <select
                  value={form.kind}
                  onChange={(event) => setForm({ ...form, kind: event.target.value as ScheduleRow['kind'] })}
                >
                  <option value="once">一次性</option>
                  <option value="interval">间隔</option>
                  <option value="daily">每天</option>
                  <option value="weekly">每周</option>
                </select>
              </label>
              {form.kind === 'once' ? (
                <label className="wb-field">
                  <span>执行时间</span>
                  <input
                    type="datetime-local"
                    value={form.date}
                    onChange={(event) => setForm({ ...form, date: event.target.value })}
                  />
                </label>
              ) : null}
              {form.kind === 'interval' ? (
                <label className="wb-field">
                  <span>间隔（分钟）</span>
                  <input
                    type="number"
                    min={1}
                    max={10080}
                    value={form.intervalMinutes}
                    onChange={(event) => setForm({ ...form, intervalMinutes: Number(event.target.value) })}
                  />
                </label>
              ) : null}
              {form.kind === 'daily' || form.kind === 'weekly' ? (
                <label className="wb-field">
                  <span>时间</span>
                  <input
                    type="time"
                    value={form.time}
                    onChange={(event) => setForm({ ...form, time: event.target.value })}
                  />
                </label>
              ) : null}
              {form.kind === 'weekly' ? (
                <label className="wb-field">
                  <span>星期</span>
                  <select
                    value={form.weekday}
                    onChange={(event) => setForm({ ...form, weekday: Number(event.target.value) })}
                  >
                    {['周一', '周二', '周三', '周四', '周五', '周六', '周日'].map((label, index) => (
                      <option key={label} value={index}>{label}</option>
                    ))}
                  </select>
                </label>
              ) : null}
            </div>
            <label className="wb-field">
              <span>Prompt</span>
              <textarea
                rows={3}
                value={form.prompt}
                onChange={(event) => setForm({ ...form, prompt: event.target.value })}
                placeholder="到点后要执行的内容"
              />
            </label>
            <div className="wb-form-foot">
              <span className="wb-muted">时间按服务器本地时区解释；运行结果在“历史”里查看。</span>
              <button type="button" className="wb-button" onClick={() => void create()} disabled={busy === 'create'}>
                {busy === 'create' ? '创建中…' : '创建'}
              </button>
            </div>
          </div>
        ) : null}

        {schedules === null ? (
          <div className="wb-empty"><b>正在读取定时任务…</b></div>
        ) : schedules.length === 0 ? (
          <div className="wb-empty">
            <b>还没有定时任务</b>
            <p>创建一个每天/每周/按间隔运行的任务，让 Pi 自动产出日报、巡检或跟踪结果。</p>
          </div>
        ) : (
          <div className="wb-schedule-list">
            {schedules.map((schedule) => (
              <article key={schedule.id} className="wb-schedule">
                <div className="wb-schedule-head">
                  <b>{schedule.name}</b>
                  <span className={`wb-chip${schedule.lastStatus === 'failed' ? ' is-danger' : ''}`}>
                    {schedule.lastStatus === 'running' ? '运行中' : schedule.lastStatus === 'failed' ? '上次失败' : schedule.enabled ? '已启用' : '已停用'}
                  </span>
                  <button
                    type="button"
                    className={`wb-switch${schedule.enabled ? ' is-on' : ''}`}
                    onClick={() => void toggle(schedule)}
                    disabled={busy === schedule.id}
                    aria-pressed={schedule.enabled}
                    aria-label={schedule.enabled ? '停用' : '启用'}
                  >
                    <span />
                  </button>
                </div>
                <p className="wb-schedule-prompt">{schedule.prompt}</p>
                <div className="wb-schedule-meta">
                  {SCHEDULE_KIND_LABEL[schedule.kind] || schedule.kind}
                  {schedule.kind === 'interval' ? ` · 每 ${schedule.intervalMinutes} 分钟` : ''}
                  {schedule.kind === 'daily' || schedule.kind === 'weekly'
                    ? ` · ${String(schedule.hour ?? 0).padStart(2, '0')}:${String(schedule.minute ?? 0).padStart(2, '0')}`
                    : ''}
                  {' · '}
                  {workspaces.find((item) => item.id === schedule.workspace)?.name || schedule.workspace}
                  {' / '}
                  {apps.find((item) => item.id === schedule.appId)?.name || schedule.appId}
                  {' / '}
                  {apps
                    .find((item) => item.id === schedule.appId)
                    ?.modes.find((item) => item.id === schedule.modeId)?.name || schedule.modeId}
                  {' · 下次 '}{formatDateTime(schedule.nextRunAt)}
                </div>
                {schedule.lastError ? <div className="wb-alert">{schedule.lastError}</div> : null}
                <div className="wb-schedule-actions">
                  <button type="button" className="wb-button" onClick={() => void runNow(schedule)} disabled={busy === schedule.id}>
                    立即运行
                  </button>
                  <button type="button" className="wb-button" onClick={() => void history(schedule)}>
                    {runs[schedule.id] ? '收起历史' : '历史'}
                  </button>
                  <span className="wb-spacer" />
                  <button type="button" className="wb-link wb-link--danger" onClick={() => void remove(schedule)}>
                    删除
                  </button>
                </div>
                {runs[schedule.id] ? (
                  <div className="wb-schedule-runs">
                    {runs[schedule.id].length === 0 ? (
                      <p className="wb-muted">还没有运行记录。</p>
                    ) : (
                      runs[schedule.id].map((run) => (
                        <div key={run.id} className="wb-schedule-run">
                          <span className={`wb-run-dot is-${run.status}`} />
                          <span>{formatDateTime(run.startedAt)}</span>
                          <span>{run.status}</span>
                          {run.error ? <span className="wb-run-error">{run.error}</span> : null}
                        </div>
                      ))
                    )}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
