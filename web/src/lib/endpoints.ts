/**
 * Where the API and the command socket live.
 *
 * Both default to the page's own origin, which is what lets the same bundle be served by
 * the server it talks to. `VITE_*` overrides exist for `npm run dev`, where Vite serves
 * the SPA on a different port than the server.
 *
 * Kept in its own module so `api.ts` can build request URLs without importing the agent
 * hook (which would be a cycle: the hook uses the API client for its REST calls).
 */

export function resolveWsUrl(
  workspace?: string,
  appId?: string,
  modeId?: string,
): string {
  const configured = import.meta.env.VITE_WS_URL
  const base =
    typeof configured === 'string' && configured.length > 0
      ? configured
      : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/ws`
  const params = new URLSearchParams()
  if (workspace) params.set('workspace', workspace)
  if (appId) params.set('app', appId)
  if (modeId) params.set('mode', modeId)
  if (![...params].length) return base
  const separator = base.includes('?') ? '&' : '?'
  return `${base}${separator}${params.toString()}`
}

export function resolveApiBase(): string {
  const configured = import.meta.env.VITE_API_BASE
  return typeof configured === 'string' && configured.length > 0 ? configured : ''
}
