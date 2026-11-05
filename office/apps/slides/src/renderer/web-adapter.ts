/**
 * Web adapter: reimplements the Electron preload globals `window.slidesApi`
 * (SlidesApi), `window.desktop` (DesktopFilesApi) and `window.projectApi`
 * (ProjectApi) for the browser SaaS. Imported first in main.tsx so the globals
 * exist before <App> mounts.
 *
 * This is a mechanical port of genoffice's apps/slides/src/main/slides-main.ts:
 * the electron main process is gone, so the parsed deck (OpenedPptx) lives in a
 * single module-level Session (adapter/session.ts) and every ipcMain.handle
 * body becomes a slidesApi method. All pptx mutation stays in @genoffice/pptx-
 * engine (browser-safe); this file only orchestrates: engine op -> buildRender-
 * Slide -> return.
 *
 * ponytail: the following are stubbed for the first web port (each marked
 * inline): text-autofit box refinement, theme-accent chart palettes, master
 * edit view, presenter multi-screen, pdf/image export, AI Office cloud gen,
 * style templates. Add when the feature actually ships.
 */
import { Buffer } from 'buffer'
// pptx-engine calls Buffer.from(...) in ~70 spots (node heritage). Provide the
// global before any engine module runs. web-adapter is imported first in main.tsx.
;(globalThis as { Buffer?: typeof Buffer }).Buffer ??= Buffer

import type { ProjectApi } from '@genoffice/project-store'
import { EMU_PER_PX_96, buildRenderSlide, type RenderSlide } from '@genoffice/pptx-render'
import {
  addChart,
  addElement,
  addMedia,
  addPicture,
  addSection,
  addSlideComment,
  addSmartArt,
  addTable,
  applyHeaderFooter,
  applyThemeToArchive,
  BUILTIN_LAYOUT_PREFIX,
  builtinLayoutInfos,
  commitSaved,
  copyElementData,
  copySlide,
  createBlankPptx,
  deleteElement,
  deleteSlide as engineDeleteSlide,
  deleteSlideComment,
  duplicateSlide,
  editChartElement,
  editGroupChildFill,
  editGroupChildStroke,
  editGroupChildTransform,
  editPictureSrcRect,
  editTableCellText,
  editTableStructure,
  editTableStyle,
  elementSpid,
  EMU_PER_PT,
  ensureBuiltinLayout,
  ensureRunLinkRels,
  ensureTableStylePart,
  findGroupChild,
  getChartElementData,
  getElementLink,
  getRunLinks,
  getSections,
  getSlideAnimations,
  getSlideComments,
  getSlideLinks,
  getSlideNotes,
  getSlideTransition,
  groupElements,
  insertBlankSlide,
  insertSlideWithLayout,
  isBackgroundLikeElement,
  listSlideLayouts,
  markChartEditable,
  materializeSlide,
  mergeTableCells,
  moveSection,
  moveSlide as engineMoveSlide,
  openPptx,
  pasteElements as enginePasteElements,
  pasteSlide,
  patchGroupChildText,
  readHeaderFooter,
  remapDeckColors,
  removeSection,
  renameSection,
  reorderElement,
  reparseDeck,
  resetSlideLayout,
  resizeTable,
  savePptx,
  setElementConnection,
  setElementFont,
  setElementImageFill,
  setElementLink,
  setElementParagraphFormat,
  setElementTextAnchor,
  setGroupChildFont,
  setGroupChildParagraphFormat,
  setPictureOpacity,
  setSlideAdvanceTime,
  setSlideAnimations,
  setSlideBackground,
  setSlideHidden,
  setSlideLayout,
  setSlideNotes,
  setSlideSize,
  setSlideTransition,
  setSections,
  setTableCellAnchor,
  setTableColWidth,
  setTableRowHeight,
  shouldOfferBuiltinLayouts,
  replaceAllInDeck,
  TABLE_STYLE_PRESETS,
  ungroupElement,
  updateConnectorsForMoved,
  type ElementClipboardItem,
  type GroupElement,
  type LinkTarget,
  type NewChartKind,
  type OpenedPptx,
  type ParagraphFormatPatch,
  type PictureElement,
  type SectionInfo,
  type Slide,
  type SlideAnimation,
  type SlideBundle,
  type TableStyleEdit,
  type TextElement,
  type ThemeSpec,
} from '@genoffice/pptx-engine'
import type {
  AiStreamRequest,
  DesktopFilesApi,
  OpenResult,
  SlidesApi,
} from '../shared/ipc'
import { applyEditParagraphs, collectParagraphFormatPatches, levelsChanged } from './adapter/edit-text'
import {
  beginHistoryBatch,
  buildAllRenderSlides,
  endHistoryBatch,
  getSession,
  pushHistory,
  rebuildSlide,
  rebuildSlideWithReparse,
  registerAiSnapshot,
  restoreAiSnapshot,
  restoreSnapshot,
  setSession,
  settleStaleHistoryBatch,
  takeSnapshot,
  type Session,
} from './adapter/session'

// Same origin; the Vite dev server proxies /ai /documents /projects /files.
const API = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
const EMBED = new URLSearchParams(location.search).get('embed') === '1'
const OFFICE_PATH = '/api/office'
const noop = () => {}
const unsub = () => noop

// ── byte <-> base64 (browser) ───────────────────────────────────────────
function bytesToBase64(bytes: Uint8Array): string {
  let bin = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) bin += String.fromCharCode(...bytes.subarray(i, i + chunk))
  return btoa(bin)
}
function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
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

/** Natural pixel size of image bytes (TIFF/decode failure -> 4:3 fallback). */
function imageNaturalSize(bytes: Uint8Array, ext: string): Promise<{ w: number; h: number }> {
  if (ext === 'tif' || ext === 'tiff') return Promise.resolve({ w: 800, h: 600 })
  return new Promise((resolve) => {
    const url = URL.createObjectURL(new Blob([bytes as BlobPart]))
    const img = new Image()
    img.onload = () => {
      resolve({ w: img.naturalWidth || 800, h: img.naturalHeight || 600 })
      URL.revokeObjectURL(url)
    }
    img.onerror = () => {
      resolve({ w: 800, h: 600 })
      URL.revokeObjectURL(url)
    }
    img.src = url
  })
}

// ── AI streaming over SSE (ported verbatim from docs adapter) ────────────
type StreamListener = (chunk: unknown) => void
const streamListeners = new Set<StreamListener>()
const controllers = new Map<string, AbortController>()
const emit = (chunk: unknown) => streamListeners.forEach((l) => l(chunk))

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
    emit({ requestId, type: 'error', error: e instanceof Error ? e.message : String(e) })
    return
  }
  if (!resp.ok || !resp.body) {
    controllers.delete(requestId)
    emit({ requestId, type: 'error', error: `HTTP ${resp.status}` })
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
          emit(JSON.parse(dataLine.slice(5).trim()))
        }
      }
    } catch (e) {
      if (!ac.signal.aborted)
        emit({ requestId, type: 'error', error: e instanceof Error ? e.message : String(e) })
    } finally {
      controllers.delete(requestId)
    }
  })()
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as T
}

/** data:<mime>;base64,<b64> → {base64,mime}, else null (gallery/AI data URLs skip the proxy). */
function dataUrlParts(url: string): { base64: string; mime: string } | null {
  const m = /^data:([^;,]+);base64,(.*)$/s.exec(url)
  return m ? { mime: m[1], base64: m[2] } : null
}

/** open deck's server doc id (session.path), or null when unsaved — scopes the
 * image gallery's "从文档提取" to the current deck. */
export function getCurrentDocId(): string | null {
  return getSession()?.path || null
}

// ── auth + storage (shared shape with docs adapter) ──────────────────────
const TOKEN_KEY = 'aioffice_token'
// Dev cross-origin handoff: the docs Home appends ?tok= when navigating here
// (separate vite origins don't share localStorage). Adopt it, then strip only
// the tok param so a ?gen= prompt survives for the App boot. No-op in prod
// (same origin → token already present, no ?tok appended).
;(() => {
  const p = new URLSearchParams(location.search)
  const tok = p.get('tok')
  if (!tok) return
  localStorage.setItem(TOKEN_KEY, tok)
  p.delete('tok')
  const q = p.toString()
  history.replaceState(null, '', location.pathname + (q ? `?${q}` : ''))
})()
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
function filenameFromDisposition(cd: string | null, fallback: string): string {
  const star = cd?.match(/filename\*=(?:UTF-8'')?([^;]+)/i)
  if (star) { try { return decodeURIComponent(star[1].replace(/"/g, '')) } catch { /* fall through */ } }
  const m = cd?.match(/filename="?([^";]+)"?/i)
  return m ? m[1] : fallback
}
async function putBlob(id: string, bytes: Uint8Array): Promise<boolean> {
  if (EMBED) {
    const r = await authFetch(`${OFFICE_PATH}/documents/${id}/blob`, {
      method: 'PUT',
      body: bytes as BodyInit,
    })
    if (r.ok) window.parent?.postMessage({ type: 'office:saved', docId: id }, location.origin)
    return r.ok
  }
  const r = await authFetch(`/documents/${id}/blob`, { method: 'PUT', body: bytes as BodyInit })
  return r.ok
}
/** upload a card thumbnail (base64 PNG, no data: prefix) for the Home doc grid */
export async function putThumb(id: string, pngBase64: string): Promise<void> {
  if (EMBED) return
  const bin = atob(pngBase64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  await authFetch(`/documents/${id}/thumb`, { method: 'PUT', body: bytes as BodyInit }).catch(
    () => {},
  )
}
async function createDoc(title: string, bytes: Uint8Array): Promise<string | null> {
  const r = await authFetch(`/documents?title=${encodeURIComponent(title)}`, {
    method: 'POST',
    body: bytes as BodyInit,
  })
  if (!r.ok) return null
  return ((await r.json()) as { id: string }).id
}

// ── px <-> EMU (viewport scale relative to the current deck width) ────────
function fitCtx(s: Session, fitWidthPx: number): { scale: number; toEmu: (px: number) => number } {
  s.fitWidthPx = fitWidthPx
  const baseWidthPx = s.opened.deck.size.cx / EMU_PER_PX_96
  const scale = fitWidthPx / baseWidthPx
  return { scale, toEmu: (px: number) => Math.round((px / scale) * EMU_PER_PX_96) }
}

/** Locate a text/shape element (undefined for pictures/tables/charts/etc.). */
function findText(slide: Slide, sourceId: string): TextElement | undefined {
  const el = slide.elements.find((e) => e.id === sourceId)
  return el && (el.type === 'text' || el.type === 'shape') ? (el as TextElement) : undefined
}

// ── open flow ────────────────────────────────────────────────────────────
async function openBytes(path: string, bytes: Uint8Array, fitWidthPx: number): Promise<OpenResult> {
  const opened = await openPptx(bytes)
  setSession({ path, opened, fitWidthPx, undoStack: [], redoStack: [] })
  return {
    path,
    slides: buildAllRenderSlides(opened, fitWidthPx),
    size: { cx: opened.deck.size.cx, cy: opened.deck.size.cy },
    // ponytail: theme body default-font lookup deferred (UI font-box fallback only)
  }
}
async function openFromId(id: string, fitWidthPx: number): Promise<OpenResult | null> {
  const path = EMBED ? `${OFFICE_PATH}/documents/${id}/blob` : `/documents/${id}/blob`
  const r = await authFetch(path)
  if (!r.ok) return null
  const bytes = new Uint8Array(await r.arrayBuffer())
  return openBytes(id, bytes, fitWidthPx)
}

// ── in-app clipboards (module-level; single-tab web build) ────────────────
let slideClipboard: { bundle: SlideBundle; png?: string } | null = null
let elemClipboard: { items: ElementClipboardItem[]; pasteCount: number } | null = null
let lastSlidePaste: { afterIndex: number; undoLen: number } | null = null

// ── chart color schemes ────────────────────────────────────────────────
// ponytail: theme-accent extraction deferred; uses the Office fallback palette.
const FALLBACK_ACCENTS = ['#4472C4', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5', '#70AD47']
function mixHex(hex: string, target: string, ratio: number): string {
  const h = hex.replace('#', '')
  const t = target.replace('#', '')
  const mix = (a: number, b: number) => Math.round(a + (b - a) * ratio)
  const r = mix(parseInt(h.slice(0, 2), 16), parseInt(t.slice(0, 2), 16))
  const g = mix(parseInt(h.slice(2, 4), 16), parseInt(t.slice(2, 4), 16))
  const b = mix(parseInt(h.slice(4, 6), 16), parseInt(t.slice(4, 6), 16))
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('').toUpperCase()}`
}
function chartColorSchemes(): Array<{ key: string; label: string; colors: string[] }> {
  const out = [
    { key: 'default', label: '主题默认', colors: FALLBACK_ACCENTS },
    { key: 'colorful', label: '多彩', colors: FALLBACK_ACCENTS },
    { key: 'colorful2', label: '多彩2', colors: [...FALLBACK_ACCENTS].reverse() },
  ]
  FALLBACK_ACCENTS.forEach((c, i) => {
    out.push({
      key: `mono${i}`,
      label: `单色 强调${i + 1}`,
      colors: [0, 0.2, 0.4, 0.6, 0.8].map((r) => mixHex(c, '#FFFFFF', r)),
    })
  })
  return out
}

/** Recolor full-bleed backdrop shapes so a page background change is visible. */
function recolorFullBleedBackdrops(slide: Slide, color: string): void {
  const size = getSession()!.opened.deck.size
  for (const el of slide.elements) {
    if (el.type !== 'shape' && el.type !== 'text') continue
    const t = el as TextElement
    if (t.fill?.type === 'solid' && isBackgroundLikeElement(el, size)) {
      t.fill = { type: 'solid', color }
      el.dirtyFill = true
    }
  }
}

function resolveLayoutPath(s: Session, layoutPath: string): string | null {
  if (layoutPath.startsWith(BUILTIN_LAYOUT_PREFIX)) {
    const key = layoutPath.slice(BUILTIN_LAYOUT_PREFIX.length)
    return ensureBuiltinLayout(s.opened.archive, s.opened.deck.size, key)
  }
  return layoutPath
}

function performSlidePaste(
  s: Session,
  op: { afterIndex: number; fitWidthPx: number; mode?: 'theme' | 'source' | 'picture' },
): { slides: RenderSlide[]; index: number; sourceId?: string } | null {
  if (!slideClipboard) return null
  const mode = op.mode ?? 'theme'
  if (mode === 'picture' && slideClipboard.png) {
    const anchorIndex = Math.min(Math.max(op.afterIndex, 0), s.opened.deck.slides.length - 1)
    const slide = s.opened.deck.slides[anchorIndex]
    const { cx, cy } = s.opened.deck.size
    const el = addPicture(s.opened, slide, {
      bytes: base64ToBytes(slideClipboard.png),
      ext: 'png',
      offset: { x: 0, y: 0, cx, cy },
    })
    if (!el) return null
    return { slides: buildAllRenderSlides(s.opened, op.fitWidthPx), index: anchorIndex, sourceId: el.id }
  }
  const slide = pasteSlide(s.opened, op.afterIndex, slideClipboard.bundle, {
    keepSourceFormatting: mode === 'source',
  })
  if (!slide) return null
  return {
    slides: buildAllRenderSlides(s.opened, op.fitWidthPx),
    index: s.opened.deck.slides.indexOf(slide),
  }
}

// ── the SlidesApi surface ─────────────────────────────────────────────────
const slidesApi: SlidesApi = {
  getLanguage: async () => 'zh',
  onLanguageChanged: unsub,

  openPptx: async (fitWidthPx) => {
    const f = await pickFile('.pptx')
    if (!f) return null
    return openBytes(f.name, new Uint8Array(await f.arrayBuffer()), fitWidthPx)
  },
  openPptxPath: async (path, fitWidthPx) => openFromId(path, fitWidthPx),
  consumePendingOpen: async (fitWidthPx) => {
    // ?open=<docId> lets the docs Home hand a pptx over cross-origin (dev);
    // same-origin prod could use sessionStorage, but the URL param works for both.
    const p = new URLSearchParams(location.search)
    const urlId = p.get('open')
    if (urlId) {
      p.delete('open')
      const q = p.toString()
      history.replaceState(null, '', location.pathname + (q ? `?${q}` : ''))
      return openFromId(urlId, fitWidthPx)
    }
    const id = sessionStorage.getItem('aioffice.pendingOpen')
    if (!id) return null
    sessionStorage.removeItem('aioffice.pendingOpen')
    return openFromId(id, fitWidthPx)
  },
  newBlank: async (fitWidthPx) => {
    const opened = await openPptx(await createBlankPptx())
    setSession({ path: '', opened, fitWidthPx, undoStack: [], redoStack: [] })
    return {
      path: '',
      slides: buildAllRenderSlides(opened, fitWidthPx),
      size: { cx: opened.deck.size.cx, cy: opened.deck.size.cy },
    }
  },

  // ponytail: HTML pipeline + cloud gen dropped — AI builds decks with local tools only
  htmlToPptx: async () => ({ error: 'HTML pipeline not available in web build' }),
  cloudGenStatus: async () => ({ enabled: false }),
  cloudGeneratePage: async () => ({ ok: false, error: 'cloud generation not available' }),

  editText: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (op.groupId) {
      const found = findGroupChild(slide, op.groupId, op.sourceId)
      if (!found || (found.child.type !== 'text' && found.child.type !== 'shape')) {
        restoreSnapshot(s, s.undoStack.pop()!)
        return null
      }
      const child = found.child as TextElement
      const newParas = applyEditParagraphs(child.text?.paragraphs ?? [], op.paragraphs)
      child.text = { ...(child.text ?? { paragraphs: [] }), paragraphs: newParas }
      ensureRunLinkRels(s.opened, op.slideIndex, newParas)
      if (!patchGroupChildText(slide, op.groupId, child)) {
        restoreSnapshot(s, s.undoStack.pop()!)
        return null
      }
      for (const { index, patch } of collectParagraphFormatPatches(op.paragraphs))
        setGroupChildParagraphFormat(slide, op.groupId, op.sourceId, patch, [index])
      return rebuildSlide(s, op.slideIndex)
    }
    const el = findText(slide, op.sourceId)
    if (!el) {
      restoreSnapshot(s, s.undoStack.pop()!)
      return null
    }
    const oldParas = el.text?.paragraphs ?? []
    const levelDirty = levelsChanged(oldParas, op.paragraphs)
    const newParas = applyEditParagraphs(oldParas, op.paragraphs)
    el.text = { ...(el.text ?? { paragraphs: [] }), paragraphs: newParas }
    ensureRunLinkRels(s.opened, op.slideIndex, newParas)
    el.dirty = true
    for (const { index, patch } of collectParagraphFormatPatches(op.paragraphs))
      setElementParagraphFormat(slide, op.sourceId, patch, [index])
    if (levelDirty) {
      el.dirtyPPr = { ...el.dirtyPPr, level: true, indents: true }
      materializeSlide(s.opened, op.slideIndex)
      return rebuildSlide(s, op.slideIndex)
    }
    // ponytail: autofit box grow/shrink refinement skipped; add when text-autofit fidelity matters
    return rebuildSlide(s, op.slideIndex)
  },

  setElementFont: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const patch = {
      ...(op.fontFamily != null ? { fontFamily: op.fontFamily } : {}),
      ...(op.fontSizePt != null ? { fontSizePt: op.fontSizePt } : {}),
      ...(op.strike != null ? { strike: op.strike } : {}),
      ...(op.bold != null ? { bold: op.bold } : {}),
      ...(op.italic != null ? { italic: op.italic } : {}),
      ...(op.underline != null ? { underline: op.underline } : {}),
      ...(op.color != null ? { color: op.color } : {}),
    }
    let changed = false
    for (const id of op.sourceIds) {
      const ok = op.groupId
        ? setGroupChildFont(slide, op.groupId, id, patch)
        : setElementFont(slide, id, patch)
      changed = changed || ok
    }
    if (!changed) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  setElementParagraphFormat: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const patch: ParagraphFormatPatch = {
      ...(op.bullet ? { bullet: op.bullet } : {}),
      ...(op.bulletChar != null ? { bulletChar: op.bulletChar } : {}),
      ...(op.bulletHangEmu != null ? { bulletHangEmu: op.bulletHangEmu } : {}),
      ...(op.bulletSizePct != null ? { bulletSizePct: op.bulletSizePct } : {}),
      ...(op.bulletColor != null ? { bulletColor: op.bulletColor } : {}),
      ...(op.lineSpacingPct != null ? { lineSpacingPct: op.lineSpacingPct } : {}),
      ...(op.spaceBeforePt != null ? { spaceBeforePt: op.spaceBeforePt } : {}),
      ...(op.spaceAfterPt != null ? { spaceAfterPt: op.spaceAfterPt } : {}),
      ...(op.align != null ? { align: op.align } : {}),
      ...(op.indentDelta != null ? { indentDelta: op.indentDelta } : {}),
    }
    let changed = false
    for (const id of op.sourceIds) {
      const ok = op.groupId
        ? setGroupChildParagraphFormat(slide, op.groupId, id, patch)
        : setElementParagraphFormat(slide, id, patch)
      changed = changed || ok
    }
    if (!changed) {
      s.undoStack.pop()
      return null
    }
    if (op.indentDelta) materializeSlide(s.opened, op.slideIndex)
    return rebuildSlide(s, op.slideIndex)
  },

  findReplace: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const { count } = replaceAllInDeck(s.opened.deck, op.find, op.replace, {
      matchCase: op.matchCase,
      firstOnly: op.firstOnly,
      slideIndex: op.slideIndex,
      elementId: op.elementId,
    })
    if (!count) {
      s.undoStack.pop()
      return { count: 0, slides: null }
    }
    return { count, slides: buildAllRenderSlides(s.opened, s.fitWidthPx) }
  },

  setSlideLayout: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    let out: Slide | null
    if (op.layoutPath) {
      const path = resolveLayoutPath(s, op.layoutPath)
      out = path ? setSlideLayout(s.opened, op.slideIndex, path) : null
    } else {
      out = resetSlideLayout(s.opened, op.slideIndex)
    }
    if (!out) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlideWithReparse(s, op.slideIndex)
  },

  setSlideSize: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    if (!setSlideSize(s.opened, op.cx, op.cy)) {
      s.undoStack.pop()
      return null
    }
    s.metaDirty = true
    return buildAllRenderSlides(s.opened, s.fitWidthPx)
  },
  getSlideSize: async () => {
    const s = getSession()
    return s ? { ...s.opened.deck.size } : null
  },

  editTransform: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    // Live-preview undo semantics: one snapshot per drag gesture.
    if (op.preview) {
      if (!s.transformPreview) {
        pushHistory(s)
        s.transformPreview = true
      }
    } else if (s.transformPreview) {
      s.transformPreview = false
    } else {
      pushHistory(s)
    }
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    if (op.groupId) {
      const found = findGroupChild(slide, op.groupId, op.sourceId)
      if (!found) return null
      // ponytail: group-child drag mapping is a best-effort reconstruction (px viewport
      // -> slide EMU -> group child coord system via chOff/chExt scale). Top-level drag
      // (the common case) is exact.
      const gExt = found.grp.transform.offset
      const ch = found.grp.childOffset
      const sx = ch?.cx ? gExt.cx / ch.cx : 1
      const sy = ch?.cy ? gExt.cy / ch.cy : 1
      const offset = {
        x: Math.round((toEmu(op.xPx) - gExt.x) / sx + (ch?.x ?? 0)),
        y: Math.round((toEmu(op.yPx) - gExt.y) / sy + (ch?.y ?? 0)),
        cx: Math.round(toEmu(op.wPx) / sx),
        cy: Math.round(toEmu(op.hPx) / sy),
      }
      if (!editGroupChildTransform(slide, op.groupId, op.sourceId, offset, op.rotationDeg)) return null
      return rebuildSlide(s, op.slideIndex)
    }
    const el = slide.elements.find((e) => e.id === op.sourceId)
    if (!el) return null
    const isTable = el.type === 'table'
    if (isTable) resizeTable(slide, op.sourceId, toEmu(op.wPx), toEmu(op.hPx))
    el.transform.offset = {
      x: toEmu(op.xPx),
      y: toEmu(op.yPx),
      cx: isTable ? el.transform.offset.cx : toEmu(op.wPx),
      cy: isTable ? el.transform.offset.cy : toEmu(op.hPx),
    }
    el.transform.rot = Math.round(op.rotationDeg * 60000)
    el.dirtyTransform = true
    updateConnectorsForMoved(slide, [op.sourceId])
    return rebuildSlide(s, op.slideIndex)
  },

  editConnectorEndpoints: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const el = slide.elements.find((e) => e.id === op.sourceId)
    if (!el) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const x1 = toEmu(op.x1Px)
    const y1 = toEmu(op.y1Px)
    const x2 = toEmu(op.x2Px)
    const y2 = toEmu(op.y2Px)
    el.transform.offset = {
      x: Math.min(x1, x2),
      y: Math.min(y1, y2),
      cx: Math.abs(x2 - x1),
      cy: Math.abs(y2 - y1),
    }
    el.transform.rot = 0
    el.transform.flipH = x1 > x2
    el.transform.flipV = y1 > y2
    el.dirtyTransform = true
    const toRef = (r?: { targetId: string; idx: number } | null) => {
      if (r === undefined) return undefined
      if (r === null) return null
      const t = slide.elements.find((e) => e.id === r.targetId)
      const spid = t ? elementSpid(t) : null
      return spid != null ? { id: spid, idx: r.idx } : null
    }
    setElementConnection(slide, op.sourceId, { start: toRef(op.start), end: toRef(op.end) })
    return rebuildSlide(s, op.slideIndex)
  },

  batchEditTransform: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    if (op.items.some((it) => !slide.elements.find((e) => e.id === it.sourceId))) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    for (const it of op.items) {
      const el = slide.elements.find((e) => e.id === it.sourceId)!
      el.transform.offset = { x: toEmu(it.xPx), y: toEmu(it.yPx), cx: toEmu(it.wPx), cy: toEmu(it.hPx) }
      el.transform.rot = Math.round(it.rotationDeg * 60000)
      el.dirtyTransform = true
    }
    updateConnectorsForMoved(slide, op.items.map((it) => it.sourceId))
    return rebuildSlide(s, op.slideIndex)
  },

  getRenderSlides: async () => {
    const s = getSession()
    if (!s) return null
    return s.opened.deck.slides.map((_, i) => rebuildSlide(s, i)!).filter(Boolean)
  },

  editPictureSrcRect: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (!editPictureSrcRect(slide, op.sourceId, op.srcRect)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  editPictureOpacity: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (!setPictureOpacity(slide, op.sourceId, op.opacity)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  editImageFill: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const f = await pickFile('image/*')
    if (!f) return null
    const bytes = new Uint8Array(await f.arrayBuffer())
    const ext = (f.name.split('.').pop() ?? 'png').toLowerCase()
    pushHistory(s)
    if (!setElementImageFill(s.opened, slide, op.sourceId, bytes, ext)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  setTextAnchor: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (!setElementTextAnchor(slide, op.sourceId, op.anchor)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  clipboardExternal: async () => {
    // ponytail: OS clipboard image/text probing skipped (browser has no sync clipboard read)
    if (slideClipboard) return { kind: 'slide' }
    if (elemClipboard) return { kind: 'internal' }
    return { kind: 'none' }
  },

  groupElements: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const r = groupElements(s.opened, op.slideIndex, op.sourceIds)
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, groupId: r.groupId }
  },
  ungroupElement: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    if (!ungroupElement(s.opened, op.slideIndex, op.sourceId)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  addElement: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const paragraphs = op.paragraphs?.length
      ? applyEditParagraphs([], op.paragraphs)
      : op.text
        ? op.text.split('\n').map((line) => ({ runs: [{ text: line }] }))
        : undefined
    const el = addElement(slide, {
      kind: op.kind,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: toEmu(op.wPx), cy: toEmu(op.hPx) },
      ...(paragraphs ? { paragraphs } : {}),
      ...(op.fillColor ? { fillColor: op.fillColor } : {}),
      ...(op.stroke
        ? { stroke: { color: op.stroke.color, widthEmu: Math.round(op.stroke.widthPt * EMU_PER_PT) } }
        : {}),
    })
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: el.id }
  },

  deleteElement: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide || !slide.elements.find((e) => e.id === op.sourceId)) return null
    pushHistory(s)
    if (!deleteElement(slide, op.sourceId)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  addSlide: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    if (!duplicateSlide(s.opened, op.sourceIndex, { clearText: !!op.clearText })) {
      s.undoStack.pop()
      return null
    }
    return { slides: buildAllRenderSlides(s.opened, op.fitWidthPx), index: op.sourceIndex + 1 }
  },
  addBlankSlide: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    if (!insertBlankSlide(s.opened, op.sourceIndex)) {
      s.undoStack.pop()
      return null
    }
    return { slides: buildAllRenderSlides(s.opened, op.fitWidthPx), index: op.sourceIndex + 1 }
  },
  addSlideWithLayout: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const path = resolveLayoutPath(s, op.layoutPath)
    if (!path || !insertSlideWithLayout(s.opened, op.sourceIndex, path)) {
      s.undoStack.pop()
      return null
    }
    return { slides: buildAllRenderSlides(s.opened, op.fitWidthPx), index: op.sourceIndex + 1 }
  },
  getLayouts: async () => {
    const s = getSession()
    if (!s) return null
    const infos = listSlideLayouts(s.opened.archive)
    const layouts = [...infos]
    if (shouldOfferBuiltinLayouts(infos))
      layouts.push(...builtinLayoutInfos(s.opened.deck.size, new Set(infos.map((l) => l.name))))
    return { layouts, size: { ...s.opened.deck.size } }
  },

  copySlide: async (slideIndex, pngBase64) => {
    const s = getSession()
    if (!s) return false
    const bundle = copySlide(s.opened, slideIndex)
    if (!bundle) return false
    slideClipboard = { bundle, ...(pngBase64 ? { png: pngBase64 } : {}) }
    return true
  },
  hasSlideClipboard: async () => slideClipboard !== null,
  pasteSlide: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const r = performSlidePaste(s, op)
    if (!r) {
      s.undoStack.pop()
      return null
    }
    lastSlidePaste = { afterIndex: op.afterIndex, undoLen: s.undoStack.length }
    return r
  },
  repasteSlide: async (op) => {
    const s = getSession()
    if (!s || !lastSlidePaste || s.undoStack.length !== lastSlidePaste.undoLen) return null
    restoreSnapshot(s, s.undoStack.pop()!)
    pushHistory(s)
    const r = performSlidePaste(s, { afterIndex: lastSlidePaste.afterIndex, fitWidthPx: op.fitWidthPx, mode: op.mode })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    lastSlidePaste.undoLen = s.undoStack.length
    return r
  },

  deleteSlide: async (slideIndex) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    if (!engineDeleteSlide(s.opened, slideIndex)) {
      s.undoStack.pop()
      return null
    }
    return buildAllRenderSlides(s.opened, s.fitWidthPx)
  },

  reorderElement: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (!reorderElement(slide, op.sourceId, op.dir)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },

  editTableCell: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const paras = applyEditParagraphs([], op.paragraphs)
    if (!editTableCellText(slide, op.sourceId, op.row, op.col, paras)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },
  tableStructure: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const r = editTableStructure(s.opened, op.slideIndex, op.sourceId, {
      kind: op.kind,
      index: op.index,
      ...(op.before ? { before: true } : {}),
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },
  tableMerge: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const r = mergeTableCells(s.opened, op.slideIndex, op.sourceId, {
      kind: op.kind,
      row: op.row,
      col: op.col,
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },
  setTableColWidth: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    if (!setTableColWidth(slide, op.sourceId, op.col, toEmu(op.wPx))) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },
  setTableRowHeight: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    if (!setTableRowHeight(slide, op.sourceId, op.row, toEmu(op.hPx))) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },
  setTableCellAnchor: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (!setTableCellAnchor(slide, op.sourceId, op.row, op.col, op.anchor)) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },
  editTableStyle: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const elIdx = slide.elements.findIndex((e) => e.id === op.sourceId)
    if (elIdx < 0) return null
    pushHistory(s)
    let edit: TableStyleEdit
    if (op.styleName) {
      const preset = TABLE_STYLE_PRESETS[op.styleName]
      if (preset?.styleId && preset.styleDefXml)
        ensureTableStylePart(s.opened, preset.styleId, preset.styleDefXml)
      edit = { preset: op.styleName } as unknown as TableStyleEdit
    } else {
      edit = {
        ...(op.firstRow != null ? { firstRow: op.firstRow } : {}),
        ...(op.bandRow != null ? { bandRow: op.bandRow } : {}),
        ...(op.shadingColor !== undefined ? { shadingColor: op.shadingColor } : {}),
        ...(op.borderColor !== undefined ? { borderColor: op.borderColor } : {}),
        ...(op.borderWidthPt !== undefined ? { borderWidthPt: op.borderWidthPt } : {}),
        ...(op.borderPreset !== undefined ? { borderPreset: op.borderPreset } : {}),
        ...(op.cells ? { cells: op.cells } : {}),
      } as unknown as TableStyleEdit
    }
    if (!editTableStyle(slide, op.sourceId, edit)) {
      s.undoStack.pop()
      return null
    }
    const rebuilt = rebuildSlideWithReparse(s, op.slideIndex)
    const newId = s.opened.deck.slides[op.slideIndex]?.elements[elIdx]?.id ?? null
    return { slide: rebuilt!, sourceId: newId }
  },

  editFill: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (op.groupId) {
      const gf =
        typeof op.fill === 'string'
          ? op.fill
          : {
              stops: [
                { pos: 0, color: op.fill.gradient.from },
                { pos: 1, color: op.fill.gradient.to },
              ],
              ...(op.fill.gradient.radial
                ? { radial: true }
                : { angle: Math.round((op.fill.gradient.angleDeg ?? 0) * 60000) }),
            }
      if (!editGroupChildFill(slide, op.groupId, op.sourceId, gf)) {
        s.undoStack.pop()
        return null
      }
      return rebuildSlide(s, op.slideIndex)
    }
    const el = findText(slide, op.sourceId)
    if (!el) {
      s.undoStack.pop()
      return null
    }
    el.fill =
      typeof op.fill === 'string'
        ? op.fill === 'none'
          ? { type: 'none' }
          : { type: 'solid', color: op.fill }
        : {
            type: 'gradient',
            stops: [
              { pos: 0, color: op.fill.gradient.from },
              { pos: 1, color: op.fill.gradient.to },
            ],
            ...(op.fill.gradient.radial
              ? { path: 'circle' }
              : { angle: Math.round((op.fill.gradient.angleDeg ?? 0) * 60000) }),
          }
    el.dirtyFill = true
    return rebuildSlide(s, op.slideIndex)
  },

  editStroke: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    if (op.groupId) {
      const stroke = op.stroke
        ? {
            color: op.stroke.color,
            widthEmu: Math.round(op.stroke.widthPt * EMU_PER_PT),
            ...(op.stroke.dash ? { dash: op.stroke.dash } : {}),
          }
        : null
      if (!editGroupChildStroke(slide, op.groupId, op.sourceId, stroke)) {
        s.undoStack.pop()
        return null
      }
      return rebuildSlide(s, op.slideIndex)
    }
    const el = findText(slide, op.sourceId) ?? (slide.elements.find((e) => e.id === op.sourceId) as TextElement | undefined)
    if (!el) {
      s.undoStack.pop()
      return null
    }
    el.stroke = op.stroke
      ? {
          fill: { type: 'solid', color: op.stroke.color },
          width: Math.round(op.stroke.widthPt * EMU_PER_PT),
          ...(op.stroke.dash ? { dash: op.stroke.dash } : {}),
        }
      : undefined
    el.dirtyStroke = true
    return rebuildSlide(s, op.slideIndex)
  },

  flipElements: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    let any = false
    for (const id of op.sourceIds) {
      const el = op.groupId
        ? findGroupChild(slide, op.groupId, id)?.child
        : slide.elements.find((e) => e.id === id)
      if (!el) continue
      if (op.axis === 'h') el.transform.flipH = !el.transform.flipH
      else el.transform.flipV = !el.transform.flipV
      el.dirtyTransform = true
      any = true
    }
    if (!any) {
      s.undoStack.pop()
      return null
    }
    updateConnectorsForMoved(slide, op.sourceIds)
    return rebuildSlide(s, op.slideIndex)
  },

  editBackground: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const targets = op.slideIndex === -1 ? s.opened.deck.slides.map((_, i) => i) : [op.slideIndex]
    for (const i of targets) {
      const slide = s.opened.deck.slides[i]
      if (!slide) continue
      setSlideBackground(slide, op.color)
      recolorFullBleedBackdrops(slide, op.color)
    }
    return buildAllRenderSlides(s.opened, op.fitWidthPx)
  },

  insertImage: async (slideIndex, fitWidthPx) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[slideIndex]
    if (!slide) return null
    const f = await pickFile('image/*')
    if (!f) return null
    const bytes = new Uint8Array(await f.arrayBuffer())
    const ext = (f.name.split('.').pop() ?? 'png').toLowerCase()
    const { w, h } = await imageNaturalSize(bytes, ext)
    const { cx, cy } = s.opened.deck.size
    const boxW = cx / 2
    const boxH = (boxW * h) / w
    const offset = { x: Math.round((cx - boxW) / 2), y: Math.round((cy - boxH) / 2), cx: Math.round(boxW), cy: Math.round(boxH) }
    pushHistory(s)
    const el = addPicture(s.opened, slide, { bytes, ext, offset })
    if (!el) {
      s.undoStack.pop()
      return { error: 'unsupported', ext }
    }
    return { slide: rebuildSlide(s, slideIndex)!, sourceId: el.id }
  },

  copyElements: async (op) => {
    const s = getSession()
    if (!s) return 0
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return 0
    const items = op.sourceIds
      .map((id) => slide.elements.find((e) => e.id === id))
      .filter((e): e is NonNullable<typeof e> => !!e)
      .map((el) => copyElementData(s.opened, slide, el))
    if (items.length) elemClipboard = { items, pasteCount: 0 }
    return items.length
  },
  pasteElements: async (op) => {
    const s = getSession()
    if (!s || !elemClipboard) return null
    pushHistory(s)
    const { scale } = fitCtx(s, op.fitWidthPx)
    const shift = Math.round(((16 * (elemClipboard.pasteCount + 1)) / scale) * EMU_PER_PX_96)
    const r = enginePasteElements(s.opened, op.slideIndex, elemClipboard.items, { dx: shift, dy: shift })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    elemClipboard.pasteCount++
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceIds: r.elementIds }
  },
  duplicateElements: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const items = op.sourceIds
      .map((id) => slide.elements.find((e) => e.id === id))
      .filter((e): e is NonNullable<typeof e> => !!e)
      .map((el) => copyElementData(s.opened, slide, el))
    if (!items.length) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const r = enginePasteElements(s.opened, op.slideIndex, items, { dx: toEmu(op.dxPx), dy: toEmu(op.dyPx) })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceIds: r.elementIds }
  },

  addTable: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const r = addTable(s.opened, op.slideIndex, {
      rows: op.rows,
      cols: op.cols,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: toEmu(op.wPx), cy: toEmu(op.hPx) },
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },

  addInk: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const el = addPicture(s.opened, slide, {
      bytes: base64ToBytes(op.base64),
      ext: 'png',
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: Math.max(1, toEmu(op.wPx)), cy: Math.max(1, toEmu(op.hPx)) },
      name: 'aislides-ink ' + Date.now().toString(36),
      descr: op.payload,
    })
    if (!el) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: el.id }
  },

  addChart: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const r = addChart(s.opened, op.slideIndex, {
      kind: (op.kind === 'barH' ? 'bar' : op.kind) as NewChartKind,
      ...(op.kind === 'barH' ? { barDir: 'bar' as const } : {}),
      ...(op.title ? { title: op.title } : {}),
      categories: op.categories,
      series: op.series,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: toEmu(op.wPx), cy: toEmu(op.hPx) },
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },
  addSmartArt: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const r = addSmartArt(s.opened, op.slideIndex, {
      layout: op.layout,
      items: op.items,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: toEmu(op.wPx), cy: toEmu(op.hPx) },
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },
  addImageBytes: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const el = addPicture(s.opened, slide, {
      bytes: base64ToBytes(op.base64),
      ext: op.ext,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: toEmu(op.wPx), cy: toEmu(op.hPx) },
      ...(op.name ? { name: op.name } : {}),
    })
    if (!el) {
      s.undoStack.pop()
      return { error: 'unsupported', ext: op.ext }
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: el.id }
  },

  insertMedia: async (slideIndex, kind, fitWidthPx) => {
    const s = getSession()
    if (!s) return null
    fitCtx(s, fitWidthPx)
    const f = await pickFile(kind === 'video' ? 'video/*' : 'audio/*')
    if (!f) return null
    const bytes = new Uint8Array(await f.arrayBuffer())
    const ext = (f.name.split('.').pop() ?? '').toLowerCase()
    const { cx, cy } = s.opened.deck.size
    const w = kind === 'video' ? cx * 0.6 : cx * 0.24
    const h = kind === 'video' ? (w * 9) / 16 : cy * 0.09
    const offset = { x: Math.round((cx - w) / 2), y: Math.round((cy - h) / 2), cx: Math.round(w), cy: Math.round(h) }
    pushHistory(s)
    const r = addMedia(s.opened, slideIndex, { kind, bytes, ext, offset, name: f.name })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, slideIndex)!, sourceId: r.elementId }
  },
  addMediaBytes: async (op) => {
    const s = getSession()
    if (!s) return null
    fitCtx(s, op.fitWidthPx)
    const { cx, cy } = s.opened.deck.size
    const w = cx * 0.6
    const h = (w * 9) / 16
    const offset = { x: Math.round((cx - w) / 2), y: Math.round((cy - h) / 2), cx: Math.round(w), cy: Math.round(h) }
    pushHistory(s)
    const r = addMedia(s.opened, op.slideIndex, {
      kind: op.kind,
      bytes: base64ToBytes(op.base64),
      ext: op.ext,
      offset,
      ...(op.name ? { name: op.name } : {}),
    })
    if (!r) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: r.elementId }
  },
  getMediaData: async (slideIndex, sourceId) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[slideIndex]
    const el = slide?.elements.find((e) => e.id === sourceId)
    if (!el || el.type !== 'picture') return null
    const p = el as PictureElement
    if (!p.media?.target) return null
    if (p.media.external) return { kind: p.media.kind, dataUrl: p.media.target }
    const bytes = s.opened.archive.readBytes(p.media.target)
    if (!bytes) return null
    const ext = (p.media.target.split('.').pop() ?? '').toLowerCase()
    const AV_MIME: Record<string, string> = {
      mp4: 'video/mp4',
      webm: 'video/webm',
      mov: 'video/quicktime',
      mp3: 'audio/mpeg',
      wav: 'audio/wav',
      m4a: 'audio/mp4',
      ogg: 'audio/ogg',
    }
    return { kind: p.media.kind, dataUrl: `data:${AV_MIME[ext] ?? 'application/octet-stream'};base64,${bytesToBase64(bytes)}` }
  },
  // ponytail: 3D model insert deferred (needs glb picker + poster); return null (no-op)
  insertModel3d: async () => null,

  setLink: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const out = setElementLink(s.opened, op.slideIndex, op.sourceId, op.target as LinkTarget | null)
    if (!out) {
      s.undoStack.pop()
      return null
    }
    return rebuildSlide(s, op.slideIndex)
  },
  getLink: async (slideIndex, sourceId) => {
    const s = getSession()
    return s ? getElementLink(s.opened, slideIndex, sourceId) : null
  },
  getSlideLinks: async (slideIndex) => {
    const s = getSession()
    if (!s) return []
    return getSlideLinks(s.opened, slideIndex).map(({ elementId, target }) => ({ sourceId: elementId, target }))
  },
  getRunLinks: async (slideIndex) => {
    const s = getSession()
    if (!s) return []
    return getRunLinks(s.opened, slideIndex).map(({ elementId, ...rest }) => ({ sourceId: elementId, ...rest }))
  },

  applyHeaderFooter: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const ok = applyHeaderFooter(s.opened, {
      footer: op.footer ?? null,
      slideNum: !!op.slideNum,
      date: op.date ?? null,
      ...(op.dateAuto ? { dateAuto: true } : {}),
    })
    if (!ok) {
      s.undoStack.pop()
      return null
    }
    return buildAllRenderSlides(s.opened, op.fitWidthPx)
  },
  getHeaderFooter: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    return slide ? readHeaderFooter(slide) : { footer: null, slideNum: false, date: null }
  },

  applyTheme: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    const spec: ThemeSpec = {
      name: op.name,
      colors: op.colors,
      ...(op.majorFont ? { majorFont: op.majorFont } : {}),
      ...(op.minorFont ? { minorFont: op.minorFont } : {}),
    }
    try {
      commitSaved(s.opened)
      const a = applyThemeToArchive(s.opened, spec)
      const b = remapDeckColors(s.opened, spec)
      if (a === 0 && b === 0) {
        s.undoStack.pop()
        return null
      }
      s.opened = reparseDeck(s.opened)
    } catch (e) {
      restoreSnapshot(s, s.undoStack.pop()!)
      return { error: e instanceof Error ? e.message : String(e) }
    }
    s.metaDirty = true
    return buildAllRenderSlides(s.opened, op.fitWidthPx)
  },

  setTransition: async (op) => {
    const s = getSession()
    if (!s) return false
    const targets = op.slideIndex === -1 ? s.opened.deck.slides.map((_, i) => i) : [op.slideIndex]
    pushHistory(s)
    for (const i of targets) {
      const slide = s.opened.deck.slides[i]
      if (slide) setSlideTransition(slide, op.kind)
    }
    return true
  },
  getTransition: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    return slide ? getSlideTransition(slide) : 'none'
  },
  setAdvanceTimes: async (op) => {
    const s = getSession()
    if (!s) return false
    pushHistory(s)
    for (const { slideIndex, ms } of op.times) {
      const slide = s.opened.deck.slides[slideIndex]
      if (slide) setSlideAdvanceTime(slide, ms)
    }
    return true
  },

  getAnimations: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    if (!s || !slide) return []
    const bySpid = new Map<number, (typeof slide.elements)[number]>()
    for (const el of slide.elements) {
      const spid = elementSpid(el)
      if (spid != null) bySpid.set(spid, el)
    }
    return getSlideAnimations(slide)
      .map((a) => {
        const el = bySpid.get(a.spid)
        if (!el) return null
        return {
          sourceId: el.id,
          targetName: el.name || '对象',
          effect: a.effect,
          trigger: a.trigger,
          durationMs: a.durationMs,
          delayMs: a.delayMs,
          ...(a.motionPath ? { motionPath: a.motionPath } : {}),
          ...(a.paragraph != null ? { paragraph: a.paragraph } : {}),
        }
      })
      .filter((x): x is NonNullable<typeof x> => !!x)
  },
  getShapeKeys: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    if (!slide) return []
    return slide.elements.map((el) => ({ sourceId: el.id, spid: elementSpid(el), name: el.name ?? '' }))
  },
  setAnimations: async (op) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[op.slideIndex]
    if (!s || !slide) return false
    const anims: SlideAnimation[] = []
    for (const it of op.items) {
      const el = slide.elements.find((e) => e.id === it.sourceId)
      const spid = el ? elementSpid(el) : null
      if (spid == null) continue
      anims.push({
        spid,
        effect: it.effect,
        trigger: it.trigger,
        durationMs: it.durationMs,
        delayMs: it.delayMs,
        ...(it.motionPath ? { motionPath: it.motionPath } : {}),
        ...(it.paragraph != null ? { paragraph: it.paragraph } : {}),
      })
    }
    pushHistory(s)
    setSlideAnimations(slide, anims)
    return true
  },
  setSlideHidden: async (op) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[op.slideIndex]
    if (!s || !slide) return null
    pushHistory(s)
    setSlideHidden(slide, op.hidden)
    return rebuildSlide(s, op.slideIndex)
  },

  getSections: async () => {
    const s = getSession()
    return s ? getSections(s.opened) : []
  },
  setSections: async (sections) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    setSections(s.opened, sections)
    s.metaDirty = true
    return getSections(s.opened)
  },
  addSection: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    addSection(s.opened, op.atSlideIndex, op.name)
    s.metaDirty = true
    return getSections(s.opened)
  },
  renameSection: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    renameSection(s.opened, op.id, op.name)
    s.metaDirty = true
    return getSections(s.opened)
  },
  removeSection: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    removeSection(s.opened, op.id, { keepSlides: true })
    s.metaDirty = true
    return getSections(s.opened)
  },
  moveSection: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    moveSection(s.opened, op.id, op.dir)
    s.metaDirty = true
    return { slides: buildAllRenderSlides(s.opened, s.fitWidthPx), sections: getSections(s.opened) }
  },
  moveSlide: async (op) => {
    const s = getSession()
    if (!s) return null
    pushHistory(s)
    engineMoveSlide(s.opened, op.fromIndex, op.toIndex)
    s.metaDirty = true
    return { slides: buildAllRenderSlides(s.opened, s.fitWidthPx), sections: getSections(s.opened) }
  },

  getNotes: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    return s && slide ? getSlideNotes(s.opened.archive, slide.path) : ''
  },
  setNotes: async (op) => {
    const s = getSession()
    if (!s) return false
    pushHistory(s)
    if (!setSlideNotes(s.opened, op.slideIndex, op.text)) {
      s.undoStack.pop()
      return false
    }
    s.metaDirty = true
    return true
  },

  getComments: async (slideIndex) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    return s && slide ? getSlideComments(s.opened.archive, slide.path) : []
  },
  addComment: async (op) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[op.slideIndex]
    if (!s || !slide) return null
    pushHistory(s)
    if (!addSlideComment(s.opened, op.slideIndex, { author: 'User', text: op.text })) {
      s.undoStack.pop()
      return null
    }
    return getSlideComments(s.opened.archive, slide.path)
  },
  deleteComment: async (op) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[op.slideIndex]
    if (!s || !slide) return null
    pushHistory(s)
    if (!deleteSlideComment(s.opened, op.slideIndex, { authorId: op.authorId, idx: op.idx })) {
      s.undoStack.pop()
      return null
    }
    return getSlideComments(s.opened.archive, slide.path)
  },

  nativeClipboard: async () => {}, // ponytail: OS clipboard cut/copy/paste no-op in web

  beginHistoryBatch: async () => {
    const s = getSession()
    if (!s) return false
    beginHistoryBatch(s)
    return true
  },
  endHistoryBatch: async () => {
    const s = getSession()
    if (!s) return null
    const before = endHistoryBatch(s)
    return before ? registerAiSnapshot(s, before) : null
  },
  aiSnapshotRestore: async (id) => {
    const s = getSession()
    if (!s || s.masterEdit || s.historyBatch) return null
    if (!restoreAiSnapshot(s, id)) return null
    return buildAllRenderSlides(s.opened, s.fitWidthPx)
  },
  undo: async () => {
    const s = getSession()
    if (!s || s.masterEdit) return null
    settleStaleHistoryBatch(s)
    if (!s.undoStack.length) return null
    s.redoStack.push(takeSnapshot(s))
    restoreSnapshot(s, s.undoStack.pop()!)
    return buildAllRenderSlides(s.opened, s.fitWidthPx)
  },
  redo: async () => {
    const s = getSession()
    if (!s || s.masterEdit) return null
    if (!s.redoStack.length) return null
    s.undoStack.push(takeSnapshot(s))
    restoreSnapshot(s, s.redoStack.pop()!)
    return buildAllRenderSlides(s.opened, s.fitWidthPx)
  },

  editChart: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const elIdx = slide.elements.findIndex((e) => e.id === op.sourceId)
    if (elIdx < 0) return null
    pushHistory(s)
    markChartEditable(slide, op.sourceId)
    const patch = {
      ...(op.kind ? { kind: (op.kind === 'barH' ? 'bar' : op.kind) as NewChartKind } : {}),
      ...(op.kind === 'barH' ? { barDir: 'bar' as const } : {}),
      ...(op.categories ? { categories: op.categories } : {}),
      ...(op.series ? { series: op.series } : {}),
      ...(op.title != null ? { title: op.title } : {}),
      ...(op.colorScheme
        ? { colorScheme: chartColorSchemes().find((c) => c.key === op.colorScheme)?.colors ?? FALLBACK_ACCENTS }
        : {}),
      ...(op.legendPos != null ? { legendPos: op.legendPos } : {}),
      ...(op.dataLabels != null ? { dataLabels: op.dataLabels } : {}),
      ...(op.gridlines != null ? { gridlines: op.gridlines } : {}),
      ...(op.catAxisTitle != null ? { catAxisTitle: op.catAxisTitle } : {}),
      ...(op.valAxisTitle != null ? { valAxisTitle: op.valAxisTitle } : {}),
      ...(op.gapWidthPct != null ? { gapWidthPct: op.gapWidthPct } : {}),
      ...(op.switchRowCol ? { switchRowCol: true } : {}),
      ...(op.pointColors ? { pointColors: op.pointColors } : {}),
    }
    if (!editChartElement(s.opened, op.slideIndex, op.sourceId, patch)) {
      s.undoStack.pop()
      return null
    }
    const rebuilt = rebuildSlideWithReparse(s, op.slideIndex)
    const newId = s.opened.deck.slides[op.slideIndex]?.elements[elIdx]?.id ?? null
    return { slide: rebuilt!, sourceId: newId }
  },
  getChartColorSchemes: async () => chartColorSchemes(),
  getChartData: async (slideIndex, sourceId) => {
    const s = getSession()
    const slide = s?.opened.deck.slides[slideIndex]
    return s && slide ? getChartElementData(slide, sourceId) : null
  },

  // ── export / print (ponytail: server-side render deferred) ──
  pickExportDir: async () => null,
  exportImages: async () => ({ ok: false, error: 'image export not available in web build' }),
  pickExportPdfPath: async () => null,
  exportPdf: async () => ({ ok: false, error: 'pdf export not available in web build' }),
  printSlides: async () => {
    window.print()
    return { ok: false }
  },

  save: async () => {
    const s = getSession()
    if (!s) return { ok: false, error: 'no presentation' }
    const bytes = await savePptx(s.opened)
    let path = s.path
    if (path) {
      if (!(await putBlob(path, bytes))) return { ok: false, error: 'save failed' }
    } else {
      const id = await createDoc('演示文稿.pptx', bytes)
      if (!id) return { ok: false, error: 'save failed' }
      s.path = path = id
    }
    commitSaved(s.opened)
    s.metaDirty = false
    return { ok: true, path, slides: buildAllRenderSlides(s.opened, s.fitWidthPx) }
  },
  saveAs: async (defaultName) => {
    const s = getSession()
    if (!s) return { ok: false, error: 'no presentation' }
    const bytes = await savePptx(s.opened)
    const id = await createDoc(defaultName, bytes)
    if (!id) return { ok: false, error: 'save failed' }
    s.path = id
    commitSaved(s.opened)
    s.metaDirty = false
    return { ok: true, path: id, slides: buildAllRenderSlides(s.opened, s.fitWidthPx) }
  },
  onCloseSaveRequest: unsub,
  reportCloseSaveResult: noop,
  setAutoSavePref: noop,
  isDirty: async () => {
    const s = getSession()
    if (!s) return false
    return (
      !!s.metaDirty ||
      s.opened.deck.slides.some(
        (sl) =>
          sl.structureDirty ||
          sl.elements.some((el) => el.dirty || el.dirtyTransform || el.dirtyFill || el.dirtyStroke || el.dirtySrcRect || el.dirtyPPr),
      )
    )
  },
  getRecentFiles: async () =>
    authFetch('/documents')
      .then((r) => (r.ok ? r.json() : []))
      .then((docs: Array<{ id: string }>) => docs.map((d) => d.id))
      .catch(() => []),
  onMenuCommand: unsub,
  onOpened: unsub,
  onRenamed: unsub,

  getAiSettings: async () => ({ provider: 'backend' }) as unknown as Awaited<ReturnType<SlidesApi['getAiSettings']>>,
  setAiSettings: async () => {},
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
  aiGskStatus: async () => ({ loggedIn: true }) as unknown as Awaited<ReturnType<SlidesApi['aiGskStatus']>>,
  aiGskLogin: async () => {},
  webSearch: async (query, maxResults) =>
    getJson(`/ai/web-search?query=${encodeURIComponent(query)}&max=${maxResults ?? 6}`).catch(() => ({
      results: [],
      method: 'error',
    })) as ReturnType<SlidesApi['webSearch']>,
  imageSearch: async (query, maxResults) =>
    getJson(`/ai/image-search?query=${encodeURIComponent(query)}&max=${maxResults ?? 8}`).catch(() => ({
      images: [],
      method: 'error',
    })) as ReturnType<SlidesApi['imageSearch']>,

  // 换图 / AI image insert: fetch bytes through the backend proxy, then addPicture
  insertImageUrl: async (op) => {
    const s = getSession()
    if (!s) return null
    const slide = s.opened.deck.slides[op.slideIndex]
    if (!slide) return null
    const img =
      dataUrlParts(op.url) ??
      (await getJson<{ base64: string; mime: string }>(
        `/ai/fetch-image?url=${encodeURIComponent(op.url)}`,
      ).catch(() => null))
    if (!img) return null
    const ext = img.mime.includes('png')
      ? 'png'
      : img.mime.includes('gif')
        ? 'gif'
        : img.mime.includes('webp')
          ? 'webp'
          : 'jpg'
    pushHistory(s)
    const { toEmu } = fitCtx(s, op.fitWidthPx)
    const el = addPicture(s.opened, slide, {
      bytes: base64ToBytes(img.base64),
      ext,
      offset: { x: toEmu(op.xPx), y: toEmu(op.yPx), cx: Math.max(1, toEmu(op.wPx)), cy: Math.max(1, toEmu(op.hPx)) },
    })
    if (!el) {
      s.undoStack.pop()
      return null
    }
    return { slide: rebuildSlide(s, op.slideIndex)!, sourceId: el.id }
  },

  // AI Office media dropped in the web build
  generateImage: async () => ({ error: 'image generation not available' }),
  analyzeMedia: async () => ({ error: 'media analysis not available' }),
  gskStatus: async () => ({ available: false }),
  onAiStream: (handler) => {
    streamListeners.add(handler as StreamListener)
    return () => streamListeners.delete(handler as StreamListener)
  },

  // style templates dropped
  saveStyleSidecar: async () => ({ ok: false }),
  saveStyleTemplate: async () => ({ ok: false, error: 'not available' }),
  listStyleTemplates: async () => [],
  loadStyleTemplate: async () => ({ ok: false, error: 'not available' }),

  // ── master edit view (ponytail: deferred; return null so the UI stays out of master mode) ──
  masterEnter: async () => null,
  masterOpen: async () => null,
  masterClose: async () => null,
  masterEditText: async () => null,
  masterEditTransform: async () => null,
  masterEditFill: async () => null,
  masterEditStroke: async () => null,
  masterDeleteElement: async () => null,

  // ── presenter multi-screen (ponytail: single-window web build, all no-op) ──
  presenterStart: async () => ({ audience: false }),
  presenterSync: noop,
  presenterInk: noop,
  presenterSwap: async () => false,
  presenterEnd: async () => {},
  audienceReady: async () => null,
  audienceNav: noop,
  onShowSync: unsub,
  onShowInk: unsub,
  onAudienceNav: unsub,
}

// ── attachment bridge (subset shared with docs; stubbed in web M4) ──────────
const desktop: DesktopFilesApi = {
  pickAttachments: async () => null,
  addAttachmentPaths: async () => ({ accepted: [], rejected: [] }),
  addPastedImage: async () => ({ accepted: [], rejected: [] }),
  readAttachment: async () => ({ ok: false, error: 'not supported in web build' }),
  readAttachmentImage: async () => ({ ok: false, error: 'not supported in web build' }),
  getPathForFile: (file) => file.name,
}

// ── project/chat persistence (backend: /projects) — copied from docs adapter ──
async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const r = await authFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return (await r.json()) as T
}
async function apiJson<T>(path: string, init: RequestInit): Promise<T> {
  const r = await authFetch(path, init)
  return (await r.json()) as T
}
const projectApi: ProjectApi = {
  resolveChat: (args) =>
    apiPost('/projects/resolve-chat', { filePath: args.filePath, tempChatId: args.tempChatId }),
  appendChat: async (args) => {
    await apiPost('/projects/append-chat', args)
  },
  loadChat: (args) =>
    apiJson(`/projects/chat?chatId=${encodeURIComponent(args.chatId)}&limit=${args.limit ?? 200}`, {}),
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
  moveFile: async (args) => {
    await apiPost('/projects/move-file', args)
  },
  getTimeline: (args) => apiJson(`/projects/${args.projectId}/timeline?limit=${args.limit ?? 50}`, {}),
}

declare global {
  interface Window {
    slidesApi: SlidesApi
    desktop: DesktopFilesApi
    projectApi: ProjectApi
  }
}

window.slidesApi = slidesApi
window.desktop = desktop
window.projectApi = projectApi

// suppress unused-import type-only false positives (kept for readability of the port)
export type { OpenedPptx, GroupElement, SectionInfo }
