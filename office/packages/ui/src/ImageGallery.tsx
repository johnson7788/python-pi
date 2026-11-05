import { useEffect, useRef, useState } from 'react'

export interface GalleryImage {
  title: string
  imageUrl: string
  sourceUrl?: string
  source?: string
  width?: number
  height?: number
}

export interface ImageGalleryProps {
  /** Runs the actual web image search — each app passes its own adapter (e.g. slidesApi.imageSearch). */
  search: (query: string, max?: number) => Promise<{ images: GalleryImage[] }>
  /** Called with the chosen image; the app decides how to insert it. */
  onPick: (image: GalleryImage) => void
  onClose?: () => void
  /** Optional seed query run on mount (web search). */
  initialQuery?: string
  placeholder?: string
  /** Enables the private "我的图库" tab (upload / extract-from-doc / delete). When
   * present it becomes the default tab. docId enables "从当前文档提取"; the
   * "上传文献提取" button (upload a doc → extract its images) is always available. */
  gallery?: { docId?: string | null }
}

const TOKEN_KEY = 'aioffice_token'

function authHeaders(): HeadersInit {
  const t = localStorage.getItem(TOKEN_KEY)
  return t ? { Authorization: `Bearer ${t}` } : {}
}

function blobToDataUrl(b: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader()
    r.onload = () => res(r.result as string)
    r.onerror = () => rej(r.error)
    r.readAsDataURL(b)
  })
}

interface MineItem {
  id: string
  name: string
  dataUrl: string // authed blob loaded as a data: URL (used for both display and insert)
}

// ponytail: layout via inline styles so it drops into any app with zero per-app CSS.
export function ImageGallery({ search, onPick, onClose, initialQuery = '', placeholder, gallery }: ImageGalleryProps) {
  const hasMine = !!gallery
  const [tab, setTab] = useState<'mine' | 'web'>(hasMine ? 'mine' : 'web')

  // ── web search state ──
  const [query, setQuery] = useState(initialQuery)
  const [images, setImages] = useState<GalleryImage[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const seq = useRef(0)

  async function run(q: string) {
    const term = q.trim()
    if (!term) return
    const my = ++seq.current
    setLoading(true)
    setError('')
    try {
      const res = await search(term, 24)
      if (my !== seq.current) return // stale
      setImages(res.images ?? [])
      if (!res.images?.length) setError('没有找到相关图片')
    } catch {
      if (my === seq.current) setError('图片搜索失败')
    } finally {
      if (my === seq.current) setLoading(false)
    }
  }

  // ── my gallery state ──
  const [mine, setMine] = useState<MineItem[]>([])
  const [mineLoading, setMineLoading] = useState(false)
  const [mineMsg, setMineMsg] = useState('')
  const [mineQuery, setMineQuery] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const docRef = useRef<HTMLInputElement>(null)

  async function loadMine(q = '') {
    setMineLoading(true)
    setMineMsg('')
    try {
      const r = await fetch(`/gallery${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ''}`, {
        headers: authHeaders(),
      })
      if (!r.ok) throw new Error()
      const list = (await r.json()) as { id: string; name: string }[]
      const items = await Promise.all(
        list.map(async (a) => {
          const b = await fetch(`/gallery/${a.id}/blob`, { headers: authHeaders() }).then((x) => x.blob())
          return { id: a.id, name: a.name, dataUrl: await blobToDataUrl(b) }
        }),
      )
      setMine(items)
      if (!items.length) setMineMsg('图库为空，上传或从文档提取图片')
    } catch {
      setMineMsg('加载图库失败')
    } finally {
      setMineLoading(false)
    }
  }

  async function doUpload(files: FileList | null) {
    if (!files?.length) return
    setMineLoading(true)
    setMineMsg('')
    try {
      for (const f of Array.from(files)) {
        if (!f.type.startsWith('image/')) continue
        await fetch(`/gallery?name=${encodeURIComponent(f.name)}`, {
          method: 'POST',
          headers: { ...authHeaders(), 'Content-Type': f.type },
          body: f,
        })
      }
      await loadMine(mineQuery)
    } catch {
      setMineMsg('上传失败')
      setMineLoading(false)
    }
  }

  async function doExtract() {
    if (!gallery?.docId) return
    setMineLoading(true)
    setMineMsg('')
    try {
      const r = await fetch(`/gallery/extract-from/${gallery.docId}`, { method: 'POST', headers: authHeaders() })
      if (!r.ok) throw new Error()
      const created = (await r.json()) as unknown[]
      setMineMsg(created.length ? `提取了 ${created.length} 张图片` : '文档中没有图片')
      await loadMine(mineQuery)
    } catch {
      setMineMsg('提取失败')
      setMineLoading(false)
    }
  }

  // Upload a document (docx/pptx/xlsx/pdf) → server extracts its images; the
  // document itself is not saved, only the extracted images land in the gallery.
  async function doExtractUpload(files: FileList | null) {
    const f = files?.[0]
    if (!f) return
    setMineLoading(true)
    setMineMsg('')
    try {
      const r = await fetch(`/gallery/extract?name=${encodeURIComponent(f.name)}`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/octet-stream' },
        body: f,
      })
      if (!r.ok) throw new Error()
      const created = (await r.json()) as unknown[]
      setMineMsg(created.length ? `提取了 ${created.length} 张图片` : '文献中没有图片')
      await loadMine(mineQuery)
    } catch {
      setMineMsg('提取失败')
      setMineLoading(false)
    }
  }

  async function doDelete(id: string) {
    await fetch(`/gallery/${id}`, { method: 'DELETE', headers: authHeaders() }).catch(() => {})
    setMine((xs) => xs.filter((x) => x.id !== id))
  }

  useEffect(() => {
    if (hasMine) void loadMine()
    else if (initialQuery.trim()) void run(initialQuery)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div style={S.panel}>
      {hasMine && (
        <div style={S.tabs}>
          <button style={tab === 'mine' ? S.tabActive : S.tab} onClick={() => setTab('mine')}>
            我的图库
          </button>
          <button style={tab === 'web' ? S.tabActive : S.tab} onClick={() => setTab('web')}>
            联网搜索
          </button>
          <div style={{ flex: 1 }} />
          {onClose && (
            <button style={S.close} onClick={onClose} aria-label="关闭">
              ×
            </button>
          )}
        </div>
      )}

      {tab === 'web' ? (
        <>
          <div style={S.bar}>
            <input
              style={S.input}
              value={query}
              placeholder={placeholder ?? '搜索图片…'}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void run(query)
              }}
              autoFocus
            />
            <button style={S.btn} onClick={() => void run(query)} disabled={loading}>
              {loading ? '搜索中…' : '搜索'}
            </button>
            {!hasMine && onClose && (
              <button style={S.close} onClick={onClose} aria-label="关闭">
                ×
              </button>
            )}
          </div>
          {error && <div style={S.hint}>{error}</div>}
          <div style={S.grid}>
            {images.map((img, i) => (
              <button key={`${img.imageUrl}-${i}`} style={S.cell} title={img.title || img.source || ''} onClick={() => onPick(img)}>
                <img src={img.imageUrl} alt={img.title || ''} style={S.thumb} loading="lazy" />
              </button>
            ))}
          </div>
        </>
      ) : (
        <>
          <div style={S.bar}>
            <input
              style={S.input}
              value={mineQuery}
              placeholder="筛选我的图片…"
              onChange={(e) => setMineQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void loadMine(mineQuery)
              }}
            />
            <button style={S.btn} onClick={() => fileRef.current?.click()} disabled={mineLoading}>
              上传图片
            </button>
            <button
              style={S.btn}
              onClick={() => docRef.current?.click()}
              disabled={mineLoading}
              title="上传文献（Word/PPT/Excel/PDF）提取其中的图片"
            >
              上传文献提取
            </button>
            {gallery?.docId && (
              <button
                style={S.btn}
                onClick={() => void doExtract()}
                disabled={mineLoading}
                title="从当前打开的文档提取图片"
              >
                从当前文档提取
              </button>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              style={{ display: 'none' }}
              onChange={(e) => void doUpload(e.target.files)}
            />
            <input
              ref={docRef}
              type="file"
              accept=".docx,.pptx,.xlsx,.pdf"
              style={{ display: 'none' }}
              onChange={(e) => void doExtractUpload(e.target.files)}
            />
          </div>
          {mineMsg && <div style={S.hint}>{mineLoading ? '处理中…' : mineMsg}</div>}
          {mineLoading && !mineMsg && <div style={S.hint}>加载中…</div>}
          <div style={S.grid}>
            {mine.map((a) => (
              <div key={a.id} style={S.cellWrap}>
                <button style={S.cell} title={a.name} onClick={() => onPick({ title: a.name, imageUrl: a.dataUrl })}>
                  <img src={a.dataUrl} alt={a.name} style={S.thumb} loading="lazy" />
                </button>
                <button style={S.del} onClick={() => void doDelete(a.id)} aria-label="删除" title="删除">
                  ×
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

const S: Record<string, React.CSSProperties> = {
  panel: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, gap: 8, padding: 12 },
  tabs: { display: 'flex', gap: 4, alignItems: 'center' },
  tab: {
    padding: '5px 12px', borderRadius: 8, border: '1px solid var(--border, #d0d5dd)',
    background: 'transparent', color: '#475467', fontSize: 14, cursor: 'pointer',
  },
  tabActive: {
    padding: '5px 12px', borderRadius: 8, border: '1px solid var(--accent, #2563eb)',
    background: 'var(--accent, #2563eb)', color: '#fff', fontSize: 14, cursor: 'pointer',
  },
  bar: { display: 'flex', gap: 8, alignItems: 'center' },
  input: {
    flex: 1, padding: '6px 10px', borderRadius: 8,
    border: '1px solid var(--border, #d0d5dd)', fontSize: 14, outline: 'none',
  },
  btn: {
    padding: '6px 14px', borderRadius: 8, border: 'none',
    background: 'var(--accent, #2563eb)', color: '#fff', fontSize: 14, cursor: 'pointer', whiteSpace: 'nowrap',
  },
  btnDisabled: {
    padding: '6px 14px', borderRadius: 8, border: 'none',
    background: '#d0d5dd', color: '#fff', fontSize: 14, cursor: 'not-allowed', whiteSpace: 'nowrap',
  },
  close: { border: 'none', background: 'transparent', fontSize: 22, lineHeight: 1, cursor: 'pointer', color: '#667085' },
  hint: { fontSize: 13, color: '#667085', padding: '4px 2px' },
  grid: {
    flex: 1, minHeight: 0, overflowY: 'auto', display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', gap: 8, alignContent: 'start',
  },
  cellWrap: { position: 'relative' },
  cell: {
    width: '100%', padding: 0, border: '1px solid var(--border, #eaecf0)', borderRadius: 8,
    overflow: 'hidden', cursor: 'pointer', background: '#f9fafb', aspectRatio: '4 / 3',
  },
  thumb: { width: '100%', height: '100%', objectFit: 'cover', display: 'block' },
  del: {
    position: 'absolute', top: 4, right: 4, width: 20, height: 20, borderRadius: '50%', border: 'none',
    background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 14, lineHeight: '20px', cursor: 'pointer', padding: 0,
  },
}
