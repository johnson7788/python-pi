/**
 * A hash router in thirty lines.
 *
 * The app has six pages and no need for nested routes, loaders or code splitting, so a
 * dependency would buy nothing that this does not. Hash routing specifically: the server
 * already falls back to `index.html` for any path, but a hash keeps the shareable URL
 * working identically whether the page is served by the server or by `vite dev`.
 */

import { useEffect, useState } from 'react'

function currentPath(): string {
  const raw = window.location.hash.replace(/^#/, '')
  if (!raw) return '/'
  return raw.startsWith('/') ? raw : `/${raw}`
}

export function navigate(path: string): void {
  if (currentPath() === path) return
  window.location.hash = path
}

export function useRoute(): string {
  const [path, setPath] = useState<string>(currentPath)

  useEffect(() => {
    const onChange = () => setPath(currentPath())
    window.addEventListener('hashchange', onChange)
    // A page loaded without a hash gets one, so back/forward behave predictably.
    if (!window.location.hash) window.location.hash = '/'
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  return path
}

/** The first segment of a route, for the nav's active state. */
export function routeSection(path: string): string {
  const segment = path.split('/').filter(Boolean)[0]
  return segment ? `/${segment}` : '/'
}
