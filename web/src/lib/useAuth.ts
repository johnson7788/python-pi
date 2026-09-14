/**
 * Who is logged in, and what that means for the rest of the UI.
 *
 * The server answers `/api/auth/me` in both modes, which is what makes one bundle serve
 * two deployments:
 *
 * * `authMode: "single"` — the original personal tool. Nobody logs in, there are no
 *   accounts, and the whole app renders immediately.
 * * `authMode: "team"` — accounts exist. No session means `401`, and the UI shows the
 *   login page instead of the workspace.
 *
 * A network failure deliberately does *not* mean "team mode". Assuming a login wall when
 * the server is simply unreachable would leave a personal user staring at a login form
 * for accounts that do not exist; the socket's own 4401 close is the authoritative
 * signal that a session is actually required.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, UnauthorizedError } from './api'

export type AuthMode = 'single' | 'team'
export type AuthStatus = 'checking' | 'ready'

export interface AuthUser {
  uid: string
  username: string
  displayName: string
  role: string
  isAdmin: boolean
  mustChangePassword: boolean
  monthlyTokenQuota: number | null
  monthlyCostQuota: number | null
}

interface MeResponse {
  authMode?: string
  user?: AuthUser | null
}

export interface AuthApi {
  status: AuthStatus
  authMode: AuthMode
  user: AuthUser | null
  /** True when the app may render: either no accounts exist, or a session is live. */
  authenticated: boolean
  /** True when a password change is required before anything else is usable. */
  mustChangePassword: boolean
  error: string | null
  notice: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
  refresh: () => Promise<void>
  setNotice: (message: string | null) => void
  /** The single funnel every 401 and socket 4401 goes through. */
  onUnauthorized: () => void
}

export function useAuth(): AuthApi {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [authMode, setAuthMode] = useState<AuthMode>('single')
  const [user, setUser] = useState<AuthUser | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // Guards against a late response from a previous load overwriting a newer one.
  const requestRef = useRef(0)

  const load = useCallback(async () => {
    const ticket = ++requestRef.current
    try {
      const payload = await api.get<MeResponse>('/api/auth/me')
      if (ticket !== requestRef.current) return
      const mode: AuthMode = payload?.authMode === 'team' ? 'team' : 'single'
      setAuthMode(mode)
      setUser(payload?.user ?? null)
    } catch (failure) {
      if (ticket !== requestRef.current) return
      if (failure instanceof UnauthorizedError) {
        // No session on a server that has accounts.
        setAuthMode('team')
        setUser(null)
      } else {
        // Unreachable server: let the app render and let the socket report the truth.
        setAuthMode('single')
        setUser(null)
      }
    } finally {
      if (ticket === requestRef.current) setStatus('ready')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const login = useCallback(async (username: string, password: string) => {
    setError(null)
    try {
      const payload = await api.post<{ user: AuthUser }>('/api/auth/login', {
        username,
        password,
      })
      setAuthMode('team')
      setUser(payload.user)
      setNotice(null)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '登录失败')
      throw failure
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/auth/logout')
    } catch {
      // A failed logout still clears the local view: the cookie is either gone or the
      // server is unreachable, and neither is a reason to keep showing someone's session.
    }
    setUser(null)
    setAuthMode('team')
    setNotice(null)
  }, [])

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    setError(null)
    const payload = await api.post<{ user: AuthUser }>('/api/auth/password', {
      currentPassword,
      newPassword,
    })
    setUser(payload.user)
    setNotice('密码已更新，其它设备上的登录已失效。')
  }, [])

  const onUnauthorized = useCallback(() => {
    setUser(null)
    setAuthMode('team')
  }, [])

  return {
    status,
    authMode,
    user,
    authenticated: authMode === 'single' || user !== null,
    mustChangePassword: Boolean(user?.mustChangePassword),
    error,
    notice,
    login,
    logout,
    changePassword,
    refresh: load,
    setNotice,
    onUnauthorized,
  }
}
