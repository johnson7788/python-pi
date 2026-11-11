/**
 * Auth gate + the only product surface: the workbench.
 *
 * The old conversation/usage/admin shells were removed; login, forced password change and
 * the account page remain because they are part of the auth boundary, not product pages.
 */

import { useEffect } from 'react'

import { useAuth } from './lib/useAuth'
import { useRoute, routeSection } from './lib/router'
import { ChangePassword, Login } from './pages/Login'
import { Workbench } from './workbench/Workbench'

export function App() {
  const auth = useAuth()
  const route = useRoute()
  const section = routeSection(route)

  useEffect(() => {
    if (!auth.authenticated || auth.mustChangePassword) return
    if (section !== '/workbench' && section !== '/account') {
      window.location.hash = '/workbench'
    }
  }, [auth.authenticated, auth.mustChangePassword, section])

  if (auth.status === 'checking') {
    return (
      <div className="auth">
        <div className="auth__panel auth__panel--plain">正在检查登录状态…</div>
      </div>
    )
  }
  if (!auth.authenticated) return <Login auth={auth} />
  if (auth.mustChangePassword) return <ChangePassword auth={auth} forced />

  if (section === '/account' && auth.authMode === 'team') {
    return (
      <>
        <a
          href="#/workbench"
          style={{
            position: 'fixed',
            left: 24,
            top: 20,
            zIndex: 10,
            padding: '6px 10px',
            borderRadius: 6,
            background: 'var(--card)',
            border: '1px solid var(--line)',
            fontSize: 13,
            color: 'var(--primary-text)',
          }}
        >
          ← 返回工作台
        </a>
        <ChangePassword auth={auth} forced={false} />
      </>
    )
  }

  return <Workbench auth={auth} route={route} />
}
