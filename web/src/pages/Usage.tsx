/**
 * Personal usage and quota for the current month.
 *
 * The quota bar is the point of the page: a number that only appears *after* a turn is
 * refused is useless, so the remaining budget is shown while there is still something to
 * decide. `null` quota means unlimited, which is stated rather than rendered as "0".
 */

import { useEffect, useState } from 'react'

import { api } from '../lib/api'
import { Card, Empty, ErrorNote, PageHeader, formatCost, formatCount } from '../components/Page'

interface UsageTotals {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost: number
  turns: number
}

interface UsagePayload {
  windowStart: string
  summary: UsageTotals
  byDay: (UsageTotals & { day: string })[]
  byKind: (UsageTotals & { request_kind: string })[]
  bySession: (UsageTotals & { session_id: string })[]
  quota: { tokens: number | null; cost: number | null }
}

function kindLabel(kind: string): string {
  if (kind === 'compaction') return '上下文压缩'
  if (kind === 'turn') return '对话'
  return kind
}

export function Usage() {
  const [data, setData] = useState<UsagePayload | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .get<UsagePayload>('/api/usage/me')
      .then((payload) => {
        if (!cancelled) setData(payload)
      })
      .catch((failure: unknown) => {
        if (!cancelled) setError(failure instanceof Error ? failure.message : '读取用量失败')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const summary = data?.summary
  const month = data?.windowStart?.slice(0, 7) ?? '本月'

  return (
    <div className="page">
      <PageHeader
        title="用量"
        description={`${month} 的 token 与费用。额度用完后新的对话会被直接拒绝。`}
      />
      <ErrorNote message={error} />

      <div className="statgrid">
        <Stat label="总 token" value={formatCount(summary?.total_tokens)} />
        <Stat label="输入 / 输出" value={`${formatCount(summary?.input_tokens)} / ${formatCount(summary?.output_tokens)}`} />
        <Stat label="缓存读取" value={formatCount(summary?.cache_read_tokens)} />
        <Stat label="费用" value={formatCost(summary?.cost)} />
        <Stat label="模型调用" value={formatCount(summary?.turns)} />
      </div>

      <Card title="额度">
        <QuotaBar
          label="token"
          used={summary?.total_tokens ?? 0}
          limit={data?.quota.tokens ?? null}
          format={formatCount}
        />
        <QuotaBar
          label="费用"
          used={summary?.cost ?? 0}
          limit={data?.quota.cost ?? null}
          format={formatCost}
        />
      </Card>

      <div className="pagegrid">
        <Card title="按类型">
          {!data || data.byKind.length === 0 ? (
            <Empty>本月还没有调用。</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>token</th>
                  <th>费用</th>
                  <th>次数</th>
                </tr>
              </thead>
              <tbody>
                {data.byKind.map((row) => (
                  <tr key={row.request_kind}>
                    <td>{kindLabel(row.request_kind)}</td>
                    <td>{formatCount(row.total_tokens)}</td>
                    <td>{formatCost(row.cost)}</td>
                    <td>{formatCount(row.turns)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="按会话">
          {!data || data.bySession.length === 0 ? (
            <Empty>本月还没有调用。</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>会话</th>
                  <th>token</th>
                  <th>费用</th>
                </tr>
              </thead>
              <tbody>
                {data.bySession.map((row) => (
                  <tr key={row.session_id}>
                    <td className="mono">{row.session_id.slice(0, 12) || '—'}</td>
                    <td>{formatCount(row.total_tokens)}</td>
                    <td>{formatCost(row.cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="按天">
        {!data || data.byDay.length === 0 ? (
          <Empty>本月还没有调用。</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>日期</th>
                <th>token</th>
                <th>费用</th>
                <th>次数</th>
              </tr>
            </thead>
            <tbody>
              {data.byDay.map((row) => (
                <tr key={row.day}>
                  <td>{row.day}</td>
                  <td>{formatCount(row.total_tokens)}</td>
                  <td>{formatCost(row.cost)}</td>
                  <td>{formatCount(row.turns)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className="stat__value">{value}</div>
    </div>
  )
}

function QuotaBar({
  label,
  used,
  limit,
  format,
}: {
  label: string
  used: number
  limit: number | null
  format: (value: number) => string
}) {
  if (limit === null || limit === undefined) {
    return (
      <div className="quota">
        <div className="quota__head">
          <span>{label} 额度</span>
          <span className="quota__value">不限</span>
        </div>
        <div className="quota__track">
          <div className="quota__fill quota__fill--none" style={{ width: '0%' }} />
        </div>
        <div className="quota__foot">本月已用 {format(used)}</div>
      </div>
    )
  }

  const ratio = limit > 0 ? Math.min(1, used / limit) : 1
  const level = ratio >= 1 ? 'danger' : ratio >= 0.8 ? 'warning' : 'ok'
  return (
    <div className="quota">
      <div className="quota__head">
        <span>{label} 额度</span>
        <span className="quota__value">
          {format(used)} / {format(limit)}
        </span>
      </div>
      <div className="quota__track">
        <div className={`quota__fill quota__fill--${level}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="quota__foot">
        {ratio >= 1 ? '已用完，新的对话会被拒绝' : `剩余 ${format(Math.max(0, limit - used))}`}
      </div>
    </div>
  )
}
