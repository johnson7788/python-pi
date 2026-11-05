/**
 * Browser port of genoffice's main-process session-state.ts. In Electron each
 * renderer window had its own Session keyed by webContents.id; in the web build
 * one browser tab holds exactly one deck, so there is a single module-level
 * CURRENT session. All undo/redo/snapshot logic is ported verbatim.
 *
 * ponytail: single deck per tab, no id-keyed Map / window refs. Multi-doc tabs,
 * if ever needed, would reintroduce a Map keyed by a tab id.
 */
import { materializeSlide, type OpenedPptx, type Slide } from '@genoffice/pptx-engine'
import { buildRenderSlide, type RenderSlide } from '@genoffice/pptx-render'

export interface Session {
  path: string
  opened: OpenedPptx
  fitWidthPx: number
  undoStack: HistorySnapshot[]
  redoStack: HistorySnapshot[]
  historyBatch?: {
    depth: number
    undoStart: number
    before: HistorySnapshot
  }
  aiSnapshots?: Map<number, HistorySnapshot>
  metaDirty?: boolean
  htmlPages?: unknown[] | null
  transformPreview?: boolean
  masterEdit?: { partPath: string; slide: Slide } | null
}

let CURRENT: Session | null = null

export function getSession(): Session | null {
  return CURRENT
}

export function requireSession(): Session {
  if (!CURRENT) throw new Error('no open presentation')
  return CURRENT
}

export function setSession(s: Session): void {
  CURRENT = s
}

// ── Undo/redo (snapshot-based) ─────────────────────────────────────────
export interface HistorySnapshot {
  slides: Slide[]
  entries: Map<string, Uint8Array>
  size: { cx: number; cy: number }
}
const MAX_HISTORY = 50

function trimHistory(stack: HistorySnapshot[]): void {
  while (stack.length > MAX_HISTORY) stack.shift()
}

export function takeSnapshot(session: Session): HistorySnapshot {
  return {
    slides: structuredClone(session.opened.deck.slides),
    entries: new Map(session.opened.archive.entries),
    size: { ...session.opened.deck.size },
  }
}

function cloneSnapshot(snap: HistorySnapshot): HistorySnapshot {
  return {
    slides: structuredClone(snap.slides),
    entries: new Map(snap.entries),
    size: { ...snap.size },
  }
}

export function pushHistory(session: Session): void {
  session.undoStack.push(takeSnapshot(session))
  trimHistory(session.undoStack)
  session.redoStack = []
  session.htmlPages = null
}

export function beginHistoryBatch(session: Session): void {
  if (session.historyBatch) {
    session.historyBatch.depth += 1
    return
  }
  session.historyBatch = {
    depth: 1,
    undoStart: session.undoStack.length,
    before: takeSnapshot(session),
  }
}

export function endHistoryBatch(session: Session): HistorySnapshot | null {
  const batch = session.historyBatch
  if (!batch) return null
  batch.depth -= 1
  if (batch.depth > 0) return null
  session.historyBatch = undefined
  if (session.undoStack.length <= batch.undoStart) return null
  session.undoStack.splice(batch.undoStart)
  session.undoStack.push(batch.before)
  trimHistory(session.undoStack)
  return batch.before
}

const MAX_AI_SNAPSHOTS = 20
let nextAiSnapshotId = 1

export function registerAiSnapshot(session: Session, snap: HistorySnapshot): number {
  const map = (session.aiSnapshots ??= new Map())
  const id = nextAiSnapshotId++
  map.set(id, cloneSnapshot(snap))
  while (map.size > MAX_AI_SNAPSHOTS) map.delete(map.keys().next().value as number)
  return id
}

export function restoreAiSnapshot(session: Session, id: number): boolean {
  const snap = session.aiSnapshots?.get(id)
  if (!snap) return false
  pushHistory(session)
  restoreSnapshot(session, snap)
  session.aiSnapshots?.delete(id)
  return true
}

export function restoreSnapshot(session: Session, snap: HistorySnapshot): void {
  const fresh = cloneSnapshot(snap)
  session.opened.deck.slides = fresh.slides
  session.opened.deck.size = fresh.size
  const entries = session.opened.archive.entries
  entries.clear()
  for (const [k, v] of fresh.entries) entries.set(k, v)
}

export function settleStaleHistoryBatch(session: Session): void {
  while (session.historyBatch) {
    const collapsed = endHistoryBatch(session)
    if (collapsed) registerAiSnapshot(session, collapsed)
  }
}

// ── RenderSlide rebuild helpers ─────────────────────────────────────────
// ponytail: no system-font metrics (Electron used child_process to enumerate
// fonts). Browser build passes no `metrics`, so buildRenderSlide falls back to
// its HeuristicMetrics default. add OpentypeMetrics from bundled fonts if text
// width fidelity ever matters.

function bytesToBase64(bytes: Uint8Array): string {
  let bin = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk)
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk))
  return btoa(bin)
}

const DISPLAY_MIME: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  bmp: 'image/bmp',
  webp: 'image/webp',
  svg: 'image/svg+xml',
}

/** Image mediaRef -> dataUrl (lazily decoded). TIFF is skipped on web (Chromium
    can't decode it and the node transcoder is unavailable); the archive keeps
    the original bytes for save fidelity. */
export function makeMediaResolver(opened: OpenedPptx) {
  const cache = new Map<string, string | undefined>()
  return (mediaRef: string): string | undefined => {
    if (cache.has(mediaRef)) return cache.get(mediaRef)
    const bytes = opened.archive.readBytes(mediaRef)
    let url: string | undefined
    if (bytes) {
      const ext = mediaRef.split('.').pop()?.toLowerCase() ?? 'png'
      // ponytail: TIFF display skipped (no node tiff decoder in browser)
      if (ext !== 'tif' && ext !== 'tiff') {
        const mime = DISPLAY_MIME[ext] ?? 'image/png'
        url = `data:${mime};base64,${bytesToBase64(bytes)}`
      }
    }
    cache.set(mediaRef, url)
    return url
  }
}

export function buildAllRenderSlides(opened: OpenedPptx, fitWidthPx: number): RenderSlide[] {
  const media = makeMediaResolver(opened)
  return opened.deck.slides.map((s, i) =>
    buildRenderSlide(s, opened.deck.size, { fitWidthPx, media, slideNo: i + 1 }),
  )
}

export function rebuildSlide(session: Session, slideIndex: number): RenderSlide | null {
  const slide = session.opened.deck.slides[slideIndex]
  if (!slide) return null
  return buildRenderSlide(slide, session.opened.deck.size, {
    fitWidthPx: session.fitWidthPx,
    media: makeMediaResolver(session.opened),
    slideNo: slideIndex + 1,
  })
}

export function rebuildSlideWithReparse(session: Session, slideIndex: number): RenderSlide | null {
  const fresh = materializeSlide(session.opened, slideIndex)
  if (!fresh) return null
  return buildRenderSlide(fresh, session.opened.deck.size, {
    fitWidthPx: session.fitWidthPx,
    media: makeMediaResolver(session.opened),
    slideNo: slideIndex + 1,
  })
}
