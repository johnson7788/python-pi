import { useState } from 'react'
import { App } from '../App'
import { hasToken } from '../web-adapter'
import { Home } from './Home'
import { Login } from './Login'
import { ShareView } from './ShareView'

// Public read-only share landing — bypasses the login gate entirely.
const shareToken = new URLSearchParams(location.search).get('share')

/**
 * Top-level SaaS shell: Login gate → chat-first Home → docs editor.
 * The editor is the existing monolithic docs App, mounted full-screen. A nav
 * nonce bumps on every open so App remounts and re-runs its boot (consuming the
 * pending open/prompt the Home stashed in sessionStorage).
 */
export function Shell() {
  const [authed, setAuthed] = useState(hasToken())
  const [view, setView] = useState<'home' | 'editor'>('home')
  const [navNonce, setNavNonce] = useState(0)

  if (shareToken) return <ShareView token={shareToken} />

  if (!authed) return <Login onAuthed={() => setAuthed(true)} />

  if (view === 'home')
    return (
      <Home
        onOpenEditor={() => {
          setNavNonce((n) => n + 1)
          setView('editor')
        }}
      />
    )

  return (
    <>
      <button className="shell-home-btn" onClick={() => setView('home')}>
        ← 主页
      </button>
      <App key={navNonce} />
    </>
  )
}
