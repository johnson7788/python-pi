/**
 * The top strip of every page: where you are, and who you are.
 *
 * In single mode there is no account to show and no password to change, so the user menu
 * collapses to a "personal mode" label — the same bundle serving both deployments should
 * not advertise a login that does not exist.
 *
 * Two pieces of chrome live here because the toolbar is the only thing that spans every
 * page: the appearance toggle, and (on narrow screens only) the button that slides the
 * sidebar in. The sidebar itself is off-canvas below 900px, so without that button it
 * would be unreachable.
 */

import { useEffect, useRef, useState } from 'react'

import { navigate, routeSection } from '../lib/router'
import { useTheme } from '../lib/useTheme'
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
  const [navOpen, setNavOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const theme = useTheme()

  useEffect(() => {
    if (!menuOpen) return
    const close = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    window.document.addEventListener('mousedown', close)
    return () => window.document.removeEventListener('mousedown', close)
  }, [menuOpen])

  // The drawer is a body class rather than component state: it has to move the sidebar,
  // which is rendered by the shell, not by this component.
  useEffect(() => {
    document.body.classList.toggle('nav-open', navOpen)
    return () => document.body.classList.remove('nav-open')
  }, [navOpen])

  const tabs: Tab[] = [
    { path: '/', label: '会话' },
    { path: '/files', label: '文件' },
    { path: '/usage', label: '用量' },
  ]
  if (auth.user?.isAdmin) tabs.push({ path: '/admin/members', label: '管理' })

  const appearance = theme.isDark ? '切换到浅色外观' : '切换到深色外观'

  return (
    <>
      <div className="topnav">
        <button
          type="button"
          className="icon-button topnav__menu"
          aria-label="打开目录"
          aria-expanded={navOpen}
          onClick={() => setNavOpen((open) => !open)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
          </svg>
        </button>

        <nav className="topnav__tabs" aria-label="主导航">
          {tabs.map((tab) => {
            const active = tab.path === '/admin/members' ? section === '/admin' : section === tab.path
            return (
              <button
                key={tab.path}
                type="button"
                className={`topnav__tab${active ? ' is-active' : ''}`}
                aria-current={active ? 'page' : undefined}
                onClick={() => {
                  navigate(tab.path)
                  setNavOpen(false)
                }}
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

          <button
            type="button"
            className="icon-button"
            onClick={theme.toggle}
            aria-label={appearance}
            title={appearance}
          >
            {theme.isDark ? (
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="8" cy="8" r="2.6" />
                <path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1" />
              </svg>
            ) : (
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M13 9.6A5.6 5.6 0 0 1 6.4 3a5.6 5.6 0 1 0 6.6 6.6z" />
              </svg>
            )}
          </button>

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

      {navOpen ? <div className="scrim" onClick={() => setNavOpen(false)} /> : null}
    </>
  )
}

function roleLabel(role: string): string {
  if (role === 'superadmin') return '超级管理员'
  if (role === 'admin') return '管理员'
  return '成员'
}
