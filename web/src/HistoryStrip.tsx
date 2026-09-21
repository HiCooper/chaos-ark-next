import React, { useMemo, useRef, useState } from 'react'
import { Chip, num, pct } from './ui'

/**
 * 历史结论热力图（交易日历）：GitHub 贡献图风格的「周 × 月」色块矩阵。
 * 每个格子是一个交易日，颜色 = 当日市场环境档位（regime），
 * 格子上的小标记 = 当日有「入手 / 埋伏」结论。
 * 点击任意格子，下方展示那一天的方向结论详情。
 *
 * 移植自 chaos-ark 的 StateStrip，数据源改为本项目的 history.json（days[]）。
 */

const REGIME_TXT: Record<string, string> = {
  ATTACK: '进攻', NEUTRAL: '中性', DEFEND: '防守', PANIC: '恐慌',
}

function stateClass(r: string): string {
  switch (r) {
    case 'ATTACK': return 'is-attack'
    case 'PANIC': return 'is-panic'
    case 'DEFEND': return 'is-defend'
    default: return 'is-neutral'
  }
}

/** 周一为一周起点；返回该日期所在周的周一（ISO 日期串，作列键） */
function mondayKey(d: Date): string {
  const offset = (d.getDay() + 6) % 7 // Mon=0..Sun=6
  const m = new Date(d)
  m.setDate(d.getDate() - offset)
  return m.toISOString().slice(0, 10)
}

/** 行 0..4 = 周一..周五（A 股只在交易日出结论；周末日期并入相邻行） */
function weekRow(d: Date): number {
  return Math.min((d.getDay() + 6) % 7, 4)
}

type Column = { key: string; slots: (any | null)[] }

export default function HistoryStrip({ days }: { days: any[] }) {
  const [selected, setSelected] = useState<string>(
    days.length ? days[days.length - 1].date : '',
  )
  const selRef = useRef<HTMLButtonElement | null>(null)

  const byDate = useMemo(() => {
    const m = new Map<string, any>()
    for (const d of days) m.set(d.date, d)
    return m
  }, [days])

  // 列 = 周×月：跨月的周拆成两列，行位仍按周一~周五对齐。
  const columns = useMemo<Column[]>(() => {
    const byKey = new Map<string, Column>()
    const order: string[] = []
    for (const d of days) {
      const dt = new Date(d.date + 'T00:00:00')
      const mon = String(dt.getMonth()).padStart(2, '0')
      const key = `${mondayKey(dt)}-${mon}`
      let col = byKey.get(key)
      if (!col) {
        col = { key, slots: [null, null, null, null, null] }
        byKey.set(key, col)
        order.push(key)
      }
      col.slots[weekRow(dt)] = d
    }
    return order.sort().map((k) => byKey.get(k)!)
  }, [days])

  const colMonths = useMemo(
    () =>
      columns.map((c) => {
        for (const d of c.slots) if (d) return new Date(d.date + 'T00:00:00').getMonth()
        return -1
      }),
    [columns],
  )

  const monthStarts = useMemo(
    () => columns.map((_, i) => i === 0 || colMonths[i] !== colMonths[i - 1]),
    [columns, colMonths],
  )

  const monthLabels = useMemo(
    () =>
      columns
        .map((_, i) => ({ col: i, text: `${colMonths[i] + 1}月` }))
        .filter((_, i) => monthStarts[i]),
    [columns, colMonths, monthStarts],
  )

  const MONTH_GAP = 7
  const colOffsets = useMemo(() => {
    const out: number[] = []
    let acc = 0
    columns.forEach((_, i) => {
      if (i > 0 && monthStarts[i]) acc += MONTH_GAP
      out.push(acc)
    })
    return out
  }, [columns, monthStarts])

  const sel = byDate.get(selected)

  const counts = useMemo(() => {
    let buy = 0, amb = 0, daysN = 0
    for (const d of days) {
      const picks = d.picks || []
      if (picks.some((p: any) => p.action === 'BUY_NOW')) buy++
      if (picks.some((p: any) => p.action === 'AMBUSH')) amb++
      if (picks.length > 0) daysN++
    }
    return { buy, amb, daysN }
  }, [days])

  return (
    <div>
      <div className="hist-strip">
        <div className="hist-scroll">
          <div className="hist-inner" style={{ width: columns.length * 13 + 26 + colOffsets[colOffsets.length - 1] }}>
            <div className="hist-months">
              {monthLabels.map((l) => (
                <span key={l.col} style={{ left: l.col * 13 + 26 + colOffsets[l.col] }}>{l.text}</span>
              ))}
            </div>
            <div className="hist-body">
              <div className="hist-days" aria-hidden="true">
                <i>一</i><i /><i>三</i><i /><i>五</i>
              </div>
              <div className="hist-grid" role="group" aria-label="交易日环境热力图，点击切换日期">
                {columns.map((col, i) => {
                  const monthStart = i > 0 && monthStarts[i]
                  return (
                    <div className={`hist-col${monthStart ? ' month-start' : ''}`} key={col.key}>
                      {col.slots.map((d, row) => {
                        if (!d) return <i className="hcell empty" key={row} />
                        const picks = d.picks || []
                        const buy = picks.filter((p: any) => p.action === 'BUY_NOW').length
                        const amb = picks.filter((p: any) => p.action === 'AMBUSH').length
                        const cls = [
                          'hcell',
                          stateClass(d.regime),
                          buy > 0 ? 'has-buy' : '',
                          amb > 0 ? 'has-ambush' : '',
                          d.date === selected ? 'selected' : '',
                        ].join(' ')
                        const label = `${d.date} · ${REGIME_TXT[d.regime] || d.regime}` +
                          (buy + amb > 0 ? ` · 入手${buy} 埋伏${amb}` : ' · 无方向结论')
                        return (
                          <button
                            key={row}
                            ref={d.date === selected ? selRef : undefined}
                            className={cls}
                            title={label}
                            aria-label={label}
                            aria-pressed={d.date === selected}
                            onClick={() => setSelected(d.date)}
                          >
                            {amb > 0 && <i className="hmark" />}
                          </button>
                        )
                      })}
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </div>
        <div className="hist-legend">
          <span className="legend-item"><i className="swatch sw-attack" />进攻</span>
          <span className="legend-item"><i className="swatch sw-neutral" />中性</span>
          <span className="legend-item"><i className="swatch sw-defend" />防守</span>
          <span className="legend-item"><i className="swatch sw-panic" />恐慌</span>
          <span className="legend-item"><i className="swatch sw-buy" />当日有「入手」</span>
          <span className="legend-item"><i className="swatch sw-ambush" />当日有「埋伏」</span>
          <span className="legend-item" style={{ marginLeft: 'auto' }}>
            共 {days.length} 个交易日 · 有结论 {counts.daysN} 日 · 入手 {counts.buy} / 埋伏 {counts.amb} · 点击方格切换日期
          </span>
        </div>
      </div>

      {sel && (
        <div className="hist-detail card">
          <div className="colhead">
            <h2>历史结论 · {sel.date}</h2>
            <span className="n">
              环境 <b style={{ color: sel.regime === 'ATTACK' ? 'var(--up)' : sel.regime === 'PANIC' ? 'var(--down)' : 'var(--ink-2)' }}>
                {REGIME_TXT[sel.regime] || sel.regime}
              </b>
              {' '}· 热度 {num(sel.env_score, 1)}
            </span>
          </div>
          <div className="note" style={{ marginBottom: 14 }}>{sel.summary}</div>
          {(!sel.picks || sel.picks.length === 0) ? (
            <div className="empty">该日无行业通过共振门槛，空仓等待。</div>
          ) : (
            sel.picks.map((p: any, i: number) => {
              const buy = p.action === 'BUY_NOW'
              return (
                <div className={`hist-pick ${buy ? 'buy' : 'ambush'}`} key={i}>
                  <div className="hp-head">
                    <span className="hp-name">{p.name}</span>
                    <Chip>{p.level === 'L1' ? '一级行业' : '细分赛道'}</Chip>
                    <Chip tone={buy ? 'up' : 'ambush'}>{buy ? '入手' : '埋伏'}</Chip>
                    {p.low_confidence && <Chip tone="warn">样本偏少</Chip>}
                    <div className="hp-score">
                      <b className="num">{num(p.composite, 1)}</b>
                      <span className="muted">综合分</span>
                    </div>
                  </div>
                  <div className="muted small">
                    建议周期 {p.horizon} 个交易日（约 {(p.horizon / 21).toFixed(1)} 个月）
                    {p.tradable && p.etf ? ` · 可交易 ${p.etf}（${p.etf_code}）` : ' · 暂无流动性达标 ETF'}
                  </div>
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}
