/**
 * The administrator console: members, deployment-wide usage, and the audit log.
 *
 * Three things shape this page.
 *
 * **Lockout is prevented server-side, and the UI explains it.** There is no confirm dialog
 * here for demoting yourself or disabling the last administrator; the server refuses with a
 * reason and the page shows it. Duplicating the rule in the browser would eventually drift
 * from the one that actually protects the deployment.
 *
 * **The audit log is the governance record.** Each row is one tool call: what was attempted,
 * what the policy decided, and whether it failed. It is the only place to answer "who ran
 * that" after the fact.
 *
 * **Quotas are edited in place, with an explicit empty field meaning unlimited.** Sending
 * `null` is how a quota is cleared, which is different from "leave it alone".
 */

import { useCallback, useEffect, useState } from 'react'

import { api } from '../lib/api'
import { useRoute } from '../lib/router'
import {
  Card,
  Empty,
  ErrorNote,
  Field,
  PageHeader,
  formatCost,
  formatCount,
  formatTime,
} from '../components/Page'

interface Member {
  uid: string
  username: string
  displayName: string
  role: string
  status: string
  mustChangePassword: boolean
  monthlyTokenQuota: number | null
  monthlyCostQuota: number | null
  createdAt: string
  lastLoginAt: string | null
}

interface UsageRow {
  uid: string
  username: string
  display_name: string
  role: string
  status: string
  monthly_token_quota: number | null
  monthly_cost_quota: number | null
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cost: number
  turns: number
}

interface AuditRow {
  id: number
  ts: string
  uid: string
  session_id: string
  tool_name: string
  decision: string
  reason: string
  input_preview: string
  is_error: number
  duration_ms: number | null
}

function roleLabel(role: string): string {
  if (role === 'superadmin') return '超级管理员'
  if (role === 'admin') return '管理员'
  return '成员'
}

function decisionLabel(decision: string): string {
  switch (decision) {
    case 'allow':
      return '放行'
    case 'approved':
      return '已批准'
    case 'denied':
      return '用户拒绝'
    case 'blocked':
      return '策略拦截'
    default:
      return decision
  }
}

const TABS = [
  { path: '/admin/members', label: '成员' },
  { path: '/admin/usage', label: '全局用量' },
  { path: '/admin/audit', label: '审计日志' },
]

export function Admin() {
  const route = useRoute()
  const [error, setError] = useState<string | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [usage, setUsage] = useState<UsageRow[]>([])
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const loadMembers = useCallback(async () => {
    const payload = await api.get<{ users: Member[] }>('/api/admin/users')
    setMembers(payload.users)
  }, [])

  const loadUsage = useCallback(async () => {
    const payload = await api.get<{ users: UsageRow[] }>('/api/admin/usage')
    setUsage(payload.users)
  }, [])

  const loadAudit = useCallback(async () => {
    const payload = await api.get<{ records: AuditRow[] }>('/api/admin/audit?limit=200')
    setAudit(payload.records)
  }, [])

  const load = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      if (route.startsWith('/admin/usage')) await loadUsage()
      else if (route.startsWith('/admin/audit')) await loadAudit()
      else await loadMembers()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '读取失败')
    } finally {
      setBusy(false)
    }
  }, [route, loadMembers, loadUsage, loadAudit])

  useEffect(() => {
    void load()
  }, [load])

  const update = async (uid: string, patch: Record<string, unknown>, label: string) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await api.patch(`/api/admin/users/${uid}`, patch)
      setNotice(label)
      await loadMembers()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '修改失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="管理"
        description={`共 ${members.length || '—'} 个账号。改动立即生效，不会通知对方。`}
        actions={
          <button type="button" className="button" onClick={() => void load()} disabled={busy}>
            刷新
          </button>
        }
      />

      <nav className="subtabs" aria-label="管理分区">
        {TABS.map((tab) => (
          <SubTabButton key={tab.path} path={tab.path} label={tab.label} route={route} />
        ))}
      </nav>

      <ErrorNote message={error} />
      {notice ? <div className="note note--ok">{notice}</div> : null}

      {route.startsWith('/admin/usage') ? (
        <GlobalUsage rows={usage} />
      ) : route.startsWith('/admin/audit') ? (
        <Audit rows={audit} />
      ) : (
        <Members members={members} busy={busy} onUpdate={update} onReload={loadMembers} setError={setError} />
      )}
    </div>
  )
}

function SubTabButton({ path, label, route }: { path: string; label: string; route: string }) {
  return (
    <button
      type="button"
      className={`subtabs__tab${route.startsWith(path) ? ' is-active' : ''}`}
      onClick={() => {
        window.location.hash = path
      }}
    >
      {label}
    </button>
  )
}

function Members({
  members,
  busy,
  onUpdate,
  onReload,
  setError,
}: {
  members: Member[]
  busy: boolean
  onUpdate: (uid: string, patch: Record<string, unknown>, label: string) => Promise<void>
  onReload: () => Promise<void>
  setError: (message: string | null) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [quota, setQuota] = useState('')

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      await api.post('/api/admin/users', {
        username: username.trim(),
        password,
        displayName: displayName.trim() || username.trim(),
        role: 'user',
        monthlyTokenQuota: quota.trim() === '' ? null : Number(quota),
      })
      setUsername('')
      setPassword('')
      setDisplayName('')
      setQuota('')
      await onReload()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '创建失败')
    }
  }

  return (
    <>
      <Card title="新建账号">
        <form className="formrow" onSubmit={create}>
          <Field label="账号">
            <input
              className="input"
              value={username}
              required
              onChange={(event) => setUsername(event.target.value)}
            />
          </Field>
          <Field label="显示名">
            <input
              className="input"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
          <Field label="初始密码" hint="至少 8 位；对方首次登录后必须修改">
            <input
              className="input"
              type="text"
              value={password}
              required
              minLength={8}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <Field label="token 额度" hint="留空表示不限">
            <input
              className="input"
              type="number"
              min={0}
              value={quota}
              onChange={(event) => setQuota(event.target.value)}
            />
          </Field>
          <button className="button button--primary" type="submit" disabled={busy}>
            创建
          </button>
        </form>
      </Card>

      <Card title="成员" scroll>
        {members.length === 0 ? (
          <Empty>还没有账号。</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>账号</th>
                <th>角色</th>
                <th>状态</th>
                <th>token 额度</th>
                <th>费用额度</th>
                <th>最后登录</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <tr key={member.uid}>
                  <td>
                    <div>{member.displayName || member.username}</div>
                    <div className="mono muted">{member.username}</div>
                  </td>
                  <td>
                    <select
                      className="input input--compact"
                      value={member.role}
                      disabled={busy}
                      onChange={(event) =>
                        void onUpdate(member.uid, { role: event.target.value }, `已修改 ${member.username} 的角色`)
                      }
                    >
                      <option value="user">成员</option>
                      <option value="admin">管理员</option>
                      <option value="superadmin">超级管理员</option>
                    </select>
                  </td>
                  <td>
                    <span className={`chip ${member.status === 'active' ? 'chip--ok' : 'chip--off'}`}>
                      {member.status === 'active' ? '启用' : '已禁用'}
                    </span>
                    {member.mustChangePassword ? <span className="chip chip--warn">待改密</span> : null}
                  </td>
                  <td>
                    <QuotaInput
                      value={member.monthlyTokenQuota}
                      disabled={busy}
                      onCommit={(value) =>
                        onUpdate(member.uid, { monthlyTokenQuota: value }, `已修改 ${member.username} 的额度`)
                      }
                    />
                  </td>
                  <td>
                    <QuotaInput
                      step="0.01"
                      value={member.monthlyCostQuota}
                      disabled={busy}
                      onCommit={(value) =>
                        onUpdate(member.uid, { monthlyCostQuota: value }, `已修改 ${member.username} 的额度`)
                      }
                    />
                  </td>
                  <td className="muted">{formatTime(member.lastLoginAt)}</td>
                  <td className="table__actions">
                    <button
                      type="button"
                      className="link-button"
                      disabled={busy}
                      onClick={() => {
                        const next = window.prompt(`为 ${member.username} 设置新密码（至少 8 位）`)
                        if (!next) return
                        void onUpdate(
                          member.uid,
                          { password: next, mustChangePassword: true },
                          `已重置 ${member.username} 的密码`,
                        )
                      }}
                    >
                      重置密码
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      disabled={busy}
                      onClick={() =>
                        void onUpdate(
                          member.uid,
                          { status: member.status === 'active' ? 'disabled' : 'active' },
                          `已${member.status === 'active' ? '禁用' : '启用'} ${member.username}`,
                        )
                      }
                    >
                      {member.status === 'active' ? '禁用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

/** Committed on blur rather than per keystroke: every change revokes nothing but writes. */
function QuotaInput({
  value,
  disabled,
  step = '1',
  onCommit,
}: {
  value: number | null
  disabled: boolean
  step?: string
  onCommit: (value: number | null) => Promise<void>
}) {
  const [draft, setDraft] = useState(value === null ? '' : String(value))

  useEffect(() => {
    setDraft(value === null ? '' : String(value))
  }, [value])

  return (
    <input
      className="input input--compact"
      type="number"
      min={0}
      step={step}
      placeholder="不限"
      value={draft}
      disabled={disabled}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        const trimmed = draft.trim()
        const next = trimmed === '' ? null : Number(trimmed)
        if (next === (value ?? null)) return
        void onCommit(next)
      }}
    />
  )
}

function GlobalUsage({ rows }: { rows: UsageRow[] }) {
  if (rows.length === 0) return <Empty>本月还没有调用。</Empty>
  return (
    <Card title="本月用量（按账号）" scroll>
      <table className="table">
        <thead>
          <tr>
            <th>账号</th>
            <th>角色</th>
            <th>token</th>
            <th>额度</th>
            <th>费用</th>
            <th>调用</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const over = row.monthly_token_quota !== null && row.total_tokens >= row.monthly_token_quota
            return (
              <tr key={row.uid} className={over ? 'is-over' : undefined}>
                <td>{row.display_name || row.username}</td>
                <td>{roleLabel(row.role)}</td>
                <td>{formatCount(row.total_tokens)}</td>
                <td>{row.monthly_token_quota === null ? '不限' : formatCount(row.monthly_token_quota)}</td>
                <td>{formatCost(row.cost)}</td>
                <td>{formatCount(row.turns)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Card>
  )
}

function Audit({ rows }: { rows: AuditRow[] }) {
  if (rows.length === 0) return <Empty>还没有工具调用记录。</Empty>
  return (
    <Card title="最近的工具调用" scroll>
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>工具</th>
            <th>决定</th>
            <th>内容</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="muted">{formatTime(row.ts)}</td>
              <td className="mono">{row.tool_name}</td>
              <td>
                <span className={`chip chip--${decisionTone(row.decision)}`}>
                  {decisionLabel(row.decision)}
                </span>
                {row.reason ? <div className="muted audit__reason">{row.reason}</div> : null}
              </td>
              <td className="mono audit__preview">{row.input_preview || '—'}</td>
              <td className="muted">
                {row.is_error ? '失败' : '成功'}
                {row.duration_ms !== null ? ` · ${row.duration_ms}ms` : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function decisionTone(decision: string): string {
  if (decision === 'blocked' || decision === 'denied') return 'danger'
  if (decision === 'approved') return 'warn'
  return 'ok'
}
