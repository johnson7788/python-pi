/**
 * The workspace file browser.
 *
 * Every path here is workspace-relative and the server re-validates it anyway — this page
 * only decides what to *show*, never what is allowed. That division matters: a UI that
 * filtered paths client-side would look safe while enforcing nothing.
 *
 * Uploads go out as raw bytes to `PUT /api/workspace/file?path=…`, one request per file,
 * which is the same endpoint a text edit uses. Downloads are plain links, so the browser's
 * own download handling and the session cookie do the work.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, resolveDownloadUrl } from '../lib/api'
import { Card, Empty, ErrorNote, PageHeader, formatTime } from '../components/Page'

interface Entry {
  name: string
  path: string
  isDir: boolean
  size: number
  modified: number
  hidden: boolean
}

interface TreePayload {
  path: string
  entries: Entry[]
  truncated: boolean
}

interface FilePayload {
  path: string
  name: string
  size: number
  modified: number
  binary: boolean
  truncated: boolean
  text: string | null
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** Join a directory path and a child name into a workspace path. */
function joinPath(dir: string, name: string): string {
  const base = dir === '/' ? '' : dir.replace(/\/$/, '')
  return `${base}/${name}`
}

function parentPath(path: string): string {
  const parts = path.split('/').filter(Boolean)
  parts.pop()
  return parts.length ? `/${parts.join('/')}` : '/'
}

export function Files() {
  const [path, setPath] = useState('/')
  const [tree, setTree] = useState<TreePayload | null>(null)
  const [shown, setShown] = useState(false)
  const [file, setFile] = useState<FilePayload | null>(null)
  const [draft, setDraft] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const uploadRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async (target: string) => {
    setBusy(true)
    setError(null)
    try {
      const payload = await api.get<TreePayload>(
        `/api/workspace/tree?path=${encodeURIComponent(target)}`,
      )
      setTree(payload)
      setPath(payload.path)
      setShown(true)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取目录失败')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    void load('/')
  }, [load])

  const openFile = async (entry: Entry) => {
    setError(null)
    setNotice(null)
    try {
      const payload = await api.get<FilePayload>(
        `/api/workspace/file?path=${encodeURIComponent(entry.path)}`,
      )
      setFile(payload)
      setDraft(payload.text ?? '')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取文件失败')
    }
  }

  const saveFile = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      await api.put(`/api/workspace/file?path=${encodeURIComponent(file.path)}`, { content: draft })
      setNotice(`已保存 ${file.name}`)
      await load(path)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setBusy(true)
    setError(null)
    let uploaded = 0
    try {
      for (const chosen of Array.from(files)) {
        const target = joinPath(path, chosen.name)
        await api.upload(`/api/workspace/file?path=${encodeURIComponent(target)}`, chosen)
        uploaded += 1
      }
      setNotice(`已上传 ${uploaded} 个文件`)
      await load(path)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '上传失败')
    } finally {
      setBusy(false)
      if (uploadRef.current) uploadRef.current.value = ''
    }
  }

  const remove = async (entry: Entry) => {
    if (!window.confirm(`删除 ${entry.path}？此操作不可撤销。`)) return
    setBusy(true)
    setError(null)
    try {
      await api.del(`/api/workspace/file?path=${encodeURIComponent(entry.path)}`)
      if (file?.path === entry.path) setFile(null)
      await load(path)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '删除失败')
    } finally {
      setBusy(false)
    }
  }

  const makeDirectory = async () => {
    const name = window.prompt('新目录名称')
    if (!name) return
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/workspace/directory', { parentPath: path, name })
      await load(path)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '创建目录失败')
    } finally {
      setBusy(false)
    }
  }

  const visible = (tree?.entries ?? []).filter((entry) => !entry.hidden)

  return (
    <div className="page">
      <PageHeader
        title="文件"
        description="这是 agent 的工作区——你在这里放进去的文件，它就能读写。"
        actions={
          <>
            <button type="button" className="button" onClick={() => void makeDirectory()} disabled={busy}>
              新建目录
            </button>
            <button
              type="button"
              className="button button--primary"
              onClick={() => uploadRef.current?.click()}
              disabled={busy}
            >
              上传文件
            </button>
            <input
              ref={uploadRef}
              type="file"
              multiple
              hidden
              onChange={(event) => void upload(event.target.files)}
            />
          </>
        }
      />

      <ErrorNote message={error} />
      {notice ? <div className="note note--ok">{notice}</div> : null}

      <div className="files">
        <Card
          title="目录"
          actions={
            <button type="button" className="link-button" onClick={() => void load('/')} disabled={busy}>
              工作区根目录
            </button>
          }
        >
          <div className="crumbs">
            <button type="button" className="link-button" onClick={() => void load('/')}>
              /
            </button>
            {path
              .split('/')
              .filter(Boolean)
              .map((part, index, all) => {
                const target = `/${all.slice(0, index + 1).join('/')}`
                return (
                  <span key={target}>
                    <button type="button" className="link-button" onClick={() => void load(target)}>
                      {part}
                    </button>
                    {index < all.length - 1 ? <span className="crumbs__sep">/</span> : null}
                  </span>
                )
              })}
          </div>

          {path !== '/' ? (
            <button type="button" className="filelist__up" onClick={() => void load(parentPath(path))}>
              ↑ 上一级
            </button>
          ) : null}

          {visible.length === 0 && shown ? (
            <Empty>这里还是空的。用右上角的「上传文件」放点东西进来。</Empty>
          ) : null}

          <ul className="filelist">
            {visible.map((entry) => (
              <li key={entry.path} className={`filelist__row${file?.path === entry.path ? ' is-active' : ''}`}>
                <button
                  type="button"
                  className="filelist__name"
                  onClick={() => (entry.isDir ? void load(entry.path) : void openFile(entry))}
                >
                  <span className="filelist__icon" aria-hidden="true">
                    {entry.isDir ? '▸' : '·'}
                  </span>
                  {entry.name}
                </button>
                <span className="filelist__size">{entry.isDir ? '' : formatSize(entry.size)}</span>
                {!entry.isDir ? (
                  <a className="link-button" href={resolveDownloadUrl(entry.path)} download>
                    下载
                  </a>
                ) : null}
                <button
                  type="button"
                  className="filelist__delete"
                  onClick={() => void remove(entry)}
                  aria-label={`删除 ${entry.name}`}
                  disabled={busy}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          {tree?.truncated ? <div className="note">目录内容过多，只显示了前一部分。</div> : null}
        </Card>

        <Card title={file ? file.path : '预览'}>
          {!file ? (
            <Empty>选择左侧的文件查看内容。</Empty>
          ) : file.binary ? (
            <Empty>
              这是二进制文件（{formatSize(file.size)}），不能在这里预览。
              <a className="link-button" href={resolveDownloadUrl(file.path)}>
                下载
              </a>
            </Empty>
          ) : file.truncated ? (
            <Empty>
              文件太大（{formatSize(file.size)}），不在这里预览。
              <a className="link-button" href={resolveDownloadUrl(file.path)}>
                下载
              </a>
            </Empty>
          ) : (
            <>
              <textarea
                className="editor"
                value={draft}
                spellCheck={false}
                onChange={(event) => setDraft(event.target.value)}
              />
              <div className="editor__bar">
                <span className="editor__meta">
                  {formatSize(file.size)} · 修改于 {formatTime(new Date(file.modified * 1000).toISOString())}
                </span>
                <button
                  type="button"
                  className="button button--primary"
                  onClick={() => void saveFile()}
                  disabled={busy || draft === (file.text ?? '')}
                >
                  保存
                </button>
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  )
}
