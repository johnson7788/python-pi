/**
 * Web adapter: reimplements the Electron preload globals `window.desktopApi`
 * (DesktopApi) and `window.projectApi` (ProjectApi) for the browser SaaS.
 * Imported first in main.tsx so the globals exist before <App> mounts.
 *
 * Mechanical port of genoffice's apps/sheets/src/main/sheets-main.ts. The
 * electron main process is gone; the xlsx engine (calamine/IronCalc) lives in
 * a Rust sidecar the BACKEND owns. This file addresses a workbook by the
 * sessionId the backend `/sheets/open` returns and never sees a filesystem
 * path. The write-planning gateway (xlsx-* modules) runs HERE in the browser;
 * it ships changed entry bytes to `/sheets/save` and the sidecar reassembles
 * the zip server-side.
 *
 * ponytail: stubbed for the first web port (each marked inline): screen
 * capture (Insert→Screenshot), pdf export, crash-recovery copy, local-image
 * insert dialog, chat attachments, AI Office cloud/Genspark. Add when the
 * feature actually ships.
 */
import { Buffer } from 'buffer'
// The xlsx gateway (csv-import, xlsx-drawing-add, xlsx-gateway) calls
// Buffer.from(...) in a few spots (node heritage). Provide the global before
// any gateway module runs. web-adapter is imported first in main.tsx.
;(globalThis as { Buffer?: typeof Buffer }).Buffer ??= Buffer

import JSZip from 'jszip'
import type { ProjectApi } from '@genoffice/project-store'
import type {
  AiSettings,
  AiStreamChunk,
  AiStreamRequest,
} from '@genoffice/ai-provider'
import type {
  DesktopApi,
  MenuAction,
  WebSearchResult,
  WorkbookFile,
  WorkbookSaveRequest,
  WorkbookSaveResult,
} from '../shared/desktop-api'
import {
  planCellEditsToXlsx,
  type CellEdit,
  type EntrySource,
} from '../gateway/xlsx-gateway'
import { parsePivotDefinition } from '../gateway/xlsx-pivot'

const API = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
const EMBED = new URLSearchParams(location.search).get('embed') === '1'
const SHEETS = EMBED ? '/api/office/sheets' : '/sheets'
const OFFICE_PATH = '/api/office'
const noop = () => {}
const unsub = () => noop

// ── byte <-> base64 (browser) ───────────────────────────────────────────
function bytesToBase64(bytes: Uint8Array): string {
  let bin = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk)
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk))
  return btoa(bin)
}
function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}
const utf8ToBase64 = (s: string) => bytesToBase64(new TextEncoder().encode(s))

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

function pickFile(accept: string): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    input.onchange = () => resolve(input.files?.[0] ?? null)
    input.click()
  })
}

// ── auth + token (shared shape with docs/slides adapters) ─────────────────
const TOKEN_KEY = 'aioffice_token'
// Dev cross-origin handoff: the docs Home appends ?tok= when navigating here
// (separate vite origins don't share localStorage). Adopt it, then strip the
// tok param. No-op in prod (same origin → token already present).
;(() => {
  const p = new URLSearchParams(location.search)
  const tok = p.get('tok')
  if (!tok) return
  localStorage.setItem(TOKEN_KEY, tok)
  p.delete('tok')
  const q = p.toString()
  history.replaceState(null, '', location.pathname + (q ? `?${q}` : ''))
})()
function hasToken(): boolean {
  return !!localStorage.getItem(TOKEN_KEY)
}
function getToken(): string {
  const t = localStorage.getItem(TOKEN_KEY)
  if (!t) throw new Error('not authenticated')
  return t
}
async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  let r: Response
  if (EMBED) {
    r = await fetch(`${API}${path}`, { ...init, headers, credentials: 'same-origin' })
  } else {
    headers.set('Authorization', `Bearer ${getToken()}`)
    r = await fetch(`${API}${path}`, { ...init, headers })
  }
  if (r.status === 401 && !EMBED) {
    localStorage.removeItem(TOKEN_KEY)
    location.reload()
  }
  return r
}
async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const r = await authFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error((await r.text().catch(() => '')) || `HTTP ${r.status}`)
  return (await r.json()) as T
}
async function apiJson<T>(path: string, init: RequestInit): Promise<T> {
  const r = await authFetch(path, init)
  return (await r.json()) as T
}
async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as T
}

/** data:<mime>;base64,<b64> → {base64,mime}, else null (gallery/AI data URLs skip the proxy). */
export function dataUrlParts(url: string): { base64: string; mime: string } | null {
  const m = /^data:([^;,]+);base64,(.*)$/s.exec(url)
  return m ? { mime: m[1], base64: m[2] } : null
}
function filenameFromDisposition(cd: string | null, fallback: string): string {
  const star = cd?.match(/filename\*=(?:UTF-8'')?([^;]+)/i)
  if (star) { try { return decodeURIComponent(star[1].replace(/"/g, '')) } catch { /* fall through */ } }
  const m = cd?.match(/filename="?([^";]+)"?/i)
  return m ? m[1] : fallback
}

// ── AI streaming over SSE (ported verbatim from docs/slides adapter) ──────
type StreamListener = (chunk: AiStreamChunk) => void
const streamListeners = new Set<StreamListener>()
const controllers = new Map<string, AbortController>()
const emit = (chunk: AiStreamChunk) => streamListeners.forEach((l) => l(chunk))

async function aiStream(request: AiStreamRequest): Promise<void> {
  const { requestId } = request
  const ac = new AbortController()
  controllers.set(requestId, ac)
  let resp: Response
  try {
    resp = await fetch(`${API}/ai/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal: ac.signal,
    })
  } catch (e) {
    controllers.delete(requestId)
    emit({ requestId, type: 'error', error: e instanceof Error ? e.message : String(e) } as AiStreamChunk)
    return
  }
  if (!resp.ok || !resp.body) {
    controllers.delete(requestId)
    emit({ requestId, type: 'error', error: `HTTP ${resp.status}` } as AiStreamChunk)
    return
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  void (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          const dataLine = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!dataLine) continue
          emit(JSON.parse(dataLine.slice(5).trim()) as AiStreamChunk)
        }
      }
    } catch (e) {
      if (!ac.signal.aborted)
        emit({ requestId, type: 'error', error: e instanceof Error ? e.message : String(e) } as AiStreamChunk)
    } finally {
      controllers.delete(requestId)
    }
  })()
}

// ── workbook session (module-level; single-tab web build) ─────────────────
// Mirrors sheets-main's SessionInfo: the sheetId→file-sheet-name map is the
// only per-session state the save planner needs (path lives server-side).
interface Session {
  sessionId: string
  docId: string | null // the AIOffice document this workbook is bound to (null = unsaved/new)
  title: string
  sheetNames: Map<string, string>
}
let session: Session | null = null

/** open workbook's server doc id, or null when unsaved — scopes the image
 * gallery's "从文档提取" to the current workbook. */
export function getCurrentDocId(): string | null {
  return session?.docId ?? null
}

function ownSheetNames(open: { sheets: Array<{ id: string; name: string }> }): Map<string, string> {
  return new Map(open.sheets.map((s) => [s.id, s.name]))
}

/** Build the DesktopApi WorkbookFile from the sidecar open result. The sidecar
 * omits sha256/readOnly (main added them); name is the doc title, not the
 * server temp path. */
async function toWorkbookFile(
  open: Record<string, unknown>,
  bytes: Uint8Array,
  title: string,
): Promise<WorkbookFile> {
  return {
    ...(open as object),
    name: title,
    sha256: await sha256Hex(bytes),
    readOnly: false,
  } as WorkbookFile
}

// A blank workbook the sidecar can open (one empty sheet + a minimal
// stylesheet the gateway can patch). Built once via JSZip.
/** Online image search for the shared ImageGallery (unauthenticated backend proxy). */
export async function imageSearch(
  query: string,
  maxResults = 24,
): Promise<{ images: Array<{ title: string; imageUrl: string; sourceUrl?: string; source?: string; width?: number; height?: number }> }> {
  return getJson<{ images: [] }>(
    `/ai/image-search?query=${encodeURIComponent(query)}&max=${maxResults}`,
  ).catch(() => ({ images: [] }))
}

export async function blankXlsxBytes(): Promise<Uint8Array> {
  const zip = new JSZip()
  zip.file(
    '[Content_Types].xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      '</Types>',
  )
  zip.file(
    '_rels/.rels',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      '</Relationships>',
  )
  zip.file(
    'xl/_rels/workbook.xml.rels',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
      '</Relationships>',
  )
  zip.file(
    'xl/workbook.xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
  )
  zip.file(
    'xl/worksheets/sheet1.xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<sheetData/></worksheet>',
  )
  zip.file(
    'xl/styles.xml',
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>' +
      '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>' +
      '<borders count="1"><border/></borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>' +
      '</styleSheet>',
  )
  return zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE' })
}

// ── open flow ──────────────────────────────────────────────────────────
async function openBytes(bytes: Uint8Array, docId: string | null, title: string): Promise<WorkbookFile> {
  const r = await authFetch(`${SHEETS}/open`, { method: 'POST', body: bytes as BodyInit })
  if (!r.ok) throw new Error((await r.text().catch(() => '')) || `HTTP ${r.status}`)
  const open = (await r.json()) as Record<string, unknown>
  session = {
    sessionId: open.sessionId as string,
    docId,
    title,
    sheetNames: ownSheetNames(open as { sheets: Array<{ id: string; name: string }> }),
  }
  return toWorkbookFile(open, bytes, title)
}

async function openFromDocId(id: string): Promise<WorkbookFile | null> {
  const blob = await authFetch(EMBED ? `${OFFICE_PATH}/documents/${id}/blob` : `/documents/${id}/blob`)
  if (!blob.ok) return null
  const bytes = new Uint8Array(await blob.arrayBuffer())
  const title = EMBED ? '工作簿.xlsx' : filenameFromDisposition(blob.headers.get('content-disposition'), '工作簿.xlsx')
  return openBytes(bytes, id, title)
}

// Cross-app handoff: docs Home routes an .xlsx doc here via ?open=<docId> (dev)
// or sessionStorage aioffice.pendingOpen. Consumed once by selectWorkbook.
const pendingDocId: string | null = (() => {
  const p = new URLSearchParams(location.search)
  const fromUrl = p.get('open')
  if (fromUrl) {
    p.delete('open')
    const q = p.toString()
    history.replaceState(null, '', location.pathname + (q ? `?${q}` : ''))
    return fromUrl
  }
  const stored = sessionStorage.getItem('aioffice.pendingOpen')
  if (stored) sessionStorage.removeItem('aioffice.pendingOpen')
  return stored
})()
let pendingConsumed = false

// ── save: port of sheets-main.ts writeWorkbookTo + saveWorkbookViaSidecar ──
// The sheetId→file-name resolution is identical to writeWorkbookTo; the plan
// is computed here (planCellEditsToXlsx) and its patched entry bytes POST to
// /sheets/save, where the sidecar reassembles the zip.
async function backendManifest(sessionId: string): Promise<
  { name: string; crc32: number; compressedSize: number; uncompressedSize: number }[]
> {
  const r = await apiPost<{ entries: { name: string; crc32: number; compressedSize: number; uncompressedSize: number }[] }>(
    `${SHEETS}/manifest`,
    { sessionId },
  )
  return r.entries
}
async function readEntryText(sessionId: string, name: string): Promise<string> {
  const r = await apiPost<{ entries: { name: string; contentB64: string }[] }>(`${SHEETS}/read-entries`, {
    sessionId,
    entries: [name],
  })
  const e = r.entries[0]
  if (!e) throw new Error(`Workbook is missing ${name}.`)
  return new TextDecoder().decode(base64ToBytes(e.contentB64))
}

const MAX_PATCH_ENTRY_BYTES = 256 * 1024 * 1024

function httpEntrySource(
  sessionId: string,
  manifest: readonly { name: string; uncompressedSize: number }[],
): EntrySource {
  const byName = new Map(manifest.map((e) => [e.name, e]))
  const cache = new Map<string, string>()
  return {
    paths: async () => manifest.map((e) => e.name),
    has: async (path) => byName.has(path),
    canPatch: async (path) => (byName.get(path)?.uncompressedSize ?? 0) <= MAX_PATCH_ENTRY_BYTES,
    containsText: async (path, needle) => {
      const r = await apiPost<{ matches: string[] }>(`${SHEETS}/scan-entries`, {
        sessionId,
        entries: [path],
        needle,
      })
      return r.matches.includes(path)
    },
    readText: async (path) => {
      const cached = cache.get(path)
      if (cached !== undefined) return cached
      const content = await readEntryText(sessionId, path)
      cache.set(path, content)
      return content
    },
  }
}

async function saveWorkbookEdits(request: WorkbookSaveRequest): Promise<WorkbookSaveResult> {
  const s = session
  if (!s || s.sessionId !== request.sessionId) throw new Error('Unknown workbook session.')

  // ── sheetId → file sheet name (verbatim from writeWorkbookTo) ──
  const addedSheetNames = new Map<string, string>()
  const duplicateSources = new Map<string, string>()
  const renames: { sheetName: string; newName: string }[] = []
  const removals: string[] = []
  const hiddenChanges: { sheetName: string; hidden: boolean }[] = []
  let orderChanged = false
  for (const op of request.sheetOps) {
    if (op.kind === 'add-sheet') {
      addedSheetNames.set(op.sheetId, op.name)
      continue
    }
    if (op.kind === 'duplicate-sheet') {
      const sourceName = s.sheetNames.get(op.sourceSheetId)
      if (!sourceName) throw new Error(`Unknown duplicate source ${op.sourceSheetId}.`)
      addedSheetNames.set(op.sheetId, op.name)
      duplicateSources.set(op.sheetId, sourceName)
      continue
    }
    if (op.kind === 'reorder-sheets') {
      orderChanged = true
      continue
    }
    const sheetName = addedSheetNames.get(op.sheetId) ?? s.sheetNames.get(op.sheetId)
    if (!sheetName) throw new Error(`Unknown worksheet ${op.sheetId}.`)
    if (op.kind === 'rename-sheet') renames.push({ sheetName, newName: op.newName })
    else if (op.kind === 'set-sheet-hidden') hiddenChanges.push({ sheetName, hidden: op.hidden })
    else removals.push(sheetName)
  }
  const renameByOriginal = new Map(renames.map((r) => [r.sheetName, r.newName]))
  const resolveSheetName = (sheetId: string): string => {
    const name = addedSheetNames.get(sheetId) ?? s.sheetNames.get(sheetId)
    if (!name) throw new Error(`Unknown worksheet ${sheetId}.`)
    return name
  }
  const sheetPlan =
    request.sheetOps.length > 0
      ? {
          renames,
          additions: [...addedSheetNames].map(([sheetId, name]) => ({
            name,
            sourceSheetName: duplicateSources.get(sheetId),
          })),
          removals,
          hiddenChanges,
          orderChanged,
          order: request.sheetOrder.map((sheetId) => {
            const original = resolveSheetName(sheetId)
            return addedSheetNames.has(sheetId)
              ? original
              : (renameByOriginal.get(original) ?? original)
          }),
        }
      : undefined

  const edits: CellEdit[] = request.edits.map((edit) => ({
    sheetName: resolveSheetName(edit.sheetId),
    row: edit.row,
    column: edit.column,
    writeValue: edit.writeValue,
    cell: { value: edit.value, formula: edit.formula },
    style: edit.style,
    rich: edit.rich,
    styleReset: edit.styleReset,
  }))
  const opsBySheet = new Map<string, unknown[]>()
  for (const op of request.structuralOps) {
    const sheetName = resolveSheetName(op.sheetId)
    const list = opsBySheet.get(sheetName) ?? []
    if ('range' in op) list.push({ kind: op.kind, range: op.range })
    else if ('size' in op) list.push({ kind: op.kind, start: op.start, end: op.end, size: op.size })
    else if ('level' in op)
      list.push({
        kind: op.kind,
        start: op.start,
        end: op.end,
        level: op.level,
        ...(op.collapsed === undefined ? {} : { collapsed: op.collapsed }),
      })
    else if ('hidden' in op)
      list.push({ kind: op.kind, start: op.start, end: op.end, hidden: op.hidden })
    else if ('before' in op)
      list.push({ kind: op.kind, index: op.index, count: op.count, before: op.before })
    else list.push({ kind: op.kind, index: op.index, count: op.count })
    opsBySheet.set(sheetName, list)
  }
  const structuralOps = [...opsBySheet].map(([sheetName, ops]) => ({ sheetName, ops }))
  const filterStates = request.filterStates.map((state) => ({
    sheetName: resolveSheetName(state.sheetId),
    filter: state.filter,
    hiddenRows: state.hiddenRows,
    visibilityRange: state.visibilityRange,
  }))
  const linksBySheet = new Map<string, { row: number; column: number; target: string | null }[]>()
  for (const link of request.hyperlinkEdits) {
    const sheetName = resolveSheetName(link.sheetId)
    const list = linksBySheet.get(sheetName) ?? []
    list.push({ row: link.row, column: link.column, target: link.target })
    linksBySheet.set(sheetName, list)
  }
  const hyperlinkEdits = [...linksBySheet].map(([sheetName, edits]) => ({ sheetName, edits }))
  const cfStates = request.cfStates.map((state) => ({
    sheetName: resolveSheetName(state.sheetId),
    rules: state.rules,
  }))
  const dvStates = request.dvStates.map((state) => ({
    sheetName: resolveSheetName(state.sheetId),
    rules: state.rules,
  }))
  const sheetProtections = request.sheetProtections.map((state) => ({
    sheetName: resolveSheetName(state.sheetId),
    protected: state.protected,
  }))
  const pageSetupStates = request.pageSetupStates.map(({ sheetId, ...rest }) => ({
    sheetName: resolveSheetName(sheetId),
    ...rest,
  }))
  const noteStates = request.noteStates.map(({ sheetId, notes }) => ({
    sheetName: resolveSheetName(sheetId),
    notes,
  }))
  const visualAdditions = request.visualAdditions.map((a) => ({
    sheetName: resolveSheetName(a.sheetId),
    anchor: a.anchor,
    chart: a.chart,
    shape: a.shape,
    image: a.image,
  }))
  const tableAdditions = request.tableAdditions.map((tbl) => ({
    sheetName: resolveSheetName(tbl.sheetId),
    area: tbl.area,
    name: tbl.name,
    columnNames: tbl.columnNames,
    style: tbl.style,
    bandedRows: tbl.bandedRows,
  }))
  const pivotAdditions = request.pivotAdditions.map((p) => ({
    sheetName: resolveSheetName(p.sheetId),
    sourceSheetName: resolveSheetName(p.sourceSheetId),
    sourceArea: p.sourceArea,
    location: p.location,
    name: p.name,
    fieldNames: p.fieldNames,
    rowFieldIndices: p.rowFieldIndices,
    columnFieldIndex: p.columnFieldIndex,
    pageFieldIndices: p.pageFieldIndices,
    rowItems: p.rowItems,
    rowLevelItems: p.rowLevelItems,
    rowLines: p.rowLines,
    columnItems: p.columnItems,
    columnFieldIndices: p.columnFieldIndices,
    colLevelItems: p.colLevelItems,
    colLines: p.colLines,
    groupings: p.groupings,
    filters: p.filters,
    rowHiddenItems: p.rowHiddenItems,
    colHiddenItems: p.colHiddenItems,
    values: p.values,
  }))
  const sparklineAdditions = request.sparklineAdditions.map(({ sheetId, ...rest }) => ({
    sheetName: resolveSheetName(sheetId),
    ...rest,
  }))
  const formulaValuesBySheet = new Map<
    string,
    { row: number; column: number; value: string | number | boolean | null }[]
  >()
  for (const cell of request.formulaValues) {
    const sheetName = resolveSheetName(cell.sheetId)
    const list = formulaValuesBySheet.get(sheetName) ?? []
    list.push({ row: cell.row, column: cell.column, value: cell.value })
    formulaValuesBySheet.set(sheetName, list)
  }
  const formulaValues = [...formulaValuesBySheet].map(([sheetName, cells]) => ({ sheetName, cells }))
  const pivotRefreshUpdates = request.pivotRefreshUpdates.map((update) => ({
    cachePath: update.cachePath,
    sheetName: resolveSheetName(update.sheetId),
    newOutputRef: update.newOutputRef,
    ...(update.relayout === undefined
      ? {}
      : {
          relayout: (({ sheetId: _s, sourceSheetId, ...rest }) => ({
            ...rest,
            sourceSheetName: resolveSheetName(sourceSheetId),
          }))(update.relayout),
        }),
  }))

  // ── plan the patched entries in-browser, ship them to the sidecar ──
  const manifest = await backendManifest(s.sessionId)
  const source = httpEntrySource(s.sessionId, manifest)
  const plan = await planCellEditsToXlsx(
    source,
    edits,
    structuralOps as never,
    request.chartEdits,
    sheetPlan as never,
    filterStates as never,
    hyperlinkEdits as never,
    cfStates as never,
    dvStates as never,
    sheetProtections as never,
    request.definedNamesState ?? null,
    visualAdditions as never,
    pageSetupStates as never,
    noteStates as never,
    tableAdditions as never,
    pivotAdditions as never,
    request.pivotCacheRefreshPaths,
    pivotRefreshUpdates as never,
    request.visualEdits,
    sparklineAdditions as never,
    formulaValues as never,
  )
  // ponytail: the client-side manifest-preservation assertion (assertManifest-
  // Preserved) is dropped — the sidecar's save_archive raw-copies untouched
  // entries deterministically. Re-add if a corruption bug ever surfaces.
  const replacements = [...plan.replaced].map(([name, content]) => ({
    name,
    contentB64: utf8ToBase64(content),
  }))
  const additions = [
    ...[...plan.added].map(([name, content]) => ({ name, contentB64: utf8ToBase64(content) })),
    ...[...plan.addedBinary].map(([name, bytes]) => ({ name, contentB64: bytesToBase64(bytes) })),
  ]

  const result = await apiPost<{ docId: string; size: number; workbook: Record<string, unknown> }>(
    `${SHEETS}/save`,
    {
      sessionId: s.sessionId,
      docId: s.docId,
      title: s.title,
      replacements,
      removals: plan.removedEntries,
      additions,
    },
  )
  // The backend reopened a fresh session over the saved file; adopt it.
  const savedBytes = base64ToBytes(
    (
      await apiPost<{ entries: { name: string; contentB64: string }[] }>(`${SHEETS}/read-entries`, {
        sessionId: result.workbook.sessionId as string,
        entries: [],
      }).catch(() => ({ entries: [] }))
    ).entries[0]?.contentB64 ?? '',
  )
  session = {
    sessionId: result.workbook.sessionId as string,
    docId: result.docId,
    title: s.title,
    sheetNames: ownSheetNames(result.workbook as { sheets: Array<{ id: string; name: string }> }),
  }
  const file = await toWorkbookFile(
    result.workbook,
    savedBytes.length ? savedBytes : new Uint8Array(result.size),
    s.title,
  )
  if (EMBED) window.parent?.postMessage({ type: 'office:saved', docId: result.docId }, location.origin)
  return { canceled: false, file, touchedEntries: plan.touchedEntries as string[] }
}

/** upload a card thumbnail (base64 PNG, no data: prefix) for the Home doc grid */
async function putThumb(docId: string, pngBase64: string): Promise<void> {
  await authFetch(`/documents/${docId}/thumb`, {
    method: 'PUT',
    body: base64ToBytes(pngBase64) as BodyInit,
  }).catch(() => {})
}

/** Grab univer's main grid canvas (the largest one under #univer-container),
 *  downscale to a card thumbnail, and upload it for the currently-saved doc.
 *  Fire-and-forget: never blocks or fails the save. */
export async function uploadWorkbookThumb(): Promise<void> {
  const docId = session?.docId
  if (!docId) return
  const canvases = Array.from(
    document.querySelectorAll<HTMLCanvasElement>('#univer-container canvas'),
  )
  const src = canvases.sort((a, b) => b.width * b.height - a.width * a.height)[0]
  if (!src || !src.width || !src.height) return
  const scale = Math.min(1, 512 / src.width)
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(src.width * scale))
  canvas.height = Math.max(1, Math.round(src.height * scale))
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.drawImage(src, 0, 0, canvas.width, canvas.height)
  const png = canvas.toDataURL('image/png').split(',')[1]
  if (png) await putThumb(docId, png)
}

// ── DesktopApi implementation ─────────────────────────────────────────────
const menuHandlers = new Set<(action: MenuAction) => void>()
const desktopApi: DesktopApi = {
  getLanguage: async () => 'zh',
  onLanguageChanged: unsub,

  selectWorkbook: async (pick) => {
    if (!hasToken() && !EMBED) return null
    if (pendingDocId && !pendingConsumed) {
      pendingConsumed = true
      return openFromDocId(pendingDocId)
    }
    if (pick) {
      // Manual Open (File → Open): browser file picker → new session. Cancel =
      // no-op (don't clobber the current workbook with a blank).
      const file = await pickFile('.xlsx,.xls,.csv')
      if (!file) return null
      const bytes = new Uint8Array(await file.arrayBuffer())
      return openBytes(bytes, null, file.name)
    }
    // Boot with nothing pending: synthesize a fresh blank workbook so the sheet
    // is editable and saveable (desktop queued one; on web we build it).
    return openBytes(await blankXlsxBytes(), null, '工作簿.xlsx')
  },

  readWorkbookRange: (request) => apiPost(`${SHEETS}/read-range`, request),
  readWorkbookFormulas: (request) => apiPost(`${SHEETS}/read-formulas`, request),
  recalcWorkbook: (request) => apiPost(`${SHEETS}/recalc`, request),
  readWorkbookMedia: (request) => apiPost(`${SHEETS}/read-media`, request),
  readPivotDefinition: async (request) => {
    const [pivotXml, cacheXml] = await Promise.all([
      readEntryText(request.sessionId, request.path),
      readEntryText(request.sessionId, request.cachePath),
    ])
    return parsePivotDefinition(pivotXml, cacheXml) as unknown as Awaited<
      ReturnType<DesktopApi['readPivotDefinition']>
    >
  },

  saveWorkbookEdits,
  // Crash-recovery copy: no local userData target on web; the doc autosaves via
  // save. ponytail: no-op, re-add if we add a server-side recovery slot.
  writeWorkbookRecovery: async () => ({ ok: false }),
  // Content-derived rename after an AI run: nothing to rename until first save
  // (which titles the doc). ponytail: no-op for the web port.
  autoRenameWorkbook: async () => ({ renamed: false }),

  closeWorkbook: async (sessionId) => {
    await apiPost(`${SHEETS}/close`, { sessionId }).catch(() => undefined)
    if (session?.sessionId === sessionId) session = null
  },
  openExternal: async (url) => {
    if (/^https?:\/\//i.test(url)) window.open(url, '_blank', 'noopener')
  },

  onMenuAction: (callback) => {
    menuHandlers.add(callback)
    return () => menuHandlers.delete(callback)
  },
  onWorkbookRenamed: unsub,
  notifyPendingEdits: noop,
  onCloseSaveRequest: unsub,
  reportCloseSaveResult: noop,
  consumeNewBlankWorkbook: async () => false,
  // Auto-open the handoff/blank workbook on boot (App gates handleInspect-
  // Workbook on this). True whenever we're authed so a new tab lands on a
  // real, saveable workbook rather than a save-less in-memory demo.
  hasQueuedWorkbook: async () => EMBED || hasToken(),

  // ── AI ──
  getAiSettings: async () => ({ provider: 'backend' }) as unknown as AiSettings,
  setAiSettings: async () => {},
  aiChat: async () => ({ text: '' }) as unknown as Awaited<ReturnType<DesktopApi['aiChat']>>,
  aiStream,
  aiStreamCancel: async (requestId) => {
    controllers.get(requestId)?.abort()
    controllers.delete(requestId)
    await fetch(`${API}/ai/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requestId }),
    }).catch(noop)
  },
  aiGskStatus: async () => ({ loggedIn: true }) as unknown as Awaited<ReturnType<DesktopApi['aiGskStatus']>>,
  aiGskLogin: async () => {},
  webSearch: async (query, maxResults): Promise<WebSearchResult> =>
    getJson<WebSearchResult>(
      `/ai/web-search?query=${encodeURIComponent(query)}&max=${maxResults ?? 6}`,
    ).catch(() => ({ results: [], method: 'error', error: 'search unavailable' })),
  onAiStream: (handler) => {
    streamListeners.add(handler)
    return () => streamListeners.delete(handler)
  },

  // ── stubs: features without a web surface yet (marked ponytail) ──
  readLocalImage: async () => {
    throw new Error('local image insert not available in web build')
  },
  captureScreenSources: async () => ({ status: 'denied', sources: [] }),
  captureScreenSource: async () => null,
  exportPdf: async () => ({ canceled: true }),

  // ── chat attachments (stubbed, like docs/slides web build) ──
  pickAttachments: async () => null,
  addAttachmentPaths: async () => ({ accepted: [], rejected: [] }),
  addPastedImage: async () => ({ accepted: [], rejected: [] }),
  readAttachment: async () => ({ ok: false, error: 'not supported in web build' }),
  readAttachmentImage: async () => ({ ok: false, error: 'not supported in web build' }),
  getPathForFile: (file) => file.name,
}

// ── project/chat persistence (backend: /projects) — copied from docs/slides ──
const projectApi: ProjectApi = {
  resolveChat: (args) =>
    apiPost('/projects/resolve-chat', { filePath: args.filePath, tempChatId: args.tempChatId }),
  appendChat: async (args) => {
    await apiPost('/projects/append-chat', args)
  },
  loadChat: (args) =>
    apiJson(
      `/projects/chat?chatId=${encodeURIComponent(args.chatId)}&limit=${args.limit ?? 200}`,
      {},
    ),
  rebindChat: (args) => apiPost('/projects/rebind-chat', args),
  listProjects: () => apiJson('/projects', {}),
  createProject: (args) => apiPost('/projects', args),
  renameProject: async (args) => {
    await authFetch(`/projects/${args.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: args.name }),
    })
  },
  deleteProject: async (args) => {
    await authFetch(`/projects/${args.id}`, { method: 'DELETE' })
  },
  moveFile: (args) => apiPost('/projects/move-file', args),
  getTimeline: (args) =>
    apiJson(`/projects/${encodeURIComponent(args.projectId)}/timeline?limit=${args.limit ?? 100}`, {}),
}

;(window as unknown as { desktopApi: DesktopApi }).desktopApi = desktopApi
;(window as unknown as { projectApi: ProjectApi }).projectApi = projectApi
