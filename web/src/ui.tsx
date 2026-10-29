import React from 'react'

export function pct(x: number | null | undefined, nd = 2, sign = true): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '—'
  const v = x * 100
  return `${sign && v > 0 ? '+' : ''}${v.toFixed(nd)}%`
}

export function num(x: number | null | undefined, nd = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '—'
  return x.toFixed(nd)
}

export function Metric(props: { k: string; v: string; n?: string; tone?: 'up' | 'down' }) {
  return (
    <div className="metric">
      <div className="k">{props.k}</div>
      <div className={`v num ${props.tone || ''}`}>{props.v}</div>
      {props.n && <div className="n">{props.n}</div>}
    </div>
  )
}

export function Chip(props: { children: React.ReactNode; tone?: string; title?: string }) {
  return <span className={`chip ${props.tone || ''}`} title={props.title}>{props.children}</span>
}

export function Bars(props: {
  rows: { label: string; value: number; text: string; color?: string }[]
  max?: number
}) {
  const max = props.max ?? Math.max(...props.rows.map((r) => r.value), 1)
  return (
    <div>
      {props.rows.map((r, i) => (
        <div className="bar-row" key={i}>
          <div className="lbl">{r.label}</div>
          <div className="bar">
            <i style={{ width: `${Math.max(2, (r.value / max) * 100)}%`, background: r.color || 'var(--accent)' }} />
          </div>
          <div className="val num">{r.text}</div>
        </div>
      ))}
    </div>
  )
}

export type MarkerLine = {
  text: string
  sub?: string
  tone?: 'up' | 'down' | 'mute'
  tag?: string                 // 短标签：买入侧 = 入手/埋伏；卖出侧 = 到期/顺延/失效
  tagTone?: 'in' | 'amb' | 'exit' | 'mute'
}

export type Marker = {
  i: number                 // 在 dates 中的索引
  k: 'B' | 'S'              // 买入 / 卖出
  act?: string              // BUY_NOW / AMBUSH（买入侧：区分入手与埋伏）
  stack?: number            // 同一交易日多个买入徽标的堆叠序号，避免重叠
  title: string             // 悬浮标题（日期 + 笔数）
  lines: MarkerLine[]       // 逐笔明细
  pos?: string              // 当日仓位（悬浮时一并展示）
  foot?: string             // 悬浮框底部补充说明（如卖出原因全称）
}

export function Card(props: { title?: string; extra?: React.ReactNode; children: React.ReactNode; tight?: boolean }) {
  return (
    <div className={`card ${props.tight ? 'tight' : ''}`}>
      {(props.title || props.extra) && (
        <div className="between" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>{props.title}</h2>
          {props.extra}
        </div>
      )}
      {props.children}
    </div>
  )
}

/**
 * 数据兜底提示条。两种情形要分开说清楚，不能混成一句：
 *   · fill（tail_fill）：官方**尚未发布**最新交易日 → 用实时行情/ETF 收益补**尾部**；
 *   · gap（gap_fill）：官方**整日缺数据**（那天的 bar 源端就没有，ETF/指数当日正常交易）
 *     → 用对应 ETF 的日收益把该日行业指数接上，点位是推算，收益口径真实。
 */
export function DataFillBar({ fill, gap, style }:
  { fill?: any; gap?: any; style?: React.CSSProperties }) {
  const hasFill = !!fill?.days && Object.keys(fill.days).length > 0
  const hasGap = !!gap?.days && Object.keys(gap.days).length > 0
  if (!hasFill && !hasGap) return null
  return (
    <>
      {hasGap && (
        <div className="warnbar" style={style}>
          <b>数据含代理日</b>（{Object.keys(gap.days).join('、')}）：申万当日<b>整日未出数</b>
          （不是抓取失败——同日 ETF 与基准指数都有行情），该日行业指数与量额由
          <b>对应 ETF 的当日收益前推</b>：收益口径真实、点位为推算，量额为估算。
          官方补发后重跑 <code>python3 engine/ark.py daily</code> 会自动用真值覆盖。
        </div>
      )}
      {hasFill && (
        <div className="warnbar" style={style}>
          <b>数据含兜底日</b>（{Object.keys(fill.days).join('、')}）：申万官方日线尚未发布（停在 {fill.official_last}），
          {(fill.method || 'realtime') === 'etf_proxy'
            ? <>本轮用<b>对应 ETF 的日收益前推</b>行业指数——收益口径真实、点位为推算，量额为估算。</>
            : <>本轮收盘价取自申万<b>实时行情</b>（真值），量额为估算。</>}
          实测兜底代价：单日点位误差中位 ≤0.7%、5 日累计收益偏差中位 ≈0.2%，方向判断不受影响；
          官方出数后重跑 <code>python3 engine/ark.py daily</code> 会自动用真值覆盖并做盘前复核。
        </div>
      )}
    </>
  )
}
