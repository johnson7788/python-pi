import { useEffect, useRef, useState } from 'react'

// Manage installed extension skills (Anthropic Agent Skills packs). Self-contained
// like ImageGallery: reads the token + does its own authed fetches, inline styles,
// so it drops into any app with zero per-app CSS.

export interface SkillItem {
  id: string
  name: string
  description: string
  version: string | null
  enabled: boolean
  size: number
  source: string
}

const TOKEN_KEY = 'aioffice_token'

function authHeaders(): HeadersInit {
  const t = localStorage.getItem(TOKEN_KEY)
  return t ? { Authorization: `Bearer ${t}` } : {}
}

/** SkillhubApi impl for createSkillhubSkill (agent-core): self-contained authed
 * fetches to /skills, uniform across all 5 apps (same token key). */
export function createSkillApi() {
  const idByName = async (name: string): Promise<string | null> => {
    const r = await fetch('/skills', { headers: authHeaders() })
    if (!r.ok) return null
    return ((await r.json()) as SkillItem[]).find((s) => s.name === name)?.id ?? null
  }
  return {
    async list(): Promise<{ name: string; description: string }[]> {
      const r = await fetch('/skills?enabled=1', { headers: authHeaders() })
      if (!r.ok) return []
      return ((await r.json()) as SkillItem[]).map((s) => ({ name: s.name, description: s.description }))
    },
    async loadBody(name: string): Promise<string> {
      const id = await idByName(name)
      if (!id) throw new Error('not found')
      const r = await fetch(`/skills/${id}`, { headers: authHeaders() })
      if (!r.ok) throw new Error('not found')
      return ((await r.json()) as { body: string }).body
    },
    async readFile(name: string, path: string): Promise<string> {
      const id = await idByName(name)
      if (!id) throw new Error('not found')
      const r = await fetch(`/skills/${id}/file?path=${encodeURIComponent(path)}`, { headers: authHeaders() })
      if (!r.ok) throw new Error('not found')
      return ((await r.json()) as { text: string }).text
    },
  }
}

export interface SkillManagerProps {
  onClose?: () => void
}

export function SkillManager({ onClose }: SkillManagerProps) {
  const [items, setItems] = useState<SkillItem[]>([])
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState('')
  const [url, setUrl] = useState('')
  const [detail, setDetail] = useState<{ name: string; body: string; files: string[] } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  async function load() {
    setLoading(true)
    setMsg('')
    try {
      const r = await fetch('/skills', { headers: authHeaders() })
      if (!r.ok) throw new Error()
      const list = (await r.json()) as SkillItem[]
      setItems(list)
      if (!list.length) setMsg('还没有安装技能。上传 SKILL.md 技能包（.zip），或粘贴下载链接安装。')
    } catch {
      setMsg('加载技能失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function doUpload(files: FileList | null) {
    const f = files?.[0]
    if (!f) return
    setLoading(true)
    setMsg('')
    try {
      const r = await fetch('/skills/upload', {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/zip' },
        body: f,
      })
      if (!r.ok) {
        const e = (await r.json().catch(() => ({}))) as { detail?: string }
        throw new Error(e.detail || '')
      }
      await load()
    } catch (e) {
      setMsg(`安装失败：${(e as Error).message || '请检查技能包格式'}`)
      setLoading(false)
    }
  }

  async function doInstall() {
    const u = url.trim()
    if (!u) return
    setLoading(true)
    setMsg('')
    try {
      const r = await fetch('/skills/install', {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: u }),
      })
      if (!r.ok) {
        const e = (await r.json().catch(() => ({}))) as { detail?: string }
        throw new Error(e.detail || '')
      }
      setUrl('')
      await load()
    } catch (e) {
      setMsg(`安装失败：${(e as Error).message || '请检查链接'}`)
      setLoading(false)
    }
  }

  async function toggle(s: SkillItem) {
    setItems((xs) => xs.map((x) => (x.id === s.id ? { ...x, enabled: !x.enabled } : x)))
    await fetch(`/skills/${s.id}`, {
      method: 'PATCH',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !s.enabled }),
    }).catch(() => void load())
  }

  async function remove(s: SkillItem) {
    setItems((xs) => xs.filter((x) => x.id !== s.id))
    await fetch(`/skills/${s.id}`, { method: 'DELETE', headers: authHeaders() }).catch(() => {})
  }

  async function openDetail(s: SkillItem) {
    setLoading(true)
    try {
      const r = await fetch(`/skills/${s.id}`, { headers: authHeaders() })
      if (!r.ok) throw new Error()
      const d = (await r.json()) as { name: string; body: string; files: string[] }
      setDetail(d)
    } catch {
      setMsg('读取详情失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={S.panel}>
      <div style={S.head}>
        <div style={{ fontSize: 16, fontWeight: 600 }}>技能中心</div>
        <div style={{ flex: 1 }} />
        <button style={S.link} onClick={() => window.open('https://skillhub.cn/', '_blank')}>
          浏览 skillhub.cn
        </button>
        {onClose && (
          <button style={S.close} onClick={onClose} aria-label="关闭">
            ×
          </button>
        )}
      </div>

      <div style={S.bar}>
        <button style={S.btn} onClick={() => fileRef.current?.click()} disabled={loading}>
          上传技能包(.zip)
        </button>
        <input
          style={S.input}
          value={url}
          placeholder="粘贴技能包下载链接（https）…"
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void doInstall()
          }}
        />
        <button style={S.btn} onClick={() => void doInstall()} disabled={loading || !url.trim()}>
          安装
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".zip"
          style={{ display: 'none' }}
          onChange={(e) => void doUpload(e.target.files)}
        />
      </div>

      {msg && <div style={S.hint}>{loading ? '处理中…' : msg}</div>}
      {loading && !msg && <div style={S.hint}>加载中…</div>}

      <div style={S.list}>
        {items.map((s) => (
          <div key={s.id} style={S.row}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={S.name}>
                {s.name}
                {s.version && <span style={S.ver}>v{s.version}</span>}
              </div>
              <div style={S.desc}>{s.description}</div>
            </div>
            <button style={S.rowBtn} onClick={() => void openDetail(s)}>
              查看
            </button>
            <label style={S.switch} title={s.enabled ? '已启用' : '已停用'}>
              <input type="checkbox" checked={s.enabled} onChange={() => void toggle(s)} />
              <span>{s.enabled ? '启用' : '停用'}</span>
            </label>
            <button style={S.del} onClick={() => void remove(s)} aria-label="删除" title="删除">
              删除
            </button>
          </div>
        ))}
      </div>

      {detail && (
        <div style={S.detailBackdrop} onClick={() => setDetail(null)}>
          <div style={S.detailCard} onClick={(e) => e.stopPropagation()}>
            <div style={S.head}>
              <div style={{ fontSize: 15, fontWeight: 600 }}>{detail.name}</div>
              <div style={{ flex: 1 }} />
              <button style={S.close} onClick={() => setDetail(null)} aria-label="关闭">
                ×
              </button>
            </div>
            {detail.files.length > 1 && (
              <div style={S.files}>文件：{detail.files.join(' · ')}</div>
            )}
            <pre style={S.body}>{detail.body}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

const S: Record<string, React.CSSProperties> = {
  panel: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, gap: 10, padding: 14 },
  head: { display: 'flex', alignItems: 'center', gap: 8 },
  bar: { display: 'flex', gap: 8, alignItems: 'center' },
  input: {
    flex: 1, padding: '6px 10px', borderRadius: 8,
    border: '1px solid var(--border, #d0d5dd)', fontSize: 14, outline: 'none',
  },
  btn: {
    padding: '6px 14px', borderRadius: 8, border: 'none',
    background: 'var(--accent, #2563eb)', color: '#fff', fontSize: 14, cursor: 'pointer', whiteSpace: 'nowrap',
  },
  link: {
    padding: '4px 10px', borderRadius: 8, border: '1px solid var(--border, #d0d5dd)',
    background: 'transparent', color: '#475467', fontSize: 13, cursor: 'pointer', whiteSpace: 'nowrap',
  },
  close: { border: 'none', background: 'transparent', fontSize: 22, lineHeight: 1, cursor: 'pointer', color: '#667085' },
  hint: { fontSize: 13, color: '#667085', padding: '2px 2px' },
  list: { flex: 1, minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 },
  row: {
    display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px',
    border: '1px solid var(--border, #eaecf0)', borderRadius: 10, background: '#fff',
  },
  name: { fontSize: 14, fontWeight: 600, color: '#101828', display: 'flex', alignItems: 'center', gap: 8 },
  ver: { fontSize: 12, color: '#667085', fontWeight: 400 },
  desc: { fontSize: 13, color: '#667085', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  rowBtn: {
    padding: '4px 10px', borderRadius: 8, border: '1px solid var(--border, #d0d5dd)',
    background: 'transparent', color: '#475467', fontSize: 13, cursor: 'pointer', whiteSpace: 'nowrap',
  },
  switch: { display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, color: '#475467', cursor: 'pointer', whiteSpace: 'nowrap' },
  del: {
    padding: '4px 10px', borderRadius: 8, border: '1px solid #fda29b',
    background: 'transparent', color: '#d92d20', fontSize: 13, cursor: 'pointer', whiteSpace: 'nowrap',
  },
  detailBackdrop: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex',
    alignItems: 'center', justifyContent: 'center', zIndex: 1000,
  },
  detailCard: {
    width: 'min(720px, 92vw)', height: '76vh', background: '#fff', borderRadius: 12,
    padding: 14, display: 'flex', flexDirection: 'column', gap: 10,
  },
  files: { fontSize: 12, color: '#667085' },
  body: {
    flex: 1, minHeight: 0, overflow: 'auto', margin: 0, padding: 12, borderRadius: 8,
    background: '#f9fafb', fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap', fontFamily: 'inherit',
  },
}
