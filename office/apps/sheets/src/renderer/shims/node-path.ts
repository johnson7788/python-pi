/**
 * Browser shim for the handful of node:path helpers the xlsx gateway uses for
 * OPC part-name math (always POSIX '/' separators inside the zip). Minimal
 * POSIX implementations — no Windows separators, no path resolution against a
 * real filesystem.
 */
export function join(...parts: string[]): string {
  return parts
    .filter((p) => p.length > 0)
    .join('/')
    .replace(/\/+/g, '/')
}

export function dirname(p: string): string {
  const i = p.replace(/\/+$/, '').lastIndexOf('/')
  return i <= 0 ? (i === 0 ? '/' : '.') : p.slice(0, i)
}

export function basename(p: string, ext?: string): string {
  const base = p.replace(/\/+$/, '').split('/').pop() ?? ''
  return ext && base.endsWith(ext) ? base.slice(0, -ext.length) : base
}

export function extname(p: string): string {
  const base = basename(p)
  const i = base.lastIndexOf('.')
  return i <= 0 ? '' : base.slice(i)
}

export default { join, dirname, basename, extname }
