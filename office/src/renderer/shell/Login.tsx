import { useState } from 'react'
import { authenticate } from '../web-adapter'
import { BrandMark } from './BrandMark'

const FEATURES = [
  { icon: '⚡', title: '高效办公', sub: '智能文档 · 一键生成' },
  { icon: '👥', title: '团队协作', sub: '多人共享 · 实时同步' },
  { icon: '🛡', title: '安全可靠', sub: '数据加密 · 隐私保护' },
]

export function Login({ onAuthed }: { onAuthed: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState(() => localStorage.getItem('aioffice_email') ?? '')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [remember, setRemember] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    const r = await authenticate(email.trim(), password, mode)
    setBusy(false)
    if (r.ok) {
      if (remember) localStorage.setItem('aioffice_email', email.trim())
      else localStorage.removeItem('aioffice_email')
      onAuthed()
    } else setError(r.error ?? '登录失败')
  }

  return (
    <div className="login-screen">
      <div className="login-hero">
        <div className="login-hero-brand">
          <BrandMark size={46} />
          <span className="brand-word">AI Office</span>
        </div>
        <div className="login-hero-tag">智能化办公 · 高效协作 · 让工作更简单</div>
        <div className="login-hero-art" aria-hidden="true">
          <div className="login-art-card c1" />
          <div className="login-art-card c2" />
          <div className="login-art-card c3" />
        </div>
      </div>

      <form className="login-card" onSubmit={submit}>
        <div className="login-card-brand">
          <BrandMark size={30} />
          <span className="brand-word">AI Office</span>
        </div>
        <div className="login-sub">
          {mode === 'login' ? '登录你的工作台，开启高效办公之旅' : '创建新账号，即刻开始'}
        </div>

        <label className="login-field">
          <span className="login-ico">✉</span>
          <input
            type="email"
            placeholder="邮箱"
            value={email}
            autoComplete="email"
            required
            onChange={(e) => setEmail(e.target.value)}
          />
          {emailOk && <span className="login-ok">✓</span>}
        </label>

        <label className="login-field">
          <span className="login-ico">🔒</span>
          <input
            type={showPw ? 'text' : 'password'}
            placeholder="密码"
            value={password}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
            minLength={6}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button type="button" className="login-eye" tabIndex={-1} onClick={() => setShowPw((s) => !s)}>
            {showPw ? '🙈' : '👁'}
          </button>
        </label>

        <label className="login-remember">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          记住我
        </label>

        {error && <div className="login-error">{error}</div>}

        <button className="login-submit" type="submit" disabled={busy}>
          {busy ? '…' : mode === 'login' ? '登 录' : '注 册'}
        </button>
        <button
          type="button"
          className="login-switch"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError('')
          }}
        >
          {mode === 'login' ? '还没有账号？立即注册' : '已有账号？返回登录'}
        </button>
      </form>

      <div className="login-features">
        {FEATURES.map((f) => (
          <div className="login-feature" key={f.title}>
            <div className="login-feature-ico">{f.icon}</div>
            <div>
              <div className="login-feature-title">{f.title}</div>
              <div className="login-feature-sub">{f.sub}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
