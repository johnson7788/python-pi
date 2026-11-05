/**
 * Render a live DOM subtree to a small PNG (base64, no data: prefix) for the
 * Home doc-card thumbnail. Uses the native SVG <foreignObject> trick (no deps):
 * clone the node into an SVG, inline the page's same-origin CSS so it renders
 * styled, rasterize via an <img>, then downscale onto a canvas.
 *
 * Best-effort: any failure (tainted canvas from cross-origin images, unloadable
 * fonts, serialize error) resolves to null — the caller fires-and-forgets and
 * Home falls back to its gradient type-tile.
 */
export async function domToThumbPng(el: HTMLElement, maxWidth = 512): Promise<string | null> {
  try {
    const rect = el.getBoundingClientRect()
    const w = Math.max(1, Math.round(rect.width))
    // clamp the captured height to a card-ish aspect so a long doc doesn't
    // produce a huge canvas; the overflow is clipped by the foreignObject
    const h = Math.max(1, Math.min(Math.round(rect.height), Math.round(w * 1.4)))

    // inline same-origin stylesheet text so the clone renders styled
    let css = ''
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules)) css += rule.cssText + '\n'
      } catch {
        /* cross-origin sheet: .cssRules throws — skip it */
      }
    }

    const clone = el.cloneNode(true) as HTMLElement
    // drop external images so toDataURL doesn't taint the canvas (data: URLs stay)
    clone.querySelectorAll('img').forEach((img) => {
      if (!(img.getAttribute('src') ?? '').startsWith('data:')) img.remove()
    })

    const xhtml = new XMLSerializer().serializeToString(clone)
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">` +
      `<foreignObject width="100%" height="100%">` +
      `<div xmlns="http://www.w3.org/1999/xhtml"><style>${css}</style>${xhtml}</div>` +
      `</foreignObject></svg>`

    const img = new Image()
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg)
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve()
      img.onerror = () => reject(new Error('svg render failed'))
    })

    const scale = Math.min(1, maxWidth / w)
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(w * scale))
    canvas.height = Math.max(1, Math.round(h * scale))
    const ctx = canvas.getContext('2d')
    if (!ctx) return null
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
    return canvas.toDataURL('image/png').split(',')[1] ?? null
  } catch {
    return null
  }
}
