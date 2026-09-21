import React from 'react'

export function pct(x: number | null | undefined, nd = 1, sign = true): string {
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

export function Chip(props: { children: React.ReactNode; tone?: string }) {
  return <span className={`chip ${props.tone || ''}`}>{props.children}</span>
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
