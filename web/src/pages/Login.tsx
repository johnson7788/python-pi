/**
 * The login page, and the forced password change that follows a first sign-in.
 *
 * Two deliberate omissions:
 *
 * * **No self-registration.** Accounts are created by an administrator, so there is no
 *   sign-up link to offer. The page says so instead of leaving a dead end.
 * * **No "forgot password".** There is no mail delivery in this deployment; the recovery
 *   path is an administrator resetting the password, which the hint states plainly.
 */

import { useState } from 'react'

import { Field } from '../components/Page'
import type { AuthApi } from '../lib/useAuth'

export function Login({ auth }: { auth: AuthApi }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setLocalError(null)
    try {
      if (mode === 'register') {
        if (password !== confirm) {
          setLocalError('两次输入的密码不一致')
          return
        }
        if (password.length < 8) {
          setLocalError('密码至少 8 个字符')
          return
        }
        await auth.register(username.trim(), password, displayName.trim())
      } else {
        await auth.login(username.trim(), password)
      }
    } catch {
      // `auth.error` carries the message; the form just stops being busy.
    } finally {
      setBusy(false)
    }
  }

  const switchMode = (next: 'login' | 'register') => {
    setMode(next)
    setLocalError(null)
    auth.setNotice(null)
  }

  return (
    <div className="auth">
      <form className="auth__panel" onSubmit={submit}>
        <div className="auth__brand">
          <span className="auth__logo" aria-hidden="true">
            π
          </span>
          <div>
            <h1 className="auth__title">Pi 编码助手</h1>
            <p className="auth__subtitle">{mode === 'register' ? '注册团队账号' : '团队自托管版'}</p>
          </div>
        </div>

        <div className="auth__switch" role="tablist" aria-label="登录或注册">
          <button type="button" className={mode === 'login' ? 'is-active' : ''} onClick={() => switchMode('login')}>登录</button>
          <button type="button" className={mode === 'register' ? 'is-active' : ''} onClick={() => switchMode('register')}>注册</button>
        </div>

        {mode === 'register' ? (
          <Field label="显示名" hint="会展示在侧栏，可留空使用用户名">
            <input
              className="input"
              type="text"
              value={displayName}
              autoComplete="name"
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
        ) : null}

        <Field label="账号">
          <input
            className="input"
            type="text"
            value={username}
            autoComplete="username"
            autoFocus
            onChange={(event) => setUsername(event.target.value)}
          />
        </Field>

        <Field label={mode === 'register' ? '密码' : '密码'} hint={mode === 'register' ? '至少 8 个字符' : undefined}>
          <input
            className="input"
            type="password"
            value={password}
            autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>

        {mode === 'register' ? (
          <Field label="再次输入密码">
            <input
              className="input"
              type="password"
              value={confirm}
              autoComplete="new-password"
              onChange={(event) => setConfirm(event.target.value)}
            />
          </Field>
        ) : null}

        {localError ? <div className="note note--error" role="alert">{localError}</div> : null}
        {auth.error ? (
          <div className="note note--error" role="alert">
            {auth.error}
          </div>
        ) : null}
        {auth.notice ? <div className="note note--ok">{auth.notice}</div> : null}

        <button className="button button--primary button--block" type="submit" disabled={busy}>
          {busy ? (mode === 'register' ? '注册中…' : '登录中…') : mode === 'register' ? '注册并登录' : '登录'}
        </button>

        <p className="auth__hint">
          {mode === 'register'
            ? '当前部署开放自助注册；注册后可直接使用团队工作台。'
            : '没有账号？切换到「注册」创建；忘记密码请联系管理员重置。'}
        </p>
      </form>
    </div>
  )
}

export function ChangePassword({
  auth,
  forced,
  inline = false,
}: {
  auth: AuthApi
  forced: boolean
  /** Rendered inside the app shell (from the user menu) rather than full-screen. */
  inline?: boolean
}) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (next !== confirm) {
      setError('两次输入的新密码不一致')
      return
    }
    if (next.length < 8) {
      setError('新密码至少 8 个字符')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await auth.changePassword(current, next)
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '修改失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={inline ? 'page' : 'auth'}>
      <form className={inline ? 'auth__panel auth__panel--inline' : 'auth__panel'} onSubmit={submit}>
        <h1 className={inline ? 'page__title' : 'auth__title'}>
          {forced ? '请先修改密码' : '修改密码'}
        </h1>
        <p className="auth__subtitle">
          {forced
            ? '这是管理员分配的初始密码，修改后才能开始使用。'
            : '修改后，其它设备上的登录会失效。'}
        </p>

        <Field label="当前密码">
          <input
            className="input"
            type="password"
            value={current}
            autoComplete="current-password"
            autoFocus
            onChange={(event) => setCurrent(event.target.value)}
          />
        </Field>
        <Field label="新密码" hint="至少 8 个字符">
          <input
            className="input"
            type="password"
            value={next}
            autoComplete="new-password"
            onChange={(event) => setNext(event.target.value)}
          />
        </Field>
        <Field label="再次输入新密码">
          <input
            className="input"
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(event) => setConfirm(event.target.value)}
          />
        </Field>

        <ErrorOrNotice error={error ?? auth.error} notice={auth.notice} />

        <button className="button button--primary button--block" type="submit" disabled={busy}>
          {busy ? '提交中…' : '修改密码'}
        </button>

        {forced ? (
          <button className="link-button auth__signout" type="button" onClick={() => void auth.logout()}>
            退出登录
          </button>
        ) : null}
      </form>
    </div>
  )
}

function ErrorOrNotice({ error, notice }: { error: string | null; notice: string | null }) {
  return (
    <>
      {error ? (
        <div className="note note--error" role="alert">
          {error}
        </div>
      ) : null}
      {notice ? <div className="note note--ok">{notice}</div> : null}
    </>
  )
}
