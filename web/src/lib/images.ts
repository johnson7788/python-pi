/**
 * Chat image attachments, prepared in the browser before they are sent.
 *
 * The server accepts inline base64 only, and a phone screenshot easily exceeds
 * 4 MB.  Downscaling here keeps a prompt small enough for the WebSocket frame
 * limit and for the model's own image budget, while still leaving text legible
 * (1568px is the long edge most vision APIs recommend).
 */

export interface DraftImage {
  id: string
  name: string
  mimeType: string
  /** base64 payload without a `data:` prefix. */
  data: string
  /** Data URL used for the thumbnail; never sent to the server. */
  dataUrl: string
  bytes: number
}

/** More than a handful of images in one turn is a bug, not a use case. */
export const MAX_ATTACHMENTS = 4

const MAX_DIMENSION = 1568
const JPEG_QUALITY = 0.85

let sequence = 0

function nextId(): string {
  sequence += 1
  return `img-${Date.now()}-${sequence}`
}

function toBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunkSize = 0x8000
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize))
  }
  return btoa(binary)
}

async function loadSource(file: File): Promise<ImageBitmap | HTMLImageElement> {
  if (typeof createImageBitmap === 'function') {
    try {
      return await createImageBitmap(file)
    } catch {
      // Older Safari cannot decode every type through createImageBitmap; fall back.
    }
  }
  const url = URL.createObjectURL(file)
  try {
    return await new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image()
      image.onload = () => resolve(image)
      image.onerror = () => reject(new Error('无法读取图片'))
      image.src = url
    })
  } finally {
    URL.revokeObjectURL(url)
  }
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY))
}

/**
 * Downscale + re-encode one image file.  Returns `null` for a non-image file so
 * the caller can ignore a stray paste without an error dialog.
 */
export async function prepareImageFile(file: File): Promise<DraftImage | null> {
  if (!file.type.startsWith('image/')) return null

  const source = await loadSource(file)
  const width = source.width
  const height = source.height
  if (!width || !height) throw new Error('图片尺寸无效')

  const scale = Math.min(1, MAX_DIMENSION / Math.max(width, height))
  const targetWidth = Math.max(1, Math.round(width * scale))
  const targetHeight = Math.max(1, Math.round(height * scale))

  const canvas = document.createElement('canvas')
  canvas.width = targetWidth
  canvas.height = targetHeight
  const context = canvas.getContext('2d')
  if (!context) throw new Error('浏览器不支持 canvas')
  // Flatten transparency onto white: JPEG has no alpha, and a transparent PNG
  // would otherwise turn black and lose legibility for the model.
  context.fillStyle = '#ffffff'
  context.fillRect(0, 0, targetWidth, targetHeight)
  context.drawImage(source as CanvasImageSource, 0, 0, targetWidth, targetHeight)
  if ('close' in source) source.close()

  const blob = await canvasToBlob(canvas)
  if (!blob) throw new Error('图片编码失败')
  const bytes = new Uint8Array(await blob.arrayBuffer())
  const data = toBase64(bytes)
  return {
    id: nextId(),
    name: file.name || 'image',
    mimeType: 'image/jpeg',
    data,
    dataUrl: `data:image/jpeg;base64,${data}`,
    bytes: bytes.length,
  }
}
