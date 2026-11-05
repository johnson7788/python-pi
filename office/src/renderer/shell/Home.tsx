import { ImageGallery, SkillManager } from '@genoffice/ui'
import { useEffect, useMemo, useState } from 'react'
import { BrandMark } from './BrandMark'
import {
  type DocMeta,
  type ProjectMeta,
  type ShareMeta,
  type VersionMeta,
  appUrl,
  clearToken,
  createShare,
  downloadDocument,
  listDocuments,
  listProjects,
  listShares,
  listVersions,
  moveToProject,
  projectDocIds,
  purgeDocument,
  restoreDocument,
  restoreVersion,
  revokeShare,
  setStar,
  shareUrl,
  thumbObjectUrl,
  trashDocument,
} from '../web-adapter'

// A generation prompt is handed to the docs editor via sessionStorage; the docs
// App picks it up on boot and auto-runs the agent (see App.tsx boot effect).
function openWithPrompt(prompt: string, onOpen: () => void) {
  sessionStorage.setItem('aioffice.pendingPrompt', prompt)
  onOpen()
}
// Mind-map docs are Markdown, opened in the markdown app with the mind-map view on.
const isMindmap = (title: string) => /思维导图|脑图|\.mindmap\.md$/i.test(title)

function openDoc(doc: DocMeta, onOpen: () => void) {
  // Route by file type: pptx→slides, xlsx→sheets, mindmap md→markdown, else→docs.
  if (isMindmap(doc.title)) {
    const u = new URL(appUrl('markdown')) // appUrl already carries ?tok= in dev
    u.searchParams.set('doc', doc.id) // markdown app consumes ?doc=
    u.searchParams.set('view', 'mindmap')
    location.href = u.toString()
    return
  }
  if (/\.pdf$/i.test(doc.title)) {
    const u = new URL(appUrl('pdf')) // appUrl already carries ?tok= in dev
    u.searchParams.set('doc', doc.id) // pdf app consumes ?doc=
    location.href = u.toString()
    return
  }
  const cross =
    /\.pptx$/i.test(doc.title) ? 'slides' : /\.(xlsx|xls|csv)$/i.test(doc.title) ? 'sheets' : null
  if (cross) {
    const u = new URL(appUrl(cross)) // appUrl already carries ?tok= in dev
    u.searchParams.set('open', doc.id)
    location.href = u.toString()
    return
  }
  sessionStorage.setItem('aioffice.pendingOpen', doc.id)
  onOpen()
}
// Other apps live on separate origins; hand the prompt over via ?gen= (works
// cross-origin in dev where sessionStorage wouldn't). `extra` carries e.g. ?view=.
function goApp(app: 'slides' | 'sheets' | 'markdown', prompt?: string, extra?: Record<string, string>) {
  const u = new URL(appUrl(app))
  if (prompt) u.searchParams.set('gen', prompt)
  for (const [k, v] of Object.entries(extra ?? {})) u.searchParams.set(k, v)
  location.href = u.toString()
}
const goSlides = (prompt?: string) => goApp('slides', prompt)
const goSheets = (prompt?: string) => goApp('sheets', prompt)
const goMindmap = (prompt?: string) => goApp('markdown', prompt, { view: 'mindmap' })

type Kind = 'docs' | 'slides' | 'sheets' | 'mindmap'

const KINDS: { id: Kind; label: string }[] = [
  { id: 'docs', label: '文字文档' },
  { id: 'slides', label: '演示文稿' },
  { id: 'sheets', label: '表格' },
  { id: 'mindmap', label: '思维导图' },
]

// Small type badge shown on recent cards / filter nav. `other` = pdf / plain md.
const BADGE: Record<Kind | 'other', { label: string; cls: string }> = {
  docs: { label: 'W', cls: 'k-docs' },
  slides: { label: 'P', cls: 'k-slides' },
  sheets: { label: 'X', cls: 'k-sheets' },
  mindmap: { label: '图', cls: 'k-mind' },
  other: { label: '·', cls: 'k-other' },
}

const KIND_LABEL: Record<Kind | 'other', string> = {
  docs: '文字文档',
  slides: '演示文稿',
  sheets: '表格',
  mindmap: '思维导图',
  other: '其他文档',
}

function docKind(doc: DocMeta): Kind | 'other' {
  if (isMindmap(doc.title)) return 'mindmap'
  switch (doc.type) {
    case 'docx':
      return 'docs'
    case 'pptx':
      return 'slides'
    case 'xlsx':
      return 'sheets'
    default:
      return 'other' // pdf / plain md
  }
}

// Card preview: real thumbnail (if the doc has one on the backend) else a
// gradient type-tile with the type glyph. ponytail: thumbnails are produced by
// each editor on save (slides wired first); tile is the graceful fallback.
function DocThumb({ doc }: { doc: DocMeta }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    let got: string | null = null
    void thumbObjectUrl(doc.id).then((u) => {
      if (live) setUrl((got = u))
      else if (u) URL.revokeObjectURL(u)
    })
    return () => {
      live = false
      if (got) URL.revokeObjectURL(got)
    }
  }, [doc.id])
  const b = BADGE[docKind(doc)]
  if (url) return <img className="home-doc-thumb" src={url} alt="" />
  return <div className={`home-doc-thumb tile ${b.cls}`}>{b.label}</div>
}

function relTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  const m = Math.floor(ms / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d} 天前`
  return new Date(iso).toLocaleDateString()
}

const QUICK: Record<Kind, { title: string; prompt: string }[]> = {
  docs: [
    { title: '写一份研究报告', prompt: '帮我写一份关于人工智能行业现状与趋势的研究报告，包含要点小节。' },
    { title: '起草商业计划', prompt: '帮我起草一份初创公司的商业计划书大纲，并展开第一部分。' },
    { title: '整理会议纪要', prompt: '把以下要点整理成结构化的会议纪要：' },
    { title: '润色一段文字', prompt: '帮我润色并改写下面这段文字，使其更专业：' },
  ],
  slides: [
    { title: '产品发布演示', prompt: '帮我做一份新产品发布的演示文稿，包含产品亮点、市场分析和路线图。' },
    { title: '项目汇报', prompt: '帮我做一份项目进展汇报的幻灯片，包含目标、进度、风险和下一步。' },
    { title: '培训课件', prompt: '帮我做一份面向新员工的入职培训课件。' },
    { title: '路演融资', prompt: '帮我做一份初创公司融资路演的 PPT 大纲。' },
  ],
  sheets: [
    { title: '月度预算表', prompt: '帮我做一份家庭月度预算表，包含收入、各项支出分类和结余合计。' },
    { title: '销售数据分析', prompt: '帮我做一份季度销售数据表，含各产品线销量、金额和环比增长率。' },
    { title: '项目进度跟踪', prompt: '帮我做一份项目任务进度跟踪表，含任务、负责人、截止日期和状态。' },
    { title: '财务三表模型', prompt: '帮我搭建一个简单的财务模型，包含利润表、资产负债表和现金流量表。' },
  ],
  mindmap: [
    { title: '知识梳理', prompt: '帮我用多级 Markdown 大纲梳理"机器学习基础"的知识框架，用作思维导图。' },
    { title: '项目规划', prompt: '帮我用层级 Markdown 大纲规划一个新产品从立项到上线的思维导图。' },
    { title: '读书笔记', prompt: '帮我把一本书的核心观点整理成多级 Markdown 大纲思维导图。' },
    { title: '头脑风暴', prompt: '围绕"提升团队效率"做一次头脑风暴，用层级 Markdown 大纲输出思维导图。' },
  ],
}

export function Home({ onOpenEditor }: { onOpenEditor: () => void }) {
  const [prompt, setPrompt] = useState('')
  const [kind, setKind] = useState<Kind>('docs')
  const [recent, setRecent] = useState<DocMeta[]>([])
  // filter = 'all' | a Kind | 'other' | `p:<projectId>`
  const [filter, setFilter] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [projects, setProjects] = useState<ProjectMeta[]>([])
  // projectId → set of doc ids (from each project's chat timeline)
  const [projDocs, setProjDocs] = useState<Record<string, Set<string>>>({})
  // version-history modal: the doc whose versions are shown (null = closed)
  const [versionsFor, setVersionsFor] = useState<DocMeta | null>(null)
  const [versions, setVersions] = useState<VersionMeta[]>([])
  // share modal: the doc whose share links are shown (null = closed)
  const [shareFor, setShareFor] = useState<DocMeta | null>(null)
  const [shares, setShares] = useState<ShareMeta[]>([])
  // recycle bin (loaded lazily when the 回收站 filter is active)
  const [trash, setTrash] = useState<DocMeta[]>([])
  // move-to-project modal: the doc being moved (null = closed)
  const [moveFor, setMoveFor] = useState<DocMeta | null>(null)
  // right-side preview panel: the selected recent card (null = hidden)
  const [selected, setSelected] = useState<DocMeta | null>(null)
  // standalone 我的图库 overlay (browse all uploaded / extracted images)
  const [showGallery, setShowGallery] = useState(false)
  const [showSkills, setShowSkills] = useState(false)

  useEffect(() => {
    void listDocuments().then(setRecent)
    void listProjects().then(async (ps) => {
      setProjects(ps)
      const map: Record<string, Set<string>> = {}
      await Promise.all(
        ps.map(async (p) => {
          map[p.id] = new Set(await projectDocIds(p.id))
        }),
      )
      setProjDocs(map)
    })
  }, [])

  // filter nav counts (by type + by project) derive from the recent list
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: recent.length }
    for (const d of recent) c[docKind(d)] = (c[docKind(d)] ?? 0) + 1
    for (const p of projects) c[`p:${p.id}`] = recent.filter((d) => projDocs[p.id]?.has(d.id)).length
    c.starred = recent.filter((d) => d.starred).length
    c.trash = trash.length
    return c
  }, [recent, projects, projDocs, trash])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    const base = filter === 'trash' ? trash : recent
    const inFilter = (d: DocMeta) => {
      if (filter === 'all' || filter === 'trash') return true
      if (filter === 'starred') return !!d.starred
      if (filter.startsWith('p:')) return projDocs[filter.slice(2)]?.has(d.id) ?? false
      return docKind(d) === filter
    }
    return base.filter((d) => inFilter(d) && (!q || d.title.toLowerCase().includes(q)))
  }, [recent, trash, filter, query, projDocs])

  const create = (k: Kind, text?: string) => {
    if (k === 'slides') goSlides(text)
    else if (k === 'sheets') goSheets(text)
    else if (k === 'mindmap') goMindmap(text)
    else if (text) openWithPrompt(text, onOpenEditor)
    else onOpenEditor()
  }

  const send = () => {
    const text = prompt.trim()
    if (text) create(kind, text)
  }

  const openVersions = async (d: DocMeta) => {
    setVersionsFor(d)
    setVersions(await listVersions(d.id))
  }
  const doRestore = async (verId: string) => {
    if (!versionsFor) return
    if (await restoreVersion(versionsFor.id, verId)) {
      setVersions(await listVersions(versionsFor.id))
      void listDocuments().then(setRecent) // updated time reflects the new latest version
    }
  }

  // lazy-load the recycle bin when its filter becomes active
  useEffect(() => {
    if (filter === 'trash') void listDocuments(true).then(setTrash)
  }, [filter])

  const toggleStar = async (d: DocMeta) => {
    const next = !d.starred
    if (await setStar(d.id, next))
      setRecent((r) => r.map((x) => (x.id === d.id ? { ...x, starred: next } : x)))
  }
  const doTrash = async (d: DocMeta) => {
    if (await trashDocument(d.id)) setRecent((r) => r.filter((x) => x.id !== d.id))
  }
  const doRestoreDoc = async (d: DocMeta) => {
    if (await restoreDocument(d.id)) {
      setTrash((t) => t.filter((x) => x.id !== d.id))
      void listDocuments().then(setRecent)
    }
  }
  const doPurge = async (d: DocMeta) => {
    if (!confirm(`彻底删除「${d.title}」？此操作不可恢复。`)) return
    if (await purgeDocument(d.id)) setTrash((t) => t.filter((x) => x.id !== d.id))
  }
  const doMove = async (projectId: string) => {
    if (!moveFor) return
    await moveToProject(moveFor.id, projectId)
    setMoveFor(null)
    // refresh project→doc map so the sidebar counts/filters reflect the move
    const map: Record<string, Set<string>> = {}
    await Promise.all(projects.map(async (p) => { map[p.id] = new Set(await projectDocIds(p.id)) }))
    setProjDocs(map)
  }

  const openShare = async (d: DocMeta) => {
    setShareFor(d)
    setShares(await listShares(d.id))
  }
  const doCreateShare = async () => {
    if (!shareFor) return
    if (await createShare(shareFor.id)) setShares(await listShares(shareFor.id))
  }
  const doRevokeShare = async (token: string) => {
    if (!shareFor) return
    if (await revokeShare(shareFor.id, token)) setShares(await listShares(shareFor.id))
  }

  const NAV: { id: string; label: string }[] = [
    { id: 'all', label: '全部' },
    { id: 'starred', label: '收藏' },
    ...KINDS.map((k) => ({ id: k.id, label: k.label })),
    { id: 'trash', label: '回收站' },
  ]
  const filterLabel =
    filter === 'all'
      ? '最近'
      : (NAV.find((n) => n.id === filter)?.label ??
        projects.find((p) => `p:${p.id}` === filter)?.name ??
        '文档')

  return (
    <div className="home">
      <aside className="home-side">
        <div className="home-logo">
          <BrandMark size={26} />
          <span className="brand-word">AI Office</span>
        </div>
        <div className="home-new-row">
          <button className="home-new" onClick={() => create('docs')}>+ 文字</button>
          <button className="home-new" onClick={() => create('slides')}>+ 演示</button>
          <button className="home-new" onClick={() => create('sheets')}>+ 表格</button>
          <button className="home-new" onClick={() => create('mindmap')}>+ 导图</button>
        </div>
        <nav className="home-nav">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={`home-nav-item${filter === n.id ? ' active' : ''}`}
              onClick={() => setFilter(n.id)}
            >
              <span>{n.label}</span>
              {counts[n.id] ? <span className="home-nav-count">{counts[n.id]}</span> : null}
            </button>
          ))}
          <button className="home-nav-item" onClick={() => setShowGallery(true)}>
            <span>我的图库</span>
          </button>
          <button className="home-nav-item" onClick={() => setShowSkills(true)}>
            <span>技能中心</span>
          </button>
        </nav>
        {projects.length > 0 && (
          <>
            <div className="home-nav-group">项目</div>
            <nav className="home-nav">
              {projects.map((p) => {
                const id = `p:${p.id}`
                return (
                  <button
                    key={p.id}
                    className={`home-nav-item${filter === id ? ' active' : ''}`}
                    onClick={() => setFilter(id)}
                  >
                    <span className="home-nav-name">{p.name}</span>
                    {counts[id] ? <span className="home-nav-count">{counts[id]}</span> : null}
                  </button>
                )
              })}
            </nav>
          </>
        )}
        <div className="home-side-spacer" />
        <div className="home-account">
          <div className="home-avatar">AI</div>
          <span className="home-account-name">我的工作台</span>
          <button
            className="home-logout"
            title="退出登录"
            onClick={() => { clearToken(); location.reload() }}
          >
            退出
          </button>
        </div>
      </aside>

      <main className="home-main">
        <div className="home-topbar">
          <input
            className="home-search"
            placeholder="搜索文档…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <section className="home-hero">
          <h1 className="home-heading">
            构建高效办公，从 AI 开始 <span className="home-wave">👋</span>
          </h1>
          <p className="home-subheading">智能文档生成 · 高效内容创作 · 让工作更简单</p>
          <div className="home-kind">
            {KINDS.map((k) => (
              <button
                key={k.id}
                className={`home-kind-btn${kind === k.id ? ' active' : ''}`}
                onClick={() => setKind(k.id)}
              >
                {k.label}
              </button>
            ))}
          </div>
          <div className="home-cards">
            {QUICK[kind].map((q) => (
              <button key={q.title} className="home-card" onClick={() => create(kind, q.prompt)}>
                {q.title}
              </button>
            ))}
          </div>
          <div className="home-composer">
            <textarea
              className="home-input"
              placeholder={
                kind === 'slides'
                  ? '描述你想要的演示文稿，AI 会帮你生成 PPT…'
                  : kind === 'sheets'
                    ? '描述你想要的表格，AI 会帮你生成 Excel…'
                    : kind === 'mindmap'
                      ? '描述你想要的主题，AI 会帮你生成思维导图…'
                      : '描述你想创建的内容，AI 会帮你生成文档…'
              }
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send()
              }}
            />
            <button className="home-send" onClick={send} disabled={!prompt.trim()}>
              生成内容
            </button>
          </div>
          <div className="home-hint">Cmd/Ctrl + Enter 发送 · 点快捷卡片直接生成</div>
        </section>

        <section className="home-recent">
          <div className="home-recent-title">{filterLabel}</div>
          {shown.length === 0 ? (
            <div className="home-empty">{query ? '没有匹配的文档' : '还没有文档'}</div>
          ) : (
            <div className="home-grid">
              {shown.map((d) => {
                return (
                  <div
                    key={d.id}
                    className={`home-doc${selected?.id === d.id ? ' selected' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => (filter === 'trash' ? undefined : setSelected(d))}
                    onDoubleClick={() => openDoc(d, onOpenEditor)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') openDoc(d, onOpenEditor)
                    }}
                  >
                    <div className="home-doc-thumb-wrap">
                      <DocThumb doc={d} />
                      {filter !== 'trash' && (
                        <button
                          className={`home-doc-star${d.starred ? ' on' : ''}`}
                          title={d.starred ? '取消收藏' : '收藏'}
                          onClick={(e) => { e.stopPropagation(); void toggleStar(d) }}
                        >
                          {d.starred ? '★' : '☆'}
                        </button>
                      )}
                    </div>
                    <span className="home-doc-title">{d.title}</span>
                    <span className="home-doc-time">{relTime(d.updated)}</span>
                    <div className="home-doc-actions">
                      {filter === 'trash' ? (
                        <>
                          <button title="恢复" onClick={(e) => { e.stopPropagation(); void doRestoreDoc(d) }}>
                            恢复
                          </button>
                          <button className="home-doc-danger" title="彻底删除" onClick={(e) => { e.stopPropagation(); void doPurge(d) }}>
                            彻底删除
                          </button>
                        </>
                      ) : (
                        <>
                          <button title="下载" onClick={(e) => { e.stopPropagation(); void downloadDocument(d.id, d.title) }}>
                            下载
                          </button>
                          <button title="历史版本" onClick={(e) => { e.stopPropagation(); void openVersions(d) }}>
                            历史
                          </button>
                          <button title="分享只读链接" onClick={(e) => { e.stopPropagation(); void openShare(d) }}>
                            分享
                          </button>
                          {projects.length > 0 && (
                            <button title="移动到项目" onClick={(e) => { e.stopPropagation(); setMoveFor(d) }}>
                              移动
                            </button>
                          )}
                          <button className="home-doc-danger" title="删除到回收站" onClick={(e) => { e.stopPropagation(); void doTrash(d) }}>
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </main>

      {selected && (
        <aside className="home-preview">
          <button className="home-preview-close" onClick={() => setSelected(null)}>✕</button>
          <div className="home-preview-thumb">
            <DocThumb doc={selected} />
          </div>
          <div className="home-preview-title">{selected.title}</div>
          <dl className="home-preview-meta">
            <div><dt>类型</dt><dd>{KIND_LABEL[docKind(selected)]}</dd></div>
            <div><dt>更新</dt><dd>{new Date(selected.updated).toLocaleString()}</dd></div>
          </dl>
          <div className="home-preview-actions">
            <button className="home-preview-open" onClick={() => openDoc(selected, onOpenEditor)}>
              打开
            </button>
            <button onClick={() => void downloadDocument(selected.id, selected.title)}>下载</button>
            <button onClick={() => void openShare(selected)}>分享</button>
          </div>
        </aside>
      )}

      {showGallery && (
        <div className="home-modal-backdrop" onClick={() => setShowGallery(false)}>
          <div
            className="home-modal"
            style={{ width: 'min(860px, 92vw)', height: '80vh', padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <ImageGallery
              search={(q, max) => window.desktop.imageSearch(q, max)}
              gallery={{ docId: null }} // browse-only; 上传文献提取 works without an open doc
              onPick={() => {}} // browse-only: no editor to insert into
              onClose={() => setShowGallery(false)}
            />
          </div>
        </div>
      )}

      {showSkills && (
        <div className="home-modal-backdrop" onClick={() => setShowSkills(false)}>
          <div
            className="home-modal"
            style={{ width: 'min(860px, 92vw)', height: '80vh', padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <SkillManager onClose={() => setShowSkills(false)} />
          </div>
        </div>
      )}

      {versionsFor && (
        <div className="home-modal-backdrop" onClick={() => setVersionsFor(null)}>
          <div className="home-modal" onClick={(e) => e.stopPropagation()}>
            <div className="home-modal-head">
              <span className="home-modal-title">历史版本 · {versionsFor.title}</span>
              <button className="home-modal-close" onClick={() => setVersionsFor(null)}>✕</button>
            </div>
            {versions.length === 0 ? (
              <div className="home-empty">暂无版本</div>
            ) : (
              <ul className="home-ver-list">
                {versions.map((v, i) => (
                  <li key={v.id} className="home-ver">
                    <span className="home-ver-info">
                      {i === 0 ? '当前版本' : `版本 ${versions.length - i}`}
                      <span className="home-ver-meta">
                        {new Date(v.created).toLocaleString()} · {(v.size / 1024).toFixed(0)} KB
                      </span>
                    </span>
                    {i !== 0 && (
                      <button className="home-ver-restore" onClick={() => void doRestore(v.id)}>
                        恢复
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {shareFor && (
        <div className="home-modal-backdrop" onClick={() => setShareFor(null)}>
          <div className="home-modal" onClick={(e) => e.stopPropagation()}>
            <div className="home-modal-head">
              <span className="home-modal-title">分享 · {shareFor.title}</span>
              <button className="home-modal-close" onClick={() => setShareFor(null)}>✕</button>
            </div>
            {shares.length === 0 ? (
              <div className="home-empty">还没有分享链接</div>
            ) : (
              <ul className="home-ver-list">
                {shares.map((s) => (
                  <li key={s.token} className="home-ver">
                    <input className="home-share-url" readOnly value={shareUrl(s.token)} />
                    <button
                      className="home-ver-restore"
                      onClick={() => void navigator.clipboard.writeText(shareUrl(s.token))}
                    >
                      复制
                    </button>
                    <button className="home-share-revoke" onClick={() => void doRevokeShare(s.token)}>
                      撤销
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <button className="home-send home-share-new" onClick={() => void doCreateShare()}>
              + 新建只读链接
            </button>
          </div>
        </div>
      )}

      {moveFor && (
        <div className="home-modal-backdrop" onClick={() => setMoveFor(null)}>
          <div className="home-modal" onClick={(e) => e.stopPropagation()}>
            <div className="home-modal-head">
              <span className="home-modal-title">移动到项目 · {moveFor.title}</span>
              <button className="home-modal-close" onClick={() => setMoveFor(null)}>✕</button>
            </div>
            <ul className="home-ver-list">
              {projects.map((p) => (
                <li key={p.id} className="home-ver">
                  <span className="home-ver-info">{p.name}</span>
                  <button className="home-ver-restore" onClick={() => void doMove(p.id)}>
                    移动到此
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}
