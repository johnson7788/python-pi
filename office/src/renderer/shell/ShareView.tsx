import { useEffect, useState } from 'react'
import { downloadShared, fetchSharedMeta } from '../web-adapter'

/**
 * Public read-only share landing (?share=<token>). No auth: fetches the share
 * metadata and offers a download. ponytail: read-only = download, not an
 * in-browser render viewer (that needs each doc type's engine) — add per-type
 * preview when it's worth the bundle.
 */
export function ShareView({ token }: { token: string }) {
  const [meta, setMeta] = useState<{ title: string; type: string } | null | 'loading'>('loading')
  useEffect(() => {
    void fetchSharedMeta(token).then((m) => setMeta(m))
  }, [token])

  return (
    <div className="share-view">
      <div className="share-card">
        <div className="home-logo">AI Office</div>
        {meta === 'loading' ? (
          <div className="share-status">加载中…</div>
        ) : meta === null ? (
          <div className="share-status">链接无效或已被撤销</div>
        ) : (
          <>
            <div className="share-title">{meta.title}</div>
            <div className="share-sub">共享文档（只读）</div>
            <button className="home-send" onClick={() => void downloadShared(token, meta.title)}>
              下载文档
            </button>
          </>
        )}
      </div>
    </div>
  )
}
