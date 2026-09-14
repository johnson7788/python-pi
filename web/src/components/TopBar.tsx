/**
 * The top strip of every page: where you are, and who you are.
 *
 * In single mode there is no account to show and no password to change, so the user menu
 * collapses to a "personal mode" label — the same bundle serving both deployments should
 * not advertise a login that does not exist.
 */

import { useEffect, useRef, useState } from 'react'

import { navigate, routeSection } from '../lib/router'
import type { AuthApi } from '../lib/useAuth'

interface Tab {
  path: string
  label: string
}

export function TopBar({
  auth,
  route,
  streaming,
}: {
  auth: AuthApi
  route: string
  streaming: boolean
}) {
  const section = routeSection(route)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!menuOpen) return
    const close = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    window.document.addEventListener('mousedown', close)
    return () => window.document.removeEventListener('mousedown', close)
  }, [menuOpen])

  const tabs: Tab[] = [
    { path: '/', label: '会话' },
    { path: '/files', label: '文件' },
    { path: '/usage', label: '用量' },
  ]
  if (auth.user?.isAdmin) tabs.push({ path: '/admin/members', label: '管理' })

  return (
    <div className="topnav">
      <nav className="topnav__tabs" aria-label="主导航">
        {tabs.map((tab) => {
          const active = tab.path === '/admin/members' ? section === '/admin' : section === tab.path
          return (
            <button
              key={tab.path}
              type="button"
              className={`topnav__tab${active ? ' is-active' : ''}`}
              aria-current={active ? 'page' : undefined}
              onClick={() => navigate(tab.path)}
            >
              {tab.label}
            </button>
          )
        })}
      </nav>

      <div className="topnav__right">
        {streaming && section !== '/' ? (
          <button type="button" className="chip chip--live" onClick={() => navigate('/')}>
            <span className="dot" aria-hidden="true" />
            正在生成，回到会话
          </button>
        ) : null}

        {auth.user ? (
          <div className="usermenu" ref={menuRef}>
            <button
              type="button"
              className="usermenu__button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <span className="usermenu__name">{auth.user.displayName || auth.user.username}</span>
              <span className={`chip chip--role chip--${auth.user.role}`}>{roleLabel(auth.user.role)}</span>
            </button>
            {menuOpen ? (
              <div className="usermenu__popup" role="menu">
                <div className="usermenu__meta">
                  <div>{auth.user.username}</div>
                  <div className="usermenu__uid">{auth.user.uid.slice(0, 12)}</div>
                </div>
                <button
                  type="button"
                  role="menuitem"
                  className="usermenu__item"
                  onClick={() => {
                    setMenuOpen(false)
                    auth.setNotice(null)
                    navigate('/account')
                  }}
                >
                  修改密码
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="usermenu__item usermenu__item--danger"
                  onClick={() => {
                    setMenuOpen(false)
                    void auth.logout()
                  }}
                >
                  退出登录
                </button>
              </div>
            ) : null}
          </div>
        ) : (
          <span className="topnav__mode" title="未启用账号（单人模式）">
            个人模式
          </span>
        )}
      </div>
    </div>
  )
}

function roleLabel(role: string): string {
  if (role === 'superadmin') return '超级管理员'
  if (role === 'admin') return '管理员'
  return '成员'
}
