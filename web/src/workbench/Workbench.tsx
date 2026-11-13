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
import { PromptHost, askConfirm, askText } from '../components/Prompt'
import { Toasts } from '../components/Toasts'
import { ToolCard } from '../components/ToolCard'
import { api } from '../lib/api'
import { toolResultText } from '../lib/useAgent'
import { resolveApiBase } from '../lib/endpoints'
import { renderMarkdown } from '../lib/markdown'
import { asRecord, isImageBlock, isTextBlock, isThinkingBlock, isToolCallBlock } from '../lib/protocol'
import { MAX_ATTACHMENTS, MAX_FILES, MAX_UPLOAD_BYTES, prepareImageFile } from '../lib/images'
import type { DraftFile, DraftImage } from '../lib/images'
import type {
  AssistantMessage,
  ContentBlock,
  CustomMessage,
  ImageAttachment,
  ImageBlock,
  Message,
  ModelInfo,
  Usage,
} from '../lib/protocol'
import { navigate } from '../lib/router'
import { useAgent } from '../lib/useAgent'
import { useTheme } from '../lib/useTheme'
import type { AgentApi, ToolExecution, TranscriptEntry } from '../lib/useAgent'
import type { AuthApi } from '../lib/useAuth'
import './workbench.css'

type View =
  | 'home'
  | 'chat'
  | 'apps'
  | 'skills'
  | 'plugins'
  | 'scheduled'
  | 'files'
  | 'market'
  | 'help'
  | 'users'

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
  examples?: string[]
  placeholder?: string
  icon?: string
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
  changes?: string[]
}

interface SkillOption {
  name: string
  description: string
  enabled: boolean
}

const APP_ACTION_LABEL: Record<string, string> = {
  create: '创建',
  update: '修改',
  delete: '删除',
  restore: '恢复默认',
  duplicate: '复制',
  import: '导入',
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

/** 图标栏 = 产品的一级导航（对应原型左侧 60px 竖栏）。 */
const RAIL: { view: View; label: string; icon: IconName }[] = [
  { view: 'apps', label: '应用', icon: 'apps' },
  { view: 'skills', label: '技能', icon: 'spark' },
  { view: 'plugins', label: '插件', icon: 'plugin' },
  { view: 'scheduled', label: '定时', icon: 'clock' },
  { view: 'files', label: '资料库', icon: 'folder' },
  { view: 'market', label: '市场', icon: 'bag' },
]

/** 顶栏标题：一级页面固定，首页/会话按当前上下文拼。 */
const PAGE_META: Partial<Record<View, { title: string; sub?: string }>> = {
  apps: { title: '应用', sub: '应用与工作模式' },
  skills: { title: '技能', sub: 'SKILL.md 做事方法' },
  plugins: { title: '插件', sub: 'Python SDK 工具扩展' },
  scheduled: { title: '定时任务', sub: '按计划自动执行 prompt' },
  files: { title: '资料库', sub: '工作区文件' },
  market: { title: '市场', sub: '安装技能与插件' },
  help: { title: '帮助', sub: '使用说明与快捷键' },
  users: { title: '用户管理', sub: '团队成员与权限' },
}

/** Bump when `office-dist` is rebuilt: it forces the editor frame to fetch a fresh
 *  shell instead of reusing a cached one (the office app hides its AI dock in embed
 *  mode, and that CSS ships inside the shell's hashed bundle). */
const OFFICE_SHELL_REV = '3'

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

const APP_ICON_NAMES = [
  'spark',
  'book',
  'code',
  'palette',
  'search',
  'slides',
  'sheet',
  'file',
  'folder',
  'image',
  'clock',
  'apps',
  'plugin',
  'skill',
  'house',
] as const

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
  chart: (
    <>
      <path d="M4 4v16h16" />
      <path d="m7.5 15.5 3.5-4 2.5 2.5 4.5-6" />
    </>
  ),
  logout: <path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M16 8l4 4-4 4M20 12H9" />,
  bag: (
    <>
      <path d="M4 9h16l-1.2 9.2a1.8 1.8 0 0 1-1.8 1.6H7a1.8 1.8 0 0 1-1.8-1.6L4 9Z" />
      <path d="M8.5 9V6.8a3.5 3.5 0 0 1 7 0V9" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.2" />
      <path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .8-1 1.5v.4" />
      <circle cx="12" cy="16.6" r=".7" fill="currentColor" stroke="none" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="2.8" />
      <path d="M19.2 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1h-.2a1.9 1.9 0 1 1 0-3.8h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5v-.2a1.9 1.9 0 1 1 3.8 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.2a1.9 1.9 0 1 1 0 3.8h-.1a1.6 1.6 0 0 0-1.5 1Z" />
    </>
  ),
  attach: (
    <path d="M20.5 11.5 12 20a5.6 5.6 0 0 1-8-8l8.2-8.2a3.8 3.8 0 0 1 5.4 5.4l-8.2 8.2a1.9 1.9 0 0 1-2.7-2.7l7.6-7.6" />
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  list: <path d="M4 7h16M4 12h16M4 17h16" />,
  sliders: (
    <>
      <path d="M4 8h9M17 8h3M4 16h3M11 16h9" />
      <circle cx="15" cy="8" r="2" />
      <circle cx="9" cy="16" r="2" />
    </>
  ),
  folderPlus: (
    <>
      <path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2h7.3a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-10Z" />
      <path d="M12 11v6M9 14h6" />
    </>
  ),
  chevronRight: <path d="m9 6 6 6-6 6" />,
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
      <path d="M15.5 5.5h-7a3 3 0 0 0-3 3v7" />
    </>
  ),
  slash: <path d="M5 5h6M8 5v14M14 9h5M16.5 9v10" />,
  download: (
    <>
      <path d="M12 4v10" />
      <path d="m8 11 4 4 4-4" />
      <path d="M5 19h14" />
    </>
  ),
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
  if (section === 'market') return 'market'
  if (section === 'help') return 'help'
  if (section === 'users') return 'users'
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


function formatClock(value: number | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

type SidebarGroup = 'workspace' | 'flat'
type SidebarSort = 'recent' | 'name'

function sessionStamp(value: number | string | undefined): number {
  if (value === undefined || value === '') return 0
  const time = new Date(value).getTime()
  return Number.isNaN(time) ? 0 : time
}

/** "刚刚 / 9分钟 / 4小时 / 1天"，一周以上退回日期，和 DSH 的书架一致。 */
function timeLabel(value: number | string | undefined): string {
  const stamp = sessionStamp(value)
  if (!stamp) return ''
  const minute = 60_000
  const diff = Date.now() - stamp
  if (diff < minute) return '刚刚'
  if (diff < 60 * minute) return `${Math.floor(diff / minute)}分钟`
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))}小时`
  if (diff < 7 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))}天`
  const date = new Date(stamp)
  return `${date.getMonth() + 1}/${date.getDate()}`
}

/**
 * One folder per workspace (DSH's shelf) or a single flat list.
 * Sessions carry their workspace when the server answers `scope=all`; before that
 * (or on an older server) they all fall into the active workspace.
 */
function buildSessionGroups(
  sessions: WorkspaceSession[],
  groupBy: SidebarGroup,
  sortBy: SidebarSort,
  workspaces: WorkspaceRow[],
  activeWorkspace: string,
  activeWorkspaceName: string,
): { key: string; label: string; rows: WorkspaceSession[] }[] {
  const sorted = [...sessions].sort((left, right) =>
    sortBy === 'name'
      ? sessionTitle(left).localeCompare(sessionTitle(right), 'zh-Hans-CN')
      : sessionStamp(right.modified) - sessionStamp(left.modified),
  )
  if (groupBy === 'flat') return sorted.length > 0 ? [{ key: 'flat', label: '', rows: sorted }] : []

  const buckets = new Map<string, { key: string; label: string; rows: WorkspaceSession[] }>()
  const seed = (id: string, name: string) => {
    if (!buckets.has(id)) buckets.set(id, { key: id, label: name, rows: [] })
  }
  for (const workspace of workspaces) seed(workspace.id, workspace.name)
  seed(activeWorkspace, activeWorkspaceName)
  for (const session of sorted) {
    const id = session.workspace?.id ?? activeWorkspace
    seed(id, session.workspace?.name ?? activeWorkspaceName)
    buckets.get(id)?.rows.push(session)
  }
  return [...buckets.values()].filter((bucket) => bucket.rows.length > 0)
}

function userText(message: Message): string {
  const content = (message as { content?: string | ContentBlock[] }).content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter(isTextBlock).map((block) => block.text).join('\n')
}

function userImages(message: Message): ImageBlock[] {
  const content = (message as { content?: string | ContentBlock[] }).content
  if (!Array.isArray(content)) return []
  return content.filter(isImageBlock).filter((block) => Boolean(block.data))
}

function imageSource(block: ImageBlock): string {
  return `data:${block.mimeType || 'image/png'};base64,${block.data ?? ''}`
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
  app?: string | null
  mode?: string | null
  modified?: number | string
  /** Set by the server when the list spans workspaces (`scope=all`). */
  workspace?: { id: string; name: string } | null
}
interface ScheduleRunRow {
  id: string
  scheduleId: string
  scheduleName: string
  startedAt: string
  finishedAt: string | null
  status: string
  sessionFile: string | null
  error: string | null
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
  const [activeAppId, setActiveAppId] = useState(
    () => (typeof window === 'undefined' ? '' : window.localStorage.getItem('pi.activeApp')) || 'general',
  )
  const [activeModeId, setActiveModeId] = useState(
    () => (typeof window === 'undefined' ? '' : window.localStorage.getItem('pi.activeMode')) || 'office',
  )
  const theme = useTheme()
  const agent = useAgent({
    enabled: true,
    workspace: activeWorkspace,
    appId: activeAppId,
    modeId: activeModeId,
    onUnauthorized: auth.onUnauthorized,
  })
  const view = viewFromRoute(route)
  const [navOpen, setNavOpen] = useState(false)
  const [railMenuOpen, setRailMenuOpen] = useState(false)
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false)
  const [viewMenuOpen, setViewMenuOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [search, setSearch] = useState('')
  // Sidebar layout prefers the DSH shelf: one folder per workspace, newest first.
  const [groupBy, setGroupBy] = useState<SidebarGroup>(
    () => (typeof window === 'undefined' ? 'workspace' : (window.localStorage.getItem('pi.sidebar.group') as SidebarGroup) || 'workspace'),
  )
  const [sortBy, setSortBy] = useState<SidebarSort>(
    () => (typeof window === 'undefined' ? 'recent' : (window.localStorage.getItem('pi.sidebar.sort') as SidebarSort) || 'recent'),
  )
  // Group headers double as disclosure toggles; the collapsed set survives reloads.
  const [collapsedGroups, setCollapsedGroups] = useState<string[]>(() => {
    if (typeof window === 'undefined') return []
    try {
      const parsed: unknown = JSON.parse(window.localStorage.getItem('pi.sidebar.collapsed') || '[]')
      return Array.isArray(parsed) ? parsed.filter((key): key is string => typeof key === 'string') : []
    } catch {
      return []
    }
  })
  const [runsCollapsed, setRunsCollapsed] = useState(
    () => typeof window !== 'undefined' && window.localStorage.getItem('pi.sidebar.runsCollapsed') === '1',
  )
  // A conversation in another workspace can only be opened after the socket has been
  // re-bound to that workspace, so the click parks the path here and the effect below
  // opens it once the new worker is connected.
  const pendingSessionRef = useRef<string | null>(null)
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [artifactFocus, setArtifactFocus] = useState<string | null>(null)
  const [notifications, setNotifications] = useState<NotificationRow[]>([])
  const [unread, setUnread] = useState(0)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [scheduleRuns, setScheduleRuns] = useState<ScheduleRunRow[] | null>(null)
  const [draft, setDraft] = useState('')
  const [chatDraft, setChatDraft] = useState('')
  const [draftImages, setDraftImages] = useState<DraftImage[]>([])
  const [chatImages, setChatImages] = useState<DraftImage[]>([])
  const [draftFiles, setDraftFiles] = useState<DraftFile[]>([])
  const [chatFiles, setChatFiles] = useState<DraftFile[]>([])

  const connectionOpen = agent.connection === 'open'
  const workspaceName =
    workspaces.find((workspace) => workspace.id === activeWorkspace)?.name || '默认工作空间'
  const workspacePath =
    workspaces.find((workspace) => workspace.id === activeWorkspace)?.path || ''
  const availableApps = apps.length > 0 ? apps : [FALLBACK_APP]
  const activeApp = availableApps.find((app) => app.id === activeAppId) || availableApps[0] || FALLBACK_APP
  const activeMode =
    activeApp.modes.find((item) => item.id === activeModeId) || activeApp.modes[0] || FALLBACK_APP.modes[0]
  const userName = auth.user?.displayName || auth.user?.username || '本地用户'

  useEffect(() => {
    try {
      window.localStorage.setItem('pi.activeApp', activeAppId)
      window.localStorage.setItem('pi.activeMode', activeModeId)
    } catch {
      // Private mode / quota: remembering the app is a convenience, not a requirement.
    }
  }, [activeAppId, activeModeId])

  useEffect(() => {
    api
      .get<{ workspaces: WorkspaceRow[]; canCreate: boolean }>('/api/workspaces')
      .then((payload) => {
        // A server that omits the list (or an older one) must not take the shell
        // down with it: normalise to an array before it reaches render.
        const rows = Array.isArray(payload.workspaces) ? payload.workspaces : []
        setWorkspaces(rows)
        setCanCreateWorkspace(Boolean(payload.canCreate))
        setWorkspaceError(null)
        if (!rows.some((workspace) => workspace.id === activeWorkspace)) {
          setActiveWorkspace(rows[0]?.id || 'default')
        }
      })
      .catch((failure: unknown) => {
        setWorkspaceError(failure instanceof Error ? failure.message : '读取工作区失败')
      })
  }, [activeWorkspace])

  useEffect(() => {
    let alive = true
    const load = () => {
      api
        .get<{ runs: ScheduleRunRow[] }>('/api/schedules/runs?limit=100')
        .then((payload) => {
          if (alive) setScheduleRuns(payload.runs)
        })
        .catch(() => {
          if (alive) setScheduleRuns([])
        })
    }
    load()
    const timer = window.setInterval(load, 30_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const loadApps = () => {
    api
      .get<{ apps: AppRow[]; canManage?: boolean }>('/api/apps')
      .then((payload) => {
        const rows = Array.isArray(payload.apps) ? payload.apps : []
        setApps(rows)
        setCanManageApps(Boolean(payload.canManage))
        if (rows.length > 0) {
          const current = rows.find((app) => app.id === activeAppId)
          if (!current) {
            setActiveAppId(rows[0].id)
            setActiveModeId(rows[0].modes[0]?.id || 'office')
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
          setNotifications(Array.isArray(payload.notifications) ? payload.notifications : [])
          setUnread(Number(payload.unread) || 0)
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
    window.localStorage.setItem('pi.sidebar.group', groupBy)
  }, [groupBy])

  useEffect(() => {
    window.localStorage.setItem('pi.sidebar.sort', sortBy)
  }, [sortBy])

  useEffect(() => {
    window.localStorage.setItem('pi.sidebar.collapsed', JSON.stringify(collapsedGroups))
  }, [collapsedGroups])

  useEffect(() => {
    window.localStorage.setItem('pi.sidebar.runsCollapsed', runsCollapsed ? '1' : '0')
  }, [runsCollapsed])

  // Closing a menu on an outside click, same contract as the other popovers.
  useEffect(() => {
    if (!viewMenuOpen && !workspaceMenuOpen) return
    const close = () => {
      setViewMenuOpen(false)
      setWorkspaceMenuOpen(false)
    }
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [viewMenuOpen, workspaceMenuOpen])

  // Opening a parked cross-workspace session needs the *new* socket: the click still
  // sees the old one as open, so wait for a real down→up transition.
  const connectionRef = useRef(agent.connection)
  useEffect(() => {
    const previous = connectionRef.current
    connectionRef.current = agent.connection
    if (agent.connection !== 'open' || previous === 'open') return
    const path = pendingSessionRef.current
    if (!path) return
    pendingSessionRef.current = null
    agent.switchSession(path)
    go('/workbench/chat')
    // `agent` is rebuilt every render; the ref guard above keeps this a one-shot.
  }, [agent.connection])

  useEffect(() => {
    if (!railMenuOpen && !workspaceMenuOpen) return
    const close = () => {
      setRailMenuOpen(false)
      setWorkspaceMenuOpen(false)
    }
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [railMenuOpen, workspaceMenuOpen])

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
    setDraftImages([])
    setChatImages([])
    setDraftFiles([])
    setChatFiles([])
    setActiveWorkspace(workspace)
    setNavOpen(false)
    goView('home')
  }

  const createWorkspace = async () => {
    const name = (await askText({
      title: '新建工作区',
      label: '工作区名称',
      placeholder: '例如：研究空间',
      confirmText: '创建',
    }))?.trim()
    if (!name) return
    try {
      const created = await api.post<WorkspaceRow>('/api/workspaces', { name })
      setWorkspaces((current) => [...current, created])
      setWorkspaceError(null)
      switchWorkspace(created.id)
    } catch (failure) {
      setWorkspaceError(failure instanceof Error ? failure.message : '创建工作区失败')
    }
  }

  const openSession = (path: string) => {
    agent.switchSession(path)
    setNavOpen(false)
    go('/workbench/chat')
  }

  /** Opens a session even when it belongs to another workspace: switch, then open. */
  const openAnySession = (session: WorkspaceSession) => {
    const workspace = session.workspace?.id
    if (workspace && workspace !== activeWorkspace) {
      pendingSessionRef.current = session.path
      switchWorkspace(workspace)
      return
    }
    openSession(session.path)
  }

  // Images are downscaled in the browser and travel to the server as inline
  // base64 (the protocol cannot carry files).  A failure on one file must not
  // discard the rest of the selection, so each is prepared independently.
  const addImages = async (target: 'home' | 'chat', files: File[]) => {
    const prepared: DraftImage[] = []
    for (const file of files) {
      try {
        const image = await prepareImageFile(file)
        if (image) prepared.push(image)
      } catch {
        // Ignore an unreadable image; the others in the same selection still go.
      }
    }
    if (prepared.length === 0) return
    const append = (current: DraftImage[]) => [...current, ...prepared].slice(0, MAX_ATTACHMENTS)
    if (target === 'home') setDraftImages(append)
    else setChatImages(append)
  }

  const removeImage = (target: 'home' | 'chat', id: string) => {
    const drop = (current: DraftImage[]) => current.filter((image) => image.id !== id)
    if (target === 'home') setDraftImages(drop)
    else setChatImages(drop)
  }

  /**
   * Non-image files cannot travel through the chat protocol (it is text + inline
   * images).  Put them in the active workspace instead and hand the agent their
   * paths in the message; the workspace is the agent's cwd, so every tool and
   * skill can open them.
   */
  const addFiles = async (target: 'home' | 'chat', files: File[]) => {
    const workspace = activeWorkspace
    const prepared: DraftFile[] = []
    for (const file of files) {
      if (file.size > MAX_UPLOAD_BYTES) {
        window.alert(`${file.name} 超过 32 MB，无法上传。`)
        continue
      }
      const safeName =
        (file.name || 'file').replace(/[\\/:*?"<>|]+/g, '_').trim() || 'file'
      const targetPath = `/attachments/${Date.now()}-${Math.random()
        .toString(36)
        .slice(2, 8)}-${safeName}`
      try {
        await api.upload(
          `/api/workspace/file?path=${encodeURIComponent(targetPath)}&workspace=${encodeURIComponent(workspace)}`,
          await file.arrayBuffer(),
        )
        prepared.push({
          id: `file-${Date.now()}-${prepared.length}`,
          name: file.name || safeName,
          mimeType: file.type || 'application/octet-stream',
          bytes: file.size,
          path: targetPath.replace(/^\/+/, ''),
        })
      } catch (failure) {
        window.alert(
          `上传 ${file.name} 失败：${failure instanceof Error ? failure.message : '未知错误'}`,
        )
      }
    }
    if (prepared.length === 0) return
    const append = (current: DraftFile[]) => [...current, ...prepared].slice(0, MAX_FILES)
    if (target === 'home') setDraftFiles(append)
    else setChatFiles(append)
  }

  const removeFile = (target: 'home' | 'chat', id: string) => {
    const drop = (current: DraftFile[]) => current.filter((file) => file.id !== id)
    if (target === 'home') setDraftFiles(drop)
    else setChatFiles(drop)
  }

  const attachmentNote = (files: DraftFile[]) =>
    files.length
      ? `\n\n附件已上传到工作区，请直接使用这些路径：\n${files
          .map((file) => `- ${file.path}（${file.name}，${formatBytes(file.bytes)}）`)
          .join('\n')}`
      : ''

  const attachmentsOf = (images: DraftImage[]): ImageAttachment[] =>
    images.map((image) => ({ type: 'image', mimeType: image.mimeType, data: image.data }))

  const sendHome = () => {
    const text = draft.trim()
    if ((!text && draftImages.length === 0 && draftFiles.length === 0) || !connectionOpen) return
    const message = `${text}${attachmentNote(draftFiles)}`.trim()
    agent.sendPrompt(message, undefined, draftImages.length ? attachmentsOf(draftImages) : undefined)
    setDraft('')
    setDraftImages([])
    setDraftFiles([])
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
    if ((!text && chatImages.length === 0 && chatFiles.length === 0) || !connectionOpen) return
    const message = `${text}${attachmentNote(chatFiles)}`.trim()
    agent.sendPrompt(message, undefined, chatImages.length ? attachmentsOf(chatImages) : undefined)
    setChatDraft('')
    setChatImages([])
    setChatFiles([])
  }

  const scheduledSessionFiles = new Set(
    (scheduleRuns ?? []).map((run) => run.sessionFile).filter((path): path is string => Boolean(path)),
  )
  const visibleSessions = (rows: WorkspaceSession[] | undefined) =>
    rows?.filter((session) => !scheduledSessionFiles.has(session.path))

  const allSessions = visibleSessions(agent.sessions) ?? []
  const query = search.trim().toLowerCase()
  const listedSessions = query
    ? allSessions.filter((session) => sessionTitle(session).toLowerCase().includes(query))
    : allSessions
  const sessionGroups = buildSessionGroups(
    listedSessions,
    groupBy,
    sortBy,
    workspaces,
    activeWorkspace,
    workspaceName,
  )
  const runningSessions = (scheduleRuns ?? []).slice(0, 4)
  const collapsedKeys = new Set(collapsedGroups)
  const toggleGroup = (key: string) =>
    setCollapsedGroups((keys) => (keys.includes(key) ? keys.filter((item) => item !== key) : [...keys, key]))
  const activeSessionPath = agent.state?.sessionFile
  const chatTitle = agent.state?.sessionName?.trim() || '新任务'
  const pageMeta = PAGE_META[view]
  const topTitle =
    view === 'home' ? '首页' : view === 'chat' ? chatTitle : pageMeta?.title ?? 'Pi'
  const topSub =
    view === 'home'
      ? `${activeApp.name} · ${activeMode.name}`
      : view === 'chat'
        ? `${workspaceName} / ${activeApp.name} / ${activeMode.name}`
        : pageMeta?.sub ?? ''

  const accountMenu = (
    <>
      {auth.authMode === 'team' ? (
        <>
          {auth.user?.isAdmin ? (
            <button
              type="button"
              onClick={() => {
                setRailMenuOpen(false)
                go('/workbench/users')
              }}
            >
              用户管理
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => {
              setRailMenuOpen(false)
              go('/account')
            }}
          >
            修改密码
          </button>
          <div className="wb-more-line" />
          <button
            type="button"
            onClick={() => {
              setRailMenuOpen(false)
              void auth.logout()
            }}
          >
            退出登录
          </button>
          <div className="wb-more-line" />
        </>
      ) : null}
      <button
        type="button"
        onClick={() => {
          setRailMenuOpen(false)
          go('/workbench/help')
        }}
      >
        帮助
      </button>
      {auth.authMode !== 'team' ? <small>单机模式 · 无需登录</small> : null}
    </>
  )

  return (
    <>
      <div className={`wb${navOpen ? ' is-nav-open' : ''}`}>
        <aside className="wb-rail">
          <button type="button" className="rail-logo" onClick={() => goView('home')} title="Pi 工作台">
            π
          </button>
          <nav className="rail-nav" aria-label="功能">
            {RAIL.map((item) => {
              // 首页与会话都属于「应用」这一层，和视觉稿的选中态一致。
              const activeView = view === 'home' || view === 'chat' ? 'apps' : view
              return (
                <button
                  key={item.view}
                  type="button"
                  className={`rail-item${activeView === item.view ? ' is-active' : ''}`}
                  onClick={() => goView(item.view)}
                  title={item.label}
                  aria-current={activeView === item.view ? 'page' : undefined}
                >
                  <Icon name={item.icon} size={19} />
                  <span>{item.label}</span>
                </button>
              )
            })}
          </nav>
          <div className="rail-foot">
            <button
              type="button"
              className={`rail-item${view === 'help' ? ' is-active' : ''}`}
              onClick={() => goView('help')}
              title="帮助"
            >
              <Icon name="help" size={19} />
              <span>帮助</span>
            </button>
            <div className="wb-more-anchor">
              <button
                type="button"
                className={`rail-item${railMenuOpen || view === 'users' ? ' is-active' : ''}`}
                onClick={(event) => {
                  event.stopPropagation()
                  setRailMenuOpen((value) => !value)
                }}
                title="设置"
              >
                <Icon name="settings" size={19} />
                <span>设置</span>
              </button>
              {railMenuOpen ? (
                <div className="rail-menu" onClick={(event) => event.stopPropagation()}>
                  {accountMenu}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              className="rail-avatar"
              onClick={(event) => {
                event.stopPropagation()
                setRailMenuOpen((value) => !value)
              }}
              title={userName}
              aria-label={`账号：${userName}`}
            >
              {userName.slice(0, 1)}
            </button>
          </div>
        </aside>

        <aside className="wb-panel">
          <div className="panel-head">
            {searchOpen ? (
              <div className="panel-search is-inline">
                <Icon name="search" size={14} />
                <input
                  autoFocus
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="搜索会话名称"
                  aria-label="搜索会话"
                />
                <button
                  type="button"
                  className="search-clear"
                  aria-label="关闭搜索"
                  onClick={() => {
                    setSearchOpen(false)
                    setSearch('')
                  }}
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            ) : (
              <>
                <h2 className="panel-title">工作区</h2>
                <div className="panel-head-actions">
                  <button
                    type="button"
                    className="panel-icon-btn"
                    title="搜索会话"
                    aria-label="搜索会话"
                    onClick={() => setSearchOpen(true)}
                  >
                    <Icon name="search" size={16} />
                  </button>
                  <div className="panel-menu-anchor">
                    <button
                      type="button"
                      className={`panel-icon-btn${viewMenuOpen ? ' is-active' : ''}`}
                      title="显示选项"
                      aria-haspopup="menu"
                      aria-expanded={viewMenuOpen}
                      onClick={(event) => {
                        event.stopPropagation()
                        setViewMenuOpen((value) => !value)
                        setWorkspaceMenuOpen(false)
                      }}
                    >
                      <Icon name="sliders" size={16} />
                    </button>
                    {viewMenuOpen ? (
                      <div className="panel-menu" role="menu" onClick={(event) => event.stopPropagation()}>
                        <div className="menu-label">分组方式</div>
                        <button type="button" onClick={() => setGroupBy('workspace')}>
                          <Icon name="folder" size={14} />
                          <span>按工作区</span>
                          {groupBy === 'workspace' ? <Icon name="check" size={14} /> : null}
                        </button>
                        <button type="button" onClick={() => setGroupBy('flat')}>
                          <Icon name="list" size={14} />
                          <span>单列表</span>
                          {groupBy === 'flat' ? <Icon name="check" size={14} /> : null}
                        </button>
                        <div className="panel-menu-line" />
                        <div className="menu-label">排序方式</div>
                        <button type="button" onClick={() => setSortBy('recent')}>
                          <Icon name="clock" size={14} />
                          <span>最近更新</span>
                          {sortBy === 'recent' ? <Icon name="check" size={14} /> : null}
                        </button>
                        <button type="button" onClick={() => setSortBy('name')}>
                          <Icon name="file" size={14} />
                          <span>会话名称</span>
                          {sortBy === 'name' ? <Icon name="check" size={14} /> : null}
                        </button>
                      </div>
                    ) : null}
                  </div>
                  <div className="panel-menu-anchor">
                    <button
                      type="button"
                      className={`panel-icon-btn${workspaceMenuOpen ? ' is-active' : ''}`}
                      title="新建或切换工作区"
                      aria-haspopup="menu"
                      aria-expanded={workspaceMenuOpen}
                      onClick={(event) => {
                        event.stopPropagation()
                        setWorkspaceMenuOpen((value) => !value)
                        setViewMenuOpen(false)
                      }}
                    >
                      <Icon name="folderPlus" size={16} />
                    </button>
                    {workspaceMenuOpen ? (
                      <div className="panel-menu" role="menu" onClick={(event) => event.stopPropagation()}>
                        {canCreateWorkspace ? (
                          <>
                            <button
                              type="button"
                              onClick={() => {
                                setWorkspaceMenuOpen(false)
                                void createWorkspace()
                              }}
                            >
                              <Icon name="plus" size={14} />
                              <span>新建工作区</span>
                            </button>
                            <div className="panel-menu-line" />
                          </>
                        ) : null}
                        <div className="menu-label">切换工作区</div>
                        {workspaces.map((workspace) => (
                          <button
                            key={workspace.id}
                            type="button"
                            title={workspace.path || workspace.name}
                            onClick={() => {
                              setWorkspaceMenuOpen(false)
                              switchWorkspace(workspace.id)
                            }}
                          >
                            <Icon name="folder" size={14} />
                            <span>{workspace.name}</span>
                            {workspace.id === activeWorkspace ? <Icon name="check" size={14} /> : null}
                          </button>
                        ))}
                        {workspaceError ? <div className="panel-menu-error">{workspaceError}</div> : null}
                      </div>
                    ) : null}
                  </div>
                </div>
              </>
            )}
          </div>

          <button type="button" className="panel-new" onClick={newTask}>
            <Icon name="plus" size={14} />
            新建任务
          </button>

          <div className="panel-scroll">
            {runningSessions.length > 0 ? (
              <div className="ws-group">
                <button
                  type="button"
                  className="ws-group-head"
                  aria-expanded={!runsCollapsed}
                  title={runsCollapsed ? '展开定时运行' : '收起定时运行'}
                  onClick={() => setRunsCollapsed((value) => !value)}
                >
                  <Icon name={runsCollapsed ? 'chevronRight' : 'chevronDown'} size={13} />
                  <Icon name="clock" size={14} />
                  <span>定时运行</span>
                  <i className="ws-group-count">{runningSessions.length}</i>
                </button>
                {runsCollapsed
                  ? null
                  : runningSessions.map((run) => (
                      <button
                        key={run.id}
                        type="button"
                        className={`conv${run.sessionFile && activeSessionPath === run.sessionFile ? ' is-active' : ''}`}
                        title={`${run.scheduleName}${run.error ? ` · ${run.error}` : ''}`}
                        onClick={() => {
                          if (run.sessionFile) openSession(run.sessionFile)
                          else go('/workbench/scheduled')
                        }}
                      >
                        <span className="conv-glyph">
                          <Icon name="clock" size={13} />
                        </span>
                        <span className="conv-title">{run.scheduleName}</span>
                        <span
                          className={`wb-run-dot is-${
                            run.status === 'success' ? 'success' : run.status === 'failed' ? 'failed' : 'running'
                          }`}
                        />
                      </button>
                    ))}
              </div>
            ) : null}

            {sessionGroups.length === 0 ? (
              <p className="panel-empty">
                {query ? '没有匹配的会话。' : '还没有会话，点上方「新建任务」开始。'}
              </p>
            ) : (
              sessionGroups.map((group) => {
                // A search should never leave matches hidden inside a collapsed folder.
                const collapsed = Boolean(group.label) && !query && collapsedKeys.has(group.key)
                return (
                  <div className="ws-group" key={group.key}>
                    {group.label ? (
                      <button
                        type="button"
                        className="ws-group-head"
                        aria-expanded={!collapsed}
                        title={collapsed ? `展开 ${group.label}` : `收起 ${group.label}`}
                        onClick={() => toggleGroup(group.key)}
                      >
                        <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} size={13} />
                        <Icon name="folder" size={14} />
                        <span>{group.label}</span>
                        <i className="ws-group-count">{group.rows.length}</i>
                      </button>
                    ) : null}
                    {collapsed
                      ? null
                      : group.rows.map((session) => {
                          const active = Boolean(activeSessionPath && activeSessionPath === session.path)
                          return (
                            <button
                              key={session.path || session.id}
                              type="button"
                              className={`conv${active ? ' is-active' : ''}`}
                              title={sessionTitle(session)}
                              onClick={() => openAnySession(session)}
                            >
                              {active ? <span className="dot" /> : null}
                              <span className="conv-title">{sessionTitle(session)}</span>
                              <time>{timeLabel(session.modified)}</time>
                            </button>
                          )
                        })}
                  </div>
                )
              })
            )}
          </div>

        </aside>

        <div className="wb-scrim" onClick={() => setNavOpen(false)} />

        <main className="wb-main">
          <header className="wb-topbar">
            <button
              type="button"
              className="wb-mobile-menu"
              onClick={() => setNavOpen(true)}
              aria-label="打开导航"
            >
              <Icon name="panel" size={18} />
            </button>
            <div className="tb-title">
              <b>{topTitle}</b>
              {topSub ? <span className="sep">/</span> : null}
              {topSub ? <span>{topSub}</span> : null}
            </div>
            <div className="tb-actions">
              {view === 'chat' ? (
                <button
                  type="button"
                  className={`wb-artifact-toggle${artifactsOpen ? ' is-active' : ''}`}
                  onClick={() => setArtifactsOpen((value) => !value)}
                  title="产物"
                >
                  <Icon name="book" size={15} />
                  产物
                </button>
              ) : null}
              <span className="tb-divider" />
              <button
                type="button"
                className="wb-theme"
                onClick={theme.toggle}
                title={theme.isDark ? '切换到浅色' : '切换到深色'}
                aria-label={theme.isDark ? '切换到浅色' : '切换到深色'}
              >
                <span aria-hidden="true">{theme.isDark ? '☀' : '☾'}</span>
              </button>
              <button
                type="button"
                className="wb-bell"
                title={unread ? `${unread} 条未读通知` : '通知'}
                onClick={(event) => {
                  event.stopPropagation()
                  setNotificationsOpen((value) => !value)
                }}
              >
                <Icon name="bell" size={16} />
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
                        <span
                          className={`wb-run-dot is-${
                            notification.kind.endsWith('failed') ? 'failed' : 'success'
                          }`}
                        />
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
          </header>

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
              images={draftImages}
              onAddImages={(files) => void addImages('home', files)}
              onRemoveImage={(id) => removeImage('home', id)}
              files={draftFiles}
              onAddFiles={(files) => void addFiles('home', files)}
              onRemoveFile={(id) => removeFile('home', id)}
              models={agent.models}
              currentModel={agent.state?.model ?? null}
              onSelectModel={agent.setModel}
            />
          ) : null}

          {view === 'chat' ? (
            <ChatView
              agent={agent}
              draft={chatDraft}
              onDraft={setChatDraft}
              onSend={sendChat}
              disabled={!connectionOpen}
              workspaceId={activeWorkspace}
              workspacePath={workspacePath}
              workspaceName={workspaceName}
              modeName={activeMode.name}
              images={chatImages}
              onAddImages={(files) => void addImages('chat', files)}
              onRemoveImage={(id) => removeImage('chat', id)}
              files={chatFiles}
              onAddFiles={(files) => void addFiles('chat', files)}
              onRemoveFile={(id) => removeFile('chat', id)}
              models={agent.models}
              currentModel={agent.state?.model ?? null}
              onSelectModel={agent.setModel}
              onOpenArtifact={(path) => {
                setArtifactFocus(workspaceRelative(path, workspacePath))
                setArtifactsOpen(true)
              }}
              artifactsOpen={artifactsOpen}
              onCloseArtifacts={() => {
                setArtifactsOpen(false)
                setArtifactFocus(null)
              }}
              artifactFocus={artifactFocus}
              onArtifactFocusHandled={() => setArtifactFocus(null)}
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
          {view === 'market' ? <MarketView /> : null}
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
          {view === 'help' ? <HelpView authMode={auth.authMode} /> : null}
          {view === 'users' ? <UsersView isAdmin={Boolean(auth.user?.isAdmin)} /> : null}
        </main>
      </div>
      <DialogHost dialogs={agent.dialogs} onRespond={agent.respondDialog} />
      <PromptHost />
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
  images,
  onAddImages,
  onRemoveImage,
  files,
  onAddFiles,
  onRemoveFile,
  models,
  currentModel,
  onSelectModel,
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
  images: DraftImage[]
  onAddImages: (files: File[]) => void
  onRemoveImage: (id: string) => void
  files: DraftFile[]
  onAddFiles: (files: File[]) => void
  onRemoveFile: (id: string) => void
  models: ModelInfo[]
  currentModel: ModelInfo | null
  onSelectModel: (model: ModelInfo) => void
}) {
  const mode = app.modes.find((item) => item.id === modeId) || app.modes[0]
  const fallback = MODES[mode.id as Mode]
  const capsules = mode.capsules.length
    ? mode.capsules
    : (fallback?.quick ?? []).map((label) => ({ id: label, name: label, prompt: label }))
  const examples = mode.examples?.length ? mode.examples : fallback?.examples ?? []
  const placeholder = mode.placeholder || fallback?.placeholder || '输入消息，或 / 调用技能'
  const cards = examples.slice(0, 4)
  const tiles: IconName[] = ['file', 'slides', 'sheet', 'clock']
  const tones = ['tile--blue', 'tile--orange', 'tile--green', 'tile--purple']

  return (
    <div className="wb-home">
      <div className="wb-home-inner">
        <h1>有什么可以帮你？</h1>
        <div className="wb-app-line">
          <i />
          {app.name} · {mode.name}
        </div>

        <div className="home-controls">
          <div className="wb-modes" role="tablist" aria-label="工作模式">
            {app.modes.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={mode.id === item.id}
                className={`wb-mode${mode.id === item.id ? ' is-active' : ''}`}
                onClick={() => onChangeMode(item.id)}
                title={item.description || item.name}
              >
                <Icon
                  name={((item.icon || MODES[item.id as Mode]?.icon || 'spark') as IconName)}
                  size={13}
                />
                {item.name}
              </button>
            ))}
          </div>
          <div className="wb-quick">
            {capsules.slice(0, 3).map((capsule, index) => (
              <button
                key={capsule.id}
                type="button"
                className={index === 0 ? 'is-accent' : ''}
                onClick={() => onDraft(capsule.prompt)}
              >
                {index === 0 ? <Icon name="spark" size={12} /> : null}
                {capsule.name}
              </button>
            ))}
          </div>
        </div>

        <WbComposer
          value={draft}
          onChange={onDraft}
          onSend={onSend}
          onAbort={onAbort}
          streaming={streaming}
          disabled={disabled}
          placeholder={placeholder}
          workspaceName={workspaceName}
          images={images}
          onAddImages={onAddImages}
          onRemoveImage={onRemoveImage}
          files={files}
          onAddFiles={onAddFiles}
          onRemoveFile={onRemoveFile}
          models={models}
          currentModel={currentModel}
          onSelectModel={onSelectModel}
        />

        {cards.length > 0 ? (
          <div className="quick-grid">
            {cards.map((example, index) => (
              <button
                key={example}
                type="button"
                className="quick-card"
                onClick={() => onDraft(example)}
              >
                <span className={`tile ${tones[index % tones.length]}`}>
                  <Icon name={tiles[index % tiles.length]} size={16} />
                </span>
                <span className="txt">
                  <b>{example}</b>
                  <small>{mode.name} · 示例提示</small>
                </span>
                <span className="go">
                  <Icon name="chevronRight" size={14} />
                </span>
              </button>
            ))}
          </div>
        ) : null}

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
  workspaceId,
  workspacePath,
  workspaceName,
  modeName,
  images,
  onAddImages,
  onRemoveImage,
  files,
  onAddFiles,
  onRemoveFile,
  models,
  currentModel,
  onSelectModel,
  artifactsOpen,
  onCloseArtifacts,
  onOpenArtifact,
  artifactFocus,
  onArtifactFocusHandled,
}: {
  agent: AgentApi
  draft: string
  onDraft: (value: string) => void
  onSend: () => void
  disabled: boolean
  workspaceId: string
  workspacePath: string
  workspaceName: string
  modeName: string
  images: DraftImage[]
  onAddImages: (files: File[]) => void
  onRemoveImage: (id: string) => void
  files: DraftFile[]
  onAddFiles: (files: File[]) => void
  onRemoveFile: (id: string) => void
  models: ModelInfo[]
  currentModel: ModelInfo | null
  onSelectModel: (model: ModelInfo) => void
  artifactsOpen: boolean
  onCloseArtifacts: () => void
  onOpenArtifact: (path: string) => void
  artifactFocus: string | null
  onArtifactFocusHandled: () => void
}) {
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
      <div className="wb-chat-body">
        <div className="wb-chat-main">
          <WbTranscript
            agent={agent}
            workspaceId={workspaceId}
            workspacePath={workspacePath}
            sessionFile={sessionFile}
            modeName={modeName}
            feedback={feedback}
            onFeedback={(timestamp, rating) => void rate(timestamp, rating)}
            onOpenPresented={onOpenArtifact}
          />
          <div className="wb-chat-composer">
            <WbComposer
              value={draft}
              onChange={onDraft}
              onSend={onSend}
              onAbort={agent.abort}
              streaming={agent.isStreaming}
              disabled={disabled}
              placeholder="继续追问，或 / 调用技能"
              workspaceName={workspaceName}
              images={images}
              onAddImages={onAddImages}
              onRemoveImage={onRemoveImage}
              files={files}
              onAddFiles={onAddFiles}
              onRemoveFile={onRemoveFile}
              models={models}
              currentModel={currentModel}
              onSelectModel={onSelectModel}
              chat
            />
            <div className="composer-hint">
              <span>Enter 发送 · Shift + Enter 换行</span>
              {agent.isStreaming ? <span>正在生成，可随时中断</span> : null}
            </div>
          </div>
        </div>
        {artifactsOpen ? (
          <ArtifactPane
            workspaceId={workspaceId}
            focusPath={artifactFocus}
            onFocusHandled={onArtifactFocusHandled}
            onClose={onCloseArtifacts}
          />
        ) : null}
      </div>
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

function workspaceRelative(path: string, workspacePath: string): string {
  const root = workspacePath.replace(/\/+$/, '')
  if (root && (path === root || path.startsWith(`${root}/`))) {
    return path.slice(root.length).replace(/^\/+/, '')
  }
  return path.replace(/^\/+/, '')
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
  embedded = false,
}: {
  workspaceId: string
  path: string
  onClose: () => void
  onSaved: () => void
  embedded?: boolean
}) {
  const docId = officeDocId(workspaceId, path)
  const lower = path.toLowerCase()
  const kind = lower.endsWith('.pptx') ? 'slides' : lower.endsWith('.xlsx') ? 'sheets' : ''
  const editorUrl = kind
    ? `/office/${kind}/?embed=1&open=${encodeURIComponent(docId)}&v=${OFFICE_SHELL_REV}`
    : `/office/?embed=1&doc=${encodeURIComponent(docId)}&v=${OFFICE_SHELL_REV}`
  const [phase, setPhase] = useState<'locking' | 'ready' | 'locked' | 'error'>('locking')
  const [message, setMessage] = useState('')
  const [saved, setSaved] = useState(false)
  const [versions, setVersions] = useState<{ id: string; size: number; created: number }[] | null>(null)
  const [showVersions, setShowVersions] = useState(false)
  const [nonce, setNonce] = useState(0)
  const [fullscreen, setFullscreen] = useState(false)
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
    <div className={`wb-office-editor${embedded && !fullscreen ? ' is-pane' : ' is-full'}`}>
      <header>
        <b>在线编辑</b>
        {phase === 'ready' && saved ? <span>已保存</span> : null}
        {phase === 'ready' ? (
          <button type="button" className="wb-link" onClick={() => void loadVersions()}>
            版本
          </button>
        ) : null}
        {embedded ? (
          <button type="button" className="wb-link" onClick={() => setFullscreen((value) => !value)}>
            {fullscreen ? '退出全屏' : '全屏'}
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

function ArtifactPane({
  workspaceId,
  focusPath,
  onFocusHandled,
  onClose,
}: {
  workspaceId: string
  focusPath?: string | null
  onFocusHandled?: () => void
  onClose: () => void
}) {
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
  const [htmlPreview, setHtmlPreview] = useState(true)
  const [slides, setSlides] = useState<SlidePreviewRow[] | null>(null)
  const [sheets, setSheets] = useState<SheetPreviewRow[] | null>(null)
  const [officePath, setOfficePath] = useState<string | null>(null)
  const [viewerOpen, setViewerOpen] = useState(false)
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
    if (!focusPath || !items) return
    const target = focusPath.replace(/^\/+/, '')
    const item = items.find((entry) => entry.path.replace(/^\/+/, '') === target)
    if (!item) return
    setSelectedPath(item.path)
    setViewerOpen(true)
    if (['docx', 'xlsx', 'pptx'].includes(item.extension)) setOfficePath(item.path)
    onFocusHandled?.()
  }, [focusPath, items, onFocusHandled])

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
        setMarkdownMode(extension === 'md' || extension === 'mdx')
        setHtmlPreview(true)
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
    <aside className={`wb-artifacts${viewerOpen ? ' is-viewer' : ' is-list'}`}>
      <header className="wb-artifacts-head">
        {viewerOpen ? (
          <button
            type="button"
            className="wb-artifacts-back"
            onClick={() => setViewerOpen(false)}
            aria-label="返回产物列表"
          >
            <Icon name="chevronLeft" size={15} />
            产物
          </button>
        ) : (
          <b>产物</b>
        )}
        {!viewerOpen ? <span>{items?.length ?? 0}</span> : <b className="wb-artifacts-current">{selected?.name || ''}</b>}
        <button type="button" className="wb-icon-button" onClick={onClose} aria-label="关闭产物">
          ×
        </button>
      </header>

      {!viewerOpen ? <div className="wb-artifact-list">
        {items === null ? (
          <p className="wb-muted">正在读取…</p>
        ) : items.length === 0 ? (
          <p className="wb-muted">这个工作区还没有可预览的产物。生成文件后会自动出现在这里。</p>
        ) : (
          items.map((item) => (
            <button
              key={item.path}
              type="button"
              className={`wb-artifact-item${item.path === selectedPath ? ' is-active' : ''}`}
              onClick={() => {
                setSelectedPath(item.path)
                setViewerOpen(true)
              }}
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
      </div> : null}

      {viewerOpen && selected ? (
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
          ) : selected.extension === 'html' || selected.extension === 'htm' ? (
            preview?.truncated ? (
              <p className="wb-muted">
                文件超过 512KB，只截断了预览，脚本不会运行。下载后在浏览器里打开，或让 agent 缩小内联数据。
              </p>
            ) : htmlPreview ? (
              <iframe
                className="wb-html-preview"
                title={selected.name}
                /* 脚本必须能跑，图才画得出来；但不能给 allow-same-origin：srcdoc 继承父页面
                   的源，两者同时给等于把工作台交给任意 HTML。只给 scripts 时文档落在不透明
                   源上，拿不到父页面 DOM、cookie、localStorage，也读不到同源接口。 */
                sandbox="allow-scripts allow-downloads"
                referrerPolicy="no-referrer"
                srcDoc={text}
              />
            ) : (
              <textarea
                className="wb-artifact-text"
                value={text}
                onChange={(event) => setText(event.target.value)}
                spellCheck={false}
                aria-label={`编辑 ${selected.name}`}
              />
            )
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
                {selected.extension === 'html' || selected.extension === 'htm' ? (
                  <button
                    type="button"
                    className={htmlPreview ? 'is-active' : ''}
                    onClick={() => setHtmlPreview((value) => !value)}
                  >
                    {htmlPreview ? '编辑' : '预览'}
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
          ) : selected.extension === 'pdf' ? (
            <iframe
              className="wb-pdf-preview"
              title={selected.name}
              src={`/api/workspace/download?workspace=${encodeURIComponent(workspaceId)}&path=${encodeURIComponent(selected.path)}&inline=1`}
            />
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
          embedded
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
  workspacePath,
  onOpen,
}: {
  files: PresentedFile[]
  workspaceId: string
  workspacePath: string
  onOpen: (path: string) => void
}) {
  return (
    <div className="wb-presented">
      <div className="wb-presented-title">
        <Icon name="book" size={13} />
        <span>交付物 · {files.length} 个文件</span>
      </div>
      <div className="wb-presented-list">
      {files.map((file) => {
        const name = file.path.split('/').filter(Boolean).pop() || file.path
        const extension = name.includes('.') ? name.split('.').pop()!.toUpperCase() : 'FILE'
        const office = /\.(docx|xlsx|pptx)$/i.test(name)
        const relative = workspaceRelative(file.path, workspacePath)
        const href = `/api/workspace/download?workspace=${encodeURIComponent(workspaceId)}&path=${encodeURIComponent(relative)}`
        return (
          <div className="wb-presented-card" key={file.path}>
            <span className="wb-presented-icon"><Icon name={extension === 'PPTX' ? 'slides' : extension === 'XLSX' ? 'sheet' : 'file'} size={20} /></span>
            <span className="wb-presented-body">
              <b title={name}>{name}</b>
              <small title={file.description || extension}>{file.description || extension}</small>
            </span>
            <button type="button" className="wb-button" onClick={() => onOpen(relative)}>
              {office ? '在线编辑' : '查看'}
            </button>
            <a className="wb-link" href={href} download>下载</a>
          </div>
        )
      })}
      </div>
    </div>
  )
}

function WbThinking({ text, running }: { text: string; running: boolean }) {
  // 默认收起，和 DSH 一致：一行「思考 · 首句」，点开才看到过程。
  const [open, setOpen] = useState(false)
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
        {running ? <em>思考中…</em> : null}
        {!open ? (
          <small>
            <i aria-hidden="true" />
            <span>{summary}</span>
          </small>
        ) : null}
        <Icon name="chevronDown" size={13} />
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
  answers: TranscriptEntry[]
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
        answers: [],
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
        answers: [],
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
    turn.answers = turn.entries.filter((entry) =>
      entry.message.role === 'assistant'
      && entry.message.content.some((block) => isTextBlock(block) && block.text.trim().length > 0),
    )
    turn.running = streaming && index === turns.length - 1
    if (turn.end < turn.start) turn.end = turn.start
  }
  return turns
}

/** 这一轮有没有「过程」可看（思考 / 工具 / 自定义消息）；只有正文就不必显示折叠行。 */
function hasProcessSteps(turn: ChatTurn): boolean {
  return turn.entries.some((entry) => {
    const message = entry.message
    if (message.role === 'toolResult' || message.role === 'custom') return true
    if (message.role !== 'assistant') return false
    return message.content.some(
      (block) => isThinkingBlock(block) || (isToolCallBlock(block) && block.name !== 'present'),
    )
  })
}

function TurnProcess({
  turn,
  children,
  now,
  lastActivityAt,
}: {
  turn: ChatTurn
  children: ReactNode
  now: number
  lastActivityAt: number
}) {
  // 一轮结束默认收起：只留一行「已完成，用时 x」，点开才看每一步的思考和工具调用。
  const [open, setOpen] = useState(false)
  const elapsed = formatElapsed((turn.running ? now : turn.end) - turn.start)
  const quiet = turn.running && now - lastActivityAt > 20_000
  const stuck = turn.running && now - lastActivityAt > 45_000
  const liveLabel = stuck
    ? `可能卡住，用时 ${elapsed}`
    : quiet
      ? `等待输出，用时 ${elapsed}`
      : `深度求索中，用时 ${elapsed}`
  if (turn.running) {
    return (
      <div className={`wb-turn-process is-running${quiet ? ' is-quiet' : ''}${stuck ? ' is-stuck' : ''}`}>
        <div className="wb-turn-process-body">{children}</div>
        <div className="wb-turn-live" role="status" aria-live="polite">
          <Icon name="spark" size={14} />
          <span>{liveLabel}</span>
          {!quiet ? <i aria-hidden="true">···</i> : null}
        </div>
      </div>
    )
  }
  return (
    <div className={`wb-turn-process${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="wb-turn-process-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>已完成，用时 {elapsed}</span>
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
  const resultContent = execution?.result?.content ?? execution?.partialResult?.content
  const resultImages = Array.isArray(resultContent)
    ? resultContent.filter(isImageBlock).filter((item) => Boolean(item.data))
    : []
  const [now, setNow] = useState(Date.now())
  const running = execution?.status === 'running'
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  const idleMs = now - (execution?.lastActivityAt ?? execution?.startedAt ?? now)
  const stalled = running && idleMs > 15_000
  const stuck = running && idleMs > 45_000
  const label = stuck ? '可能卡住' : stalled ? '等待输出' : toolStatus(execution)
  // 默认收起，只留一行（状态在右侧），点开才看参数和输出。
  const [expanded, setExpanded] = useState(false)
  return (
    <details
      open={expanded}
      onToggle={(event) => {
        const next = event.currentTarget.open
        if (next !== expanded) setExpanded(next)
      }}
      className={`wb-process-row is-${toolStatus(execution) === '失败' ? 'error' : execution?.status || 'done'}${stalled ? ' is-stalled' : ''}${stuck ? ' is-stuck' : ''}`}
    >
      <summary>
        <span className={`wb-process-glyph${running && !stalled ? ' is-running' : ''}`}><Icon name={(block as { name?: string }).name === 'bash' ? 'code' : (block as { name?: string }).name === 'present' ? 'book' : 'file'} size={14} /></span>
        <b>{title}</b>
        <span className="wb-process-summary">{summary}</span>
        <em>{label}</em>
      </summary>
      {argsText ? <pre className="wb-process-pre">{argsText}</pre> : null}
      {resultText ? <pre className="wb-process-pre">{resultText}</pre> : null}
      {resultImages.length > 0 ? (
        <div className="wb-process-images">
          {resultImages.map((image, index) => (
            <img key={index} src={imageSource(image)} alt={`工具输出图片 ${index + 1}`} />
          ))}
        </div>
      ) : null}
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
  const images = message.role === 'toolResult'
    ? message.content.filter(isImageBlock).filter((block) => Boolean(block.data))
    : []
  const name = message.role === 'toolResult' ? message.toolName || '工具' : '工具'
  const [expanded, setExpanded] = useState(false)
  if (!text.trim()) return null
  return (
    <details
      open={expanded}
      onToggle={(event) => {
        const next = event.currentTarget.open
        if (next !== expanded) setExpanded(next)
      }}
      className={`wb-process-row${message.role === 'toolResult' && message.isError ? ' is-error' : ''}`}
    >
      <summary>
        <span className="wb-process-glyph"><Icon name="code" size={14} /></span>
        <b>{name}</b>
        <span className="wb-process-summary">结果</span>
        <em>{message.role === 'toolResult' && message.isError ? '失败' : '完成'}</em>
      </summary>
      <pre className="wb-process-pre">{text}</pre>
      {images.length > 0 ? (
        <div className="wb-process-images">
          {images.map((image, index) => (
            <img key={index} src={imageSource(image)} alt={`工具输出图片 ${index + 1}`} />
          ))}
        </div>
      ) : null}
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
          return (
            <div className="wb-process-assistant" key={entry.key}>
              {message.content.map((block, index) => {
                if (isThinkingBlock(block)) {
                  return <WbThinking key={`${entry.key}-think-${index}`} text={block.thinking} running={Boolean(entry.streaming)} />
                }
                if (isToolCallBlock(block)) {
                  if (block.name === 'present') return null
                  return <ProcessToolRow key={`${entry.key}-tool-${index}`} block={block} execution={toolExecutions[block.id]} />
                }
                if (isTextBlock(block)) {
                  return null
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

function TurnAnswer({
  turn,
  sessionFile,
  feedback,
  onFeedback,
}: {
  turn: ChatTurn
  sessionFile?: string
  feedback: Record<number, number>
  onFeedback: (timestamp: number, rating: number) => void
}) {
  // Hooks run before any early return so a turn that gains text mid-stream keeps
  // the same hook order as one that already has it.
  const [copied, setCopied] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const blocks = turn.answers.flatMap((entry) =>
    entry.message.role === 'assistant' ? entry.message.content.filter(isTextBlock) : [],
  )
  const final = turn.final?.message.role === 'assistant' ? (turn.final.message as AssistantMessage) : null
  const usage = usageLine(final?.usage)
  const copyAnswer = async () => {
    try {
      await navigator.clipboard.writeText(blocks.map((block) => block.text).join('\n\n'))
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard permission is optional; leave the button silent on refusal.
    }
  }
  const downloadDocx = async () => {
    setDownloading(true)
    try {
      const response = await fetch('/api/export/docx', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          markdown: blocks.map((block) => block.text).join('\n\n'),
          title: '回答',
          subtitle: new Date().toLocaleDateString('zh-CN'),
        }),
      })
      if (!response.ok) throw new Error(await response.text())
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `回答-${new Date().toISOString().slice(0, 10)}.docx`
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      // 导出失败不打断阅读：用户还在看答案，给个 title 提示即可。
    } finally {
      setDownloading(false)
    }
  }
  if (blocks.length === 0) return null
  return (
    <>
      <div className="wb-msg-content">
        {blocks.map((block, index) => (
          <div
            className="wb-md"
            key={index}
            dangerouslySetInnerHTML={{ __html: renderMarkdown(block.text) }}
          />
        ))}
      </div>
      <div className="wb-msg-foot">
          {usage ? <span className="num">{usage}</span> : null}
          <div className="wb-msg-actions">
            <button
              type="button"
              className="wb-copy"
              onClick={() => void copyAnswer()}
              title={copied ? '已复制' : '复制回答'}
              aria-label={copied ? '已复制' : '复制回答'}
            >
              {copied ? '已复制' : <Icon name="copy" size={13} />}
            </button>
            <button
              type="button"
              className="wb-copy"
              onClick={() => void downloadDocx()}
              title={downloading ? '正在生成…' : '下载 Word'}
              aria-label={downloading ? '正在生成' : '下载 Word'}
              disabled={downloading}
            >
              <Icon name="download" size={13} />
            </button>
          {final && sessionFile ? (
            <div className="wb-feedback">
              {[1, -1].map((rating) => (
                <button
                  key={rating}
                  type="button"
                  className={feedback[final.timestamp ?? 0] === rating ? 'is-active' : ''}
                  onClick={() => onFeedback(final.timestamp ?? 0, rating)}
                  title={rating === 1 ? '有帮助' : '没帮助'}
                  aria-label={rating === 1 ? '有帮助' : '没帮助'}
                >
                  {rating === 1 ? '👍' : '👎'}
                </button>
              ))}
              {feedback[final.timestamp ?? 0] ? <span>已反馈</span> : null}
            </div>
          ) : null}
          </div>
      </div>
    </>
  )
}

function WbTranscript({
  agent,
  workspaceId,
  workspacePath,
  sessionFile,
  modeName,
  feedback,
  onFeedback,
  onOpenPresented,
}: {
  agent: AgentApi
  workspaceId: string
  workspacePath: string
  sessionFile?: string
  modeName: string
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
  const lastActivity = useRef(Date.now())
  useEffect(() => {
    if (agent.isStreaming) lastActivity.current = Date.now()
  }, [agent.transcript, agent.toolExecutions, agent.isStreaming])
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
              <div className="wb-msg-ai">
                <span className="wb-msg-avatar" aria-hidden="true">
                  π
                </span>
                <div className="wb-msg-body">
                  <div className="wb-msg-name">
                    <b>Pi</b>
                    <span>AI 助手</span>
                    <span className="wb-msg-mode">· {modeName}</span>
                    <time className="num">{formatClock(turn.start)}</time>
                  </div>
                  {turn.running || hasProcessSteps(turn) ? (
                    <TurnProcess turn={turn} now={now} lastActivityAt={lastActivity.current}>
                      <ProcessEntries turn={turn} toolExecutions={agent.toolExecutions} />
                    </TurnProcess>
                  ) : null}
                  <TurnAnswer
                    turn={turn}
                    sessionFile={sessionFile}
                    feedback={feedback}
                    onFeedback={onFeedback}
                  />
                </div>
              </div>
            </section>
          ))}
          {presented.length > 0 ? (
            <PresentedFiles
              files={presented}
              workspaceId={workspaceId}
              workspacePath={workspacePath}
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
    const images = userImages(message)
    const text = userText(message)
    return (
      <div className="wb-msg-user">
        {images.length > 0 ? (
          <div className="wb-msg-user-images">
            {images.map((image, index) => (
              <img key={index} src={imageSource(image)} alt={`图片 ${index + 1}`} />
            ))}
          </div>
        ) : null}
        {text ? <div className="wb-msg-user-text">{text}</div> : null}
        {formatClock(messageTime(entry)) ? (
          <time className="num">{formatClock(messageTime(entry))}</time>
        ) : null}
      </div>
    )
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
        <span className="wb-msg-avatar" aria-hidden="true">
          π
        </span>
        <div className="wb-msg-body">
          <div className="wb-msg-name">
            <b>Pi</b>
            <span>AI 助手</span>
            {formatClock(assistant.timestamp) ? (
              <time className="num">{formatClock(assistant.timestamp)}</time>
            ) : null}
          </div>
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
    const images = message.content.filter(isImageBlock).filter((block) => Boolean(block.data))
    return (
      <div className="wb-msg-tool">
        <ToolCard
          toolCallId={message.toolCallId}
          toolName={message.toolName ?? '工具'}
          execution={toolExecutions[message.toolCallId]}
          fallbackText={message.content.filter(isTextBlock).map((part) => part.text).join('\n')}
          fallbackIsError={message.isError}
        />
        {images.length > 0 ? (
          <div className="wb-msg-tool-images">
            {images.map((image, index) => (
              <img key={index} src={imageSource(image)} alt={`工具输出图片 ${index + 1}`} />
            ))}
          </div>
        ) : null}
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
  if (isImageBlock(block)) {
    if (!block.data) return null
    return <img className="wb-msg-inline-image" src={imageSource(block)} alt="图片" />
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
  images,
  onAddImages,
  onRemoveImage,
  files,
  onAddFiles,
  onRemoveFile,
  models,
  currentModel,
  onSelectModel,
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
  images: DraftImage[]
  onAddImages: (files: File[]) => void
  onRemoveImage: (id: string) => void
  files: DraftFile[]
  onAddFiles: (files: File[]) => void
  onRemoveFile: (id: string) => void
  models: ModelInfo[]
  currentModel: ModelInfo | null
  onSelectModel: (model: ModelInfo) => void
  chat?: boolean
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [modelOpen, setModelOpen] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, 240)}px`
  }, [value])

  useEffect(() => {
    if (!modelOpen) return
    const close = () => setModelOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [modelOpen])

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (streaming) return
      onSend()
    }
  }

  const acceptFiles = (files: FileList | File[] | null | undefined) => {
    if (!files) return
    const picked = Array.from(files)
    const images = picked.filter((file) => file.type.startsWith('image/'))
    const others = picked.filter((file) => !file.type.startsWith('image/'))
    if (images.length > 0) onAddImages(images.slice(0, MAX_ATTACHMENTS))
    if (others.length > 0) onAddFiles(others.slice(0, MAX_FILES))
  }

  const ready = value.trim().length > 0 || images.length > 0 || files.length > 0
  const current = currentModel || models[0] || null
  const modelLabel = current?.name || current?.id || '模型'

  const insertSkillSlash = () => {
    const next = value.trimEnd() ? `${value.trimEnd()} /` : '/'
    onChange(next)
    window.setTimeout(() => ref.current?.focus(), 0)
  }

  return (
    <div
      className={`wb-composer${chat ? ' is-chat' : ''}`}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes('Files')) event.preventDefault()
      }}
      onDrop={(event) => {
        const dropped = Array.from(event.dataTransfer.files)
        if (dropped.length > 0) {
          event.preventDefault()
          acceptFiles(dropped)
        }
      }}
    >
      <input
        ref={fileRef}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          acceptFiles(event.target.files)
          event.target.value = ''
        }}
      />
      {images.length > 0 || files.length > 0 ? (
        <div className="wb-attachments">
          {images.map((image) => (
            <div className="wb-attachment" key={image.id}>
              <img src={image.dataUrl} alt={image.name} />
              <button
                type="button"
                className="wb-attachment-remove"
                onClick={() => onRemoveImage(image.id)}
                aria-label={`移除 ${image.name}`}
                title="移除图片"
              >
                ×
              </button>
            </div>
          ))}
          {files.map((file) => (
            <div className="wb-attachment is-file" key={file.id} title={file.path}>
              <Icon name="file" size={16} />
              <span className="wb-attachment-name">
                <b>{file.name}</b>
                <i>{formatBytes(file.bytes)}</i>
              </span>
              <button
                type="button"
                className="wb-attachment-remove"
                onClick={() => onRemoveFile(file.id)}
                aria-label={`移除 ${file.name}`}
                title="移除附件"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <textarea
        ref={ref}
        value={value}
        rows={1}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={(event) => {
          const pasted = Array.from(event.clipboardData.files)
          if (pasted.length > 0) {
            event.preventDefault()
            acceptFiles(pasted)
          }
        }}
        disabled={disabled && !streaming}
      />
      <div className="wb-composer-bar">
        <div className="model-anchor" onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            className="model-chip"
            title="选择模型"
            aria-haspopup="menu"
            aria-expanded={modelOpen}
            onClick={() => setModelOpen((open) => !open)}
          >
            <Icon name="spark" size={12} />
            <span>{modelLabel}</span>
            {models.length > 0 ? <Icon name="chevronDown" size={11} /> : null}
          </button>
          {modelOpen ? (
            <div className="model-menu" role="menu">
              {models.length === 0 ? (
                <p className="model-empty">当前部署没有上报可用模型。</p>
              ) : (
                models.map((model) => {
                  const activeModel = current?.id === model.id
                  return (
                    <button
                      key={`${model.provider}/${model.id}`}
                      type="button"
                      role="menuitemradio"
                      aria-checked={activeModel}
                      className={activeModel ? 'is-active' : ''}
                      onClick={() => {
                        onSelectModel(model)
                        setModelOpen(false)
                      }}
                    >
                      <span>
                        <b>{model.name || model.id}</b>
                        <small>
                          {model.provider} · {model.id}
                        </small>
                      </span>
                      {activeModel ? (
                        <span className="model-check">
                          <Icon name="check" size={13} />
                        </span>
                      ) : null}
                    </button>
                  )
                })
              )}
            </div>
          ) : null}
        </div>
        <button
          type="button"
          className="wb-attach"
          title="添加附件（图片、PDF、Word 等任意文件）"
          onClick={() => fileRef.current?.click()}
          disabled={disabled}
        >
          <Icon name="attach" size={15} />
        </button>
        <button
          type="button"
          className="wb-skill-btn"
          title="调用技能（/）"
          onClick={insertSkillSlash}
        >
          <Icon name="slash" size={15} />
        </button>
        <span className="wb-spacer" />
        {chat ? (
          <span className="wb-ws-hint" title={workspaceName}>
            <Icon name="house" size={12} />
            {workspaceName}
          </span>
        ) : null}
        {streaming ? (
          <button type="button" className="wb-abort" onClick={onAbort}>
            中断
          </button>
        ) : null}
        <button
          type="button"
          className={`wb-send${ready ? ' is-ready' : ''}${streaming ? ' is-stop' : ''}`}
          onClick={streaming ? onAbort : onSend}
          disabled={!streaming && (!ready || disabled)}
          aria-label={streaming ? '停止生成' : '发送'}
          title={streaming ? '停止生成' : '发送'}
        >
          <Icon name={streaming ? 'stop' : 'arrowUp'} size={streaming ? 14 : 17} />
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

function MarketView() {
  const [kind, setKind] = useState<'skill' | 'plugin'>('skill')
  return (
    <div className="wb-page">
      <div className="wb-page-inner">
        <header className="wb-page-head">
          <div>
            <h2>市场</h2>
            <p>技能与插件都从这里安装；装好后在「技能」「插件」页启停。只安装可信来源。</p>
          </div>
          <div className="wb-tabs" role="tablist" aria-label="市场类型">
            <button
              type="button"
              role="tab"
              aria-selected={kind === 'skill'}
              className={kind === 'skill' ? 'is-active' : ''}
              onClick={() => setKind('skill')}
            >
              技能
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={kind === 'plugin'}
              className={kind === 'plugin' ? 'is-active' : ''}
              onClick={() => setKind('plugin')}
            >
              插件
            </button>
          </div>
        </header>
        <MarketPanel kind={kind} onInstalled={() => undefined} />
      </div>
    </div>
  )
}

function SkillsView({ workspace }: { workspace: string }) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null)
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
          {skills ? <span className="wb-chip">{enabled} / {skills.length} 启用</span> : null}
          <button type="button" className="wb-button" onClick={() => go('/workbench/market')}>
            去市场
          </button>
        </header>

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
    const name = (await askText({ title: '新建目录', label: '目录名称', placeholder: '例如：资料' }))?.trim()
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
            <p>当前工作区的文件与产物；上传、预览和目录都在这里。</p>
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
  const [skills, setSkills] = useState<SkillOption[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const exportRef = useRef<HTMLAnchorElement>(null)
  const importRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    api
      .get<{ skills: SkillOption[] }>('/api/skills')
      .then((payload) => setSkills(payload.skills))
      .catch(() => setSkills([]))
  }, [])

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
    const restoreConfirmed = await askConfirm({
      title: '恢复默认配置',
      message: `恢复「${app.name}」的内置默认配置？当前修改会被覆盖。`,
      confirmText: '恢复',
      danger: true,
    })
    if (!restoreConfirmed) return
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

  const duplicate = async (app: AppRow) => {
    setBusy(true)
    setError(null)
    try {
      await api.post(`/api/apps/${encodeURIComponent(app.id)}/duplicate`, {})
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '复制应用失败')
    } finally {
      setBusy(false)
    }
  }

  const move = async (index: number, delta: number) => {
    const target = index + delta
    if (target < 0 || target >= apps.length) return
    const ordered = [...apps]
    const [moved] = ordered.splice(index, 1)
    ordered.splice(target, 0, moved)
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/apps/order', { ids: ordered.map((item) => item.id) })
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '调整顺序失败')
    } finally {
      setBusy(false)
    }
  }

  const exportApps = async () => {
    setError(null)
    try {
      const payload = await api.get<{ apps: AppRow[]; exportedAt: string }>('/api/apps/export')
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = exportRef.current
      if (anchor) {
        anchor.href = url
        anchor.download = `pi-apps-${payload.exportedAt.slice(0, 10)}.json`
        anchor.click()
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '导出失败')
    }
  }

  const importApps = async (file: File) => {
    setBusy(true)
    setError(null)
    try {
      const text = await file.text()
      const payload = JSON.parse(text) as { apps?: AppRow[] }
      if (!Array.isArray(payload.apps)) throw new Error('文件里没有 apps 数组')
      await api.post('/api/apps/import', { apps: payload.apps })
      onSaved()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '导入失败')
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
          examples: [],
          placeholder: '',
          icon: 'spark',
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
    const removeConfirmed = await askConfirm({
      title: '删除应用',
      message: `删除应用「${draft.name}」？此操作不可撤销。`,
      confirmText: '删除',
      danger: true,
    })
    if (!removeConfirmed) return
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
            <p>
              应用把工作模式和场景胶囊组合起来；全团队共用同一套配置，只有管理员可以修改。
              选中后，首页与下一条消息都会使用它的提示词与技能绑定。
            </p>
          </div>
          {canManage ? (
            <>
              <button type="button" className="wb-button" onClick={openNew}>新建应用</button>
              <button type="button" className="wb-button" onClick={() => void exportApps()}>导出</button>
              <button type="button" className="wb-button" onClick={() => importRef.current?.click()} disabled={busy}>
                导入
              </button>
              <input
                ref={importRef}
                type="file"
                accept="application/json,.json"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) void importApps(file)
                }}
              />
              <a ref={exportRef} hidden aria-hidden="true" />
            </>
          ) : (
            <span className="wb-chip">{apps.length} 个应用</span>
          )}
        </header>

        {error ? <div className="wb-alert">{error}</div> : null}

        <div className="wb-apps-grid">
          {apps.map((app, index) => {
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
                    <>
                      <button type="button" className="wb-button" onClick={() => void move(index, -1)} disabled={busy || index === 0} aria-label="上移">
                        ↑
                      </button>
                      <button type="button" className="wb-button" onClick={() => void move(index, 1)} disabled={busy || index === apps.length - 1} aria-label="下移">
                        ↓
                      </button>
                      <button type="button" className="wb-button" onClick={() => openEdit(app)}>编辑</button>
                      <button type="button" className="wb-button" onClick={() => void duplicate(app)} disabled={busy}>复制</button>
                    </>
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
          应用配置保存在部署级，所有成员看到的是同一套；管理员修改后会重启相关 worker，
          下一条消息使用新提示词。
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
                        {item.changes?.length ? (
                          <ul className="wb-audit-changes">
                            {item.changes.map((change) => (
                              <li key={change}>{change}</li>
                            ))}
                          </ul>
                        ) : null}
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
            skills={skills}
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
  skills,
  error,
  busy,
  onChange,
  onCancel,
  onSave,
  onDelete,
}: {
  draft: AppRow
  skills: SkillOption[]
  error: string | null
  busy: boolean
  onChange: (app: AppRow) => void
  onCancel: () => void
  onSave: () => void
  onDelete?: () => void
}) {
  const [preview, setPreview] = useState<Array<{ id: string; name: string; prompt: string; chars: number }> | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

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

  const toggleSkill = (modeIndex: number, name: string, checked: boolean) => {
    const mode = draft.modes[modeIndex]
    const current = mode.skillBinding === 'all' ? [] : [...mode.skillBinding]
    const next = checked ? [...current, name] : current.filter((item) => item !== name)
    updateMode(modeIndex, { skillBinding: Array.from(new Set(next)) })
  }

  const runPreview = async () => {
    setPreviewError(null)
    try {
      const payload = await api.post<{ modes: Array<{ id: string; name: string; prompt: string; chars: number }> }>(
        '/api/apps/preview',
        draft,
      )
      setPreview(payload.modes)
    } catch (failure) {
      setPreviewError(failure instanceof Error ? failure.message : '预览失败')
    }
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
          {previewError ? <div className="wb-alert">{previewError}</div> : null}
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
              const bound = customBinding ? (mode.skillBinding as string[]) : []
              const unknown = bound.filter((name) => !skills.some((skill) => skill.name === name))
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
                    <small className={(mode.prompt || '').length > 4000 ? 'wb-field-error' : undefined}>
                      {(mode.prompt || '').length} / 4000
                    </small>
                  </label>

                  <div className="wb-form-grid">
                    <label className="wb-field">
                      <span>图标</span>
                      <select
                        value={mode.icon || 'spark'}
                        onChange={(event) => updateMode(modeIndex, { icon: event.target.value })}
                      >
                        {APP_ICON_NAMES.map((name) => (
                          <option key={name} value={name}>{name}</option>
                        ))}
                      </select>
                    </label>
                    <label className="wb-field">
                      <span>输入框占位符</span>
                      <input
                        value={mode.placeholder || ''}
                        maxLength={80}
                        onChange={(event) => updateMode(modeIndex, { placeholder: event.target.value })}
                        placeholder="例如：输入研究问题、PICO 或文献片段"
                      />
                    </label>
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
                  </div>

                  {customBinding ? (
                    <div className="wb-field">
                      <span>绑定技能（{bound.length} 个{bound.length === 0 ? '，本模式不加载技能' : ''}）</span>
                      {skills.length === 0 ? (
                        <p className="wb-muted">没有读取到技能目录，可先保存，稍后在技能页确认。</p>
                      ) : (
                        <div className="wb-skill-picker">
                          {skills.map((skill) => (
                            <label key={skill.name} className={skill.enabled ? '' : 'is-disabled'}>
                              <input
                                type="checkbox"
                                checked={bound.includes(skill.name)}
                                onChange={(event) => toggleSkill(modeIndex, skill.name, event.target.checked)}
                              />
                              <span>{skill.name}</span>
                              {skill.enabled ? null : <i>已停用</i>}
                            </label>
                          ))}
                        </div>
                      )}
                      {unknown.length > 0 ? (
                        <p className="wb-field-error">
                          这些技能已不存在：{unknown.join('、')}（保存后会被忽略）
                        </p>
                      ) : null}
                    </div>
                  ) : null}

                  <label className="wb-field">
                    <span>示例问题（每行一条，最多 6 条）</span>
                    <textarea
                      rows={3}
                      value={(mode.examples || []).join('\n')}
                      onChange={(event) =>
                        updateMode(modeIndex, {
                          examples: event.target.value
                            .split('\n')
                            .map((item) => item.trim())
                            .filter(Boolean)
                            .slice(0, 6),
                        })
                      }
                      placeholder="首页推荐给用户的示例问题"
                    />
                  </label>

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
                    examples: [],
                    placeholder: '',
                    icon: 'spark',
                    capsules: [],
                  },
                ],
              })
            }
          >
            + 添加模式
          </button>

          {preview ? (
            <div className="wb-preview">
              <b>组合预览</b>
              <p className="wb-muted">提示词会追加在系统提示词之后，不替换它。</p>
              {preview.map((mode) => (
                <details key={mode.id}>
                  <summary>
                    {mode.name} · {mode.chars} 字符
                  </summary>
                  <pre>{mode.prompt || '(空)'}</pre>
                </details>
              ))}
            </div>
          ) : null}
        </div>

        <footer className="wb-modal-foot">
          {onDelete ? (
            <button type="button" className="wb-link wb-link--danger" onClick={onDelete}>
              删除应用
            </button>
          ) : null}
          <span className="wb-spacer" />
          <button type="button" className="wb-button" onClick={() => void runPreview()}>预览</button>
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

interface PluginStatusRow {
  id: string
  loaded: boolean
  tools: string[]
  errors: string[]
  warnings: string[]
}

interface PendingPluginRow {
  id: string
  pluginId: string
  name: string
  version: string
  description: string
  submitter: string
  createdAt: number
}

interface AdminUser {
  uid: string
  username: string
  displayName: string
  role: string
  status: string
  mustChangePassword: boolean
  createdAt: string
  lastLoginAt: string | null
}

function UsersView({ isAdmin }: { isAdmin: boolean }) {
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({ username: '', displayName: '', password: '' })

  const load = () => {
    if (!isAdmin) return
    setError(null)
    api
      .get<{ users: AdminUser[] }>('/api/admin/users')
      .then((payload) => setUsers(payload.users))
      .catch((failure: unknown) => {
        setUsers([])
        setError(failure instanceof Error ? failure.message : '读取用户失败')
      })
  }

  useEffect(load, [isAdmin])

  const create = async () => {
    const username = form.username.trim()
    if (!username || form.password.length < 8) {
      setError('用户名不能为空，密码至少 8 位。')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/admin/users', {
        username,
        displayName: form.displayName.trim() || username,
        password: form.password,
        role: 'user',
        mustChangePassword: true,
      })
      setForm({ username: '', displayName: '', password: '' })
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '创建用户失败')
    } finally {
      setBusy(false)
    }
  }

  if (!isAdmin) {
    return (
      <div className="wb-page"><div className="wb-page-inner"><header className="wb-page-head"><div><h2>用户管理</h2><p>只有管理员可以创建和管理用户。</p></div></header></div></div>
    )
  }

  return (
    <div className="wb-page">
      <div className="wb-page-inner wb-users-page">
        <header className="wb-page-head">
          <div>
            <h2>用户管理</h2>
            <p>团队成员的账号由管理员创建；新用户首次登录必须修改密码。</p>
          </div>
          <span className="wb-chip">{users?.length ?? 0} 个用户</span>
        </header>
        {error ? <div className="wb-alert">{error}</div> : null}
        <div className="wb-user-form">
          <label>用户名<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} placeholder="例如 zhangsan" /></label>
          <label>显示名<input value={form.displayName} onChange={(event) => setForm({ ...form, displayName: event.target.value })} placeholder="例如 张三" /></label>
          <label>初始密码<input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="至少 8 位" /></label>
          <button type="button" className="wb-button" onClick={() => void create()} disabled={busy}>{busy ? '创建中…' : '创建用户'}</button>
        </div>
        <div className="wb-user-table">
          <div className="wb-user-row is-head"><span>用户名</span><span>显示名</span><span>角色</span><span>状态</span><span>最近登录</span></div>
          {users === null ? <p className="wb-muted">正在读取…</p> : users.map((user) => (
            <div className="wb-user-row" key={user.uid}>
              <b>{user.username}</b><span>{user.displayName}</span><span>{user.role === 'user' ? '成员' : '管理员'}</span><span>{user.status === 'disabled' ? '已停用' : '正常'}</span><span>{user.lastLoginAt ? formatWhen(user.lastLoginAt) : '从未'}</span>
            </div>
          ))}
        </div>
        <p className="wb-muted wb-user-hint">给同事分配用户名和初始密码后，对方打开同一地址即可登录；首次登录会要求改成自己的密码。</p>
      </div>
    </div>
  )
}

function HelpView({ authMode }: { authMode: string }) {
  return (
    <div className="wb-page">
      <div className="wb-page-inner wb-help-page">
        <header className="wb-page-head">
          <div>
            <h2>帮助</h2>
            <p>常用操作、产物和定时任务说明。</p>
          </div>
          <span className="wb-chip">{authMode === 'team' ? '团队模式' : '单机模式 · 无需登录'}</span>
        </header>
        <div className="wb-help-grid">
          <section>
            <h3>开始使用</h3>
            <p>在首页输入任务，选择工作模式或场景胶囊；发送后会创建一条会话。</p>
            <p>普通对话在「工作区」下面，点击工作区即可展开或收起该工作区的会话。</p>
          </section>
          <section>
            <h3>产物与在线编辑</h3>
            <p>模型生成的文件通过 <code>present</code> 声明为交付文件，显示在回答最后。</p>
            <p>点击「在线编辑」会在右侧打开编辑器；「产物」按钮可以浏览当前工作区的全部文件。</p>
            <p>DOCX/XLSX/PPTX 支持在线编辑，PDF 和图片支持右侧预览。</p>
          </section>
          <section>
            <h3>定时任务</h3>
            <p>「定时任务」按一次性、间隔、每天或每周运行 prompt；每次运行都会新建一个会话，把任务交给 AI 执行。</p>
            <p>运行产生的会话和执行记录在「定时任务」里，普通对话在下面「工作区」里。</p>
            <p>单机模式下任务同样保存在本地，不需要登录。</p>
          </section>
          <section>
            <h3>命令执行</h3>
            <p>命令默认自动批准，不再逐条弹权限确认；<code>sudo</code>、容器控制、读取密钥等硬安全规则仍会拒绝。</p>
            <p>如需恢复逐条确认，启动时设置 <code>PI_AUTO_APPROVE_TOOLS=0</code>。</p>
          </section>
          <section>
            <h3>快捷键</h3>
            <p><kbd>Enter</kbd> 发送，<kbd>Shift</kbd> + <kbd>Enter</kbd> 换行；<kbd>Esc</kbd> 关闭当前弹层。</p>
          </section>
          <section>
            <h3>团队成员</h3>
            <p>团队模式默认只有 <code>admin</code>。登录页现在开放自助注册，成员可以自己创建账号并直接登录。</p>
            <p>管理员也可以在「更多 → 用户管理」里创建成员、分配初始密码。</p>
            <p>用 <code>PI_ADMIN_PASSWORD</code> 预设管理员密码时，首次登录不会强制改密；随机生成的管理员密码才会要求改密。</p>
          </section>
          <section>
            <h3>单机模式与登录</h3>
            <p>单机模式没有账号和登录页，侧栏显示「本地用户」是正常的；工作空间在 <code>~/.pi/workspaces/&lt;系统用户名&gt;</code>。</p>
            <p>需要多用户登录时，用团队模式启动：设置 <code>PI_AUTH_MODE=team</code> 和管理员密码。</p>
          </section>
        </div>
      </div>
    </div>
  )
}

function PendingPanel({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<PendingPluginRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = () => {
    setItems(null)
    setError(null)
    api
      .get<{ submissions: PendingPluginRow[] }>('/api/plugin-submissions')
      .then((payload) => setItems(payload.submissions))
      .catch((failure: unknown) => {
        setItems([])
        setError(failure instanceof Error ? failure.message : '读取待审插件失败')
      })
  }

  useEffect(load, [])

  const act = async (item: PendingPluginRow, action: 'approve' | 'reject') => {
    setBusy(item.id)
    setError(null)
    try {
      if (action === 'approve') {
        await api.post(`/api/plugin-submissions/${encodeURIComponent(item.id)}/approve`, {})
      } else {
        await api.del(`/api/plugin-submissions/${encodeURIComponent(item.id)}`)
      }
      load()
      onChanged()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '操作失败')
    } finally {
      setBusy(null)
    }
  }

  if (items === null) {
    return <div className="wb-empty"><b>正在读取待审插件…</b></div>
  }
  if (items.length === 0) {
    return (
      <div className="wb-empty">
        <b>没有待审插件</b>
        <p>作者在「已安装」页点「提交插件」上传 zip；审核通过后才会真正安装。</p>
      </div>
    )
  }
  return (
    <div className="wb-plugin-list">
      {error ? <div className="wb-alert">{error}</div> : null}
      {items.map((item) => (
        <article key={item.id} className="wb-plugin">
          <span className="wb-plugin-icon"><Icon name="plugin" size={19} /></span>
          <div className="wb-plugin-body">
            <b>{item.name} <i>v{item.version}</i></b>
            <p>{item.description || '没有描述'}</p>
            <p className="wb-plugin-status">
              提交人 {item.submitter || '未知'} · {new Date(item.createdAt * 1000).toLocaleString()}
            </p>
          </div>
          <button
            type="button"
            className="wb-link"
            onClick={() => void act(item, 'approve')}
            disabled={busy === item.id}
          >
            安装
          </button>
          <button
            type="button"
            className="wb-link wb-link--danger"
            onClick={() => void act(item, 'reject')}
            disabled={busy === item.id}
          >
            驳回
          </button>
        </article>
      ))}
    </div>
  )
}

function PluginsView({ appId }: { appId: string }) {
  const [plugins, setPlugins] = useState<PluginRow[] | null>(null)
  const [view, setView] = useState<'installed' | 'pending'>('installed')
  const [canManage, setCanManage] = useState(false)
  const [canInstall, setCanInstall] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [status, setStatus] = useState<Record<string, PluginStatusRow>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const uploadRef = useRef<HTMLInputElement>(null)
  const submitRef = useRef<HTMLInputElement>(null)

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
    // Status is best effort: a user with no live worker yet still sees the list.
    api
      .get<{ plugins: PluginStatusRow[] }>(`/api/plugins/status?app=${encodeURIComponent(appId)}`)
      .then((payload) => {
        const next: Record<string, PluginStatusRow> = {}
        for (const item of payload.plugins) next[item.id] = item
        setStatus(next)
      })
      .catch(() => setStatus({}))
  }

  useEffect(load, [appId])

  const install = async (file: File) => {
    setBusy('install')
    setError(null)
    try {
      await api.postRaw('/api/plugins/install', await file.arrayBuffer())
      load()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '安装失败')
    } finally {
      setBusy(null)
    }
  }

  const submit = async (file: File) => {
    setBusy('submit')
    setError(null)
    setNotice(null)
    try {
      const payload = await api.postRaw<{ pluginId: string; name: string }>(
        '/api/plugin-submissions',
        await file.arrayBuffer(),
      )
      setNotice(`已提交「${payload.name}」，等待管理员审核后即可安装。`)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '提交失败')
    } finally {
      setBusy(null)
    }
  }

  const uninstall = async (plugin: PluginRow) => {
    const uninstallConfirmed = await askConfirm({
      title: '卸载插件',
      message: `卸载插件「${plugin.name}」？目录和所有租户的启用记录都会删除。`,
      confirmText: '卸载',
      danger: true,
    })
    if (!uninstallConfirmed) return
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
            {canInstall ? (
              <button type="button" className={view === 'pending' ? 'is-active' : ''} onClick={() => setView('pending')}>待审</button>
            ) : null}
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
          {view === 'installed' && canManage ? (
            <>
              <button type="button" className="wb-button" onClick={() => submitRef.current?.click()} disabled={busy === 'submit'}>
                {busy === 'submit' ? '提交中…' : '提交插件'}
              </button>
              <input
                ref={submitRef}
                type="file"
                accept=".zip,application/zip"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) void submit(file)
                }}
              />
            </>
          ) : null}
        </header>

        {notice ? <div className="wb-alert">{notice}</div> : null}
        {view === 'pending' ? (
          <PendingPanel onChanged={load} />
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
                  {status[plugin.id] ? (
                    <p className="wb-plugin-status">
                      {status[plugin.id].errors.length > 0
                        ? `加载失败：${status[plugin.id].errors[0]}`
                        : status[plugin.id].loaded
                          ? `已加载 ${status[plugin.id].tools.length} 个工具${
                              status[plugin.id].tools.length
                                ? `：${status[plugin.id].tools.join('、')}`
                                : ''
                            }`
                          : '尚未加载（下一条消息生效）'}
                    </p>
                  ) : null}
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
    const removeConfirmed = await askConfirm({
      title: '删除定时任务',
      message: `删除「${schedule.name}」？运行历史也会删除。`,
      confirmText: '删除',
      danger: true,
    })
    if (!removeConfirmed) return
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
            <p>每个任务按计划新建一个会话，把 prompt 交给 AI 执行；运行产生的会话和历史都记录在这里。</p>
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
                <span>工作区</span>
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
