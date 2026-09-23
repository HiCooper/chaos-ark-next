import React, { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { num, pct } from './ui'

/**
 * 标的走势核验弹窗：点「可交易标的」或折算样本弹出。
 *
 * 目的只有一个——让用户逐笔核对「ETF 交易的钱算得对不对」：
 *   · 走势图同时画「复权连续价」（结算取价口径）与「原始市价」（可与行情软件逐日核对），
 *     份额折算（拆分/合并）日两侧价的跳变一目了然；
 *   · B/S 成交点直接落在图上，悬浮给出价格、原因、当日组合净值、权重、名义账本；
 *   · 折算断点用菱形标出，悬浮给出「原始跳变 vs 参照收益」与复权因子；
 *   · 下方账本表：固定名义本金 1 万元 → 份额 → 卖出金额 → 毛/净收益，
 *     净收益率与结算层 ret 逐位对账（差值必须为 0）。
 */

const NOTIONAL = 10000                 // 单笔账本的名义本金（元），只作核验基准
const COST_RT = 0.0014                 // 双边成本 = 2×(佣金万二 + 滑点万五)，与结算层同源
const C_ADJ = '#c62828'                // 复权价（与主曲线同色系 = 结算口径）
const C_RAW = '#8b94a3'                // 原始市价
const C_B = '#c62828'
const C_S = '#1565c0'
const C_BRK = '#b45309'                // 份额折算断点

export type SidEvent = {
  k: 'B' | 'S'
  i: number                            // 在组合曲线 dates 中的索引
  date?: string                        // B 事件所在交易日（由父层按 i 填）
  sw: string
  sid: string
  price?: number
  bpx?: number
  bd?: string
  h?: number
  a?: string
  sc?: number
  why?: string
  gross?: number
  ret?: number
  ex?: number
  w?: number
}

export type EtfModalInput = {
  sid: string
  /** 组合曲线（B/S 悬浮里显示当日净值用） */
  curveDates: string[]
  curveNav: number[]
  /** 该标的的全部 B/S 事件（父层从 review.json events 里筛好） */
  events: SidEvent[]
  /** 打开弹窗时聚焦的日期（如某笔交易的买入日或某个折算日） */
  focusDate?: string
  onClose: () => void
}

type EtfSeries = {
  sid: string
  code: string
  name: string
  sw?: string | null
  dates: string[]
  raw: (number | null)[]
  adj: (number | null)[]
  breaks: { date: string; prev: string; raw_ret: number; ref_ret: number; ref_src: string; ratio?: number; factor: number }[]
}

const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏' }
const REF_TXT: Record<string, string> = { index: '行业指数', median: '全池中位数' }

/** 该标的的成交配对表：一行 = 一笔完整交易（以 S 事件为准） */
function pairTrades(events: SidEvent[]) {
  const idxOf: Record<string, number> = {}
  return events
    .filter((e) => e.k === 'S' && e.bd)
    .map((s) => {
      const b = events.find((e) => e.k === 'B' && e.sid === s.sid && e.date === s.bd)
      const bpx = s.bpx ?? b?.price
      const spx = s.price
      const gross = spx != null && bpx ? spx / bpx - 1 : null
      return {
        buyDate: s.bd!,
        sellDate: s.date!,
        bpx,
        spx,
        gross,
        ret: s.ret,
        grossEv: s.gross ?? null,
        why: s.why,
        wBuy: b?.w,
        wSell: s.w,
        act: s.a,
      }
    })
    .sort((a, b) => b.buyDate.localeCompare(a.buyDate)) // 买入日倒序：最近的交易排最前
}

/** 序列首末非空值的累计涨跌幅（数据起点 → 最新交易日，「到目前为止」的 ETF 自身涨幅） */
function seriesTotal(a: (number | null)[], dates: string[]):
  { pct: number; from: string; to: string } | null {
  let i0 = -1, i1 = -1
  for (let i = 0; i < a.length; i++) if (a[i] != null) { if (i0 < 0) i0 = i; i1 = i }
  if (i0 < 0 || i1 <= i0 || a[i0] == null || a[i1] == null) return null
  return { pct: a[i1]! / a[i0]! - 1, from: dates[i0], to: dates[i1] }
}

export function EtfModal(props: EtfModalInput) {
  const { sid, curveDates, curveNav, events, focusDate, onClose } = props
  const [data, setData] = useState<EtfSeries | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    let alive = true
    fetch(`data/etf/${sid}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => { if (alive) setData(j) })
      .catch((e) => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [sid])

  const trades = useMemo(() => pairTrades(events), [events])

  // ---- 到目前为止的总收益摘要 ----
  // 已结算交易统计（名义账本口径：每笔独立 1 万元本金）
  const settled = useMemo(() => {
    const rs = trades.map((t) => t.ret).filter((r): r is number => r != null)
    if (!rs.length) return null
    const win = rs.filter((r) => r > 0).length
    return {
      n: rs.length,
      winRate: win / rs.length,
      avg: rs.reduce((a, b) => a + b, 0) / rs.length,
      // 累计名义盈亏：每笔 1 万元独立账本的盈亏求和（元）
      pnl: rs.reduce((a, r) => a + r * NOTIONAL, 0),
    }
  }, [trades])

  // 在途持仓：B 事件没有任何 S 事件与之配对（S.bd = 买入日）
  const openPos = useMemo(() => {
    const soldBd = new Set(events.filter((e) => e.k === 'S').map((e) => e.bd))
    return events.filter((e) => e.k === 'B' && e.date && !soldBd.has(e.date))
  }, [events])

  // ETF 自身「到目前为止」的累计涨跌（复权口径，数据起点 → 最新交易日）
  const etfTotal = useMemo(
    () => (data ? seriesTotal(data.adj, data.dates) : null),
    [data],
  )

  // 最新复权价（在途持仓估值用）
  const lastAdj = useMemo(() => {
    if (!data) return null
    for (let i = data.adj.length - 1; i >= 0; i--) {
      if (data.adj[i] != null) return { v: data.adj[i]!, d: data.dates[i] }
    }
    return null
  }, [data])

  // ---- echarts ----
  const navIdxOf: Record<string, number> = useMemo(() => {
    const m: Record<string, number> = {}
    curveDates.forEach((d, i) => { m[d] = i })
    return m
  }, [curveDates])

  useEffect(() => {
    if (!data || !box.current) return
    echarts.getInstanceByDom(box.current)?.dispose()
    const ch = echarts.init(box.current)
    chart.current = ch
    ;(box.current as any).__chart = ch

    const idxOf: Record<string, number> = {}
    data.dates.forEach((d, i) => { idxOf[d] = i })
    const evByDate: Record<string, SidEvent[]> = {}
    for (const e of events) {
      const d = e.k === 'B' ? e.date : e.date
      if (d) (evByDate[d] = evByDate[d] || []).push(e)
    }
    const brkByDate: Record<string, any> = {}
    for (const b of data.breaks) brkByDate[b.date] = b

    const bPts = events.filter((e) => e.k === 'B' && idxOf[e.date!] !== undefined)
      .map((e) => ({ value: [e.date, data.adj[idxOf[e.date!]]], date: e.date }))
    const sPts = events.filter((e) => e.k === 'S' && idxOf[e.date!] !== undefined)
      .map((e) => ({ value: [e.date, data.adj[idxOf[e.date!]]], date: e.date }))
    const brkPts = data.breaks.filter((b) => idxOf[b.date] !== undefined)
      .map((b) => ({ value: [b.date, data.adj[idxOf[b.date]]], date: b.date }))

    // 初始视窗：focusDate 为主，否则第一处折算日，否则全区间
    const fi = focusDate && idxOf[focusDate] !== undefined ? idxOf[focusDate]
      : data.breaks.length && idxOf[data.breaks[0].date] !== undefined ? idxOf[data.breaks[0].date]
        : null
    const W = 240
    const z0 = fi == null ? 0 : Math.max(0, fi - W) / (data.dates.length - 1) * 100
    const z1 = fi == null ? 100 : Math.min(data.dates.length - 1, fi + W) / (data.dates.length - 1) * 100

    const tipKV = (k: string, v: string) => `<div class="ek-kv"><span>${k}</span><b>${v}</b></div>`

    ch.setOption({
      animation: false,
      grid: { left: 62, right: 62, top: 34, height: 240 },
      legend: {
        top: 2, left: 54, itemWidth: 14, itemHeight: 8, itemGap: 14,
        textStyle: { fontSize: 11, color: '#4b5563' },
        data: ['复权连续价（结算口径）', '原始市价（行情软件口径）', '买入 B', '卖出 S', '份额折算日'],
      },
      tooltip: {
        trigger: 'axis', confine: true, transitionDuration: 0,
        backgroundColor: '#101725', borderWidth: 0, padding: [8, 10],
        textStyle: { color: '#fff', fontSize: 12 },
        extraCssText: 'border-radius:8px;box-shadow:0 8px 22px rgba(16,23,37,.24);',
        formatter: (ps: any) => {
          const arr = Array.isArray(ps) ? ps : [ps]
          const date = String(arr[0]?.axisValue ?? '')
          const i = idxOf[date]
          if (i === undefined) return ''
          const raw = data.raw[i]
          const adj = data.adj[i]
          let html = `<div class="ek-h">${date}</div>`
          html += tipKV('原始市价', raw != null ? raw.toFixed(3) : '停牌/无数据')
          html += tipKV('复权连续价', adj != null ? adj.toFixed(4) : '—')
          const ni = navIdxOf[date]
          if (ni != null && curveNav[ni] != null) html += tipKV('组合净值', curveNav[ni].toFixed(4))
          const brk = brkByDate[date]
          if (brk) {
            html += `<div class="ek-sec">份额折算日 ⚠</div>`
            html += tipKV('原始价格跳变', `${(brk.raw_ret * 100).toFixed(1)}%`)
            html += tipKV('参照真实收益', `${(brk.ref_ret * 100).toFixed(2)}%（${REF_TXT[brk.ref_src] || brk.ref_src}）`)
            html += tipKV('复权因子', `×${brk.factor.toFixed(4)}`)
            html += `<div class="ek-mute">价值中性事件：持有人份额×价格不变，不应记为盈亏。</div>`
          }
          const evs = evByDate[date] || []
          for (const e of evs) {
            const isB = e.k === 'B'
            html += `<div class="ek-sec">${isB ? '买入 B' : '卖出 S'} · ${e.sw}${e.a ? `（${ACT_TXT[e.a] || e.a}）` : ''}</div>`
            html += tipKV(isB ? '买入价（T+1开盘）' : '卖出价（到期收盘）', (e.price ?? 0).toFixed(4))
            if (!isB) {
              html += tipKV('毛收益率', e.gross != null ? pct(e.gross, 2) : '—')
              html += tipKV('净收益率（扣双边成本）', e.ret != null ? pct(e.ret, 2) : '—')
              html += tipKV('相对基准超额', e.ex != null ? pct(e.ex, 2) : '—')
            }
            if (e.w != null) html += tipKV('组合权重 w', `${(e.w * 100).toFixed(1)}%`)
            if (isB && e.h) html += tipKV('建议周期', `${e.h} 个交易日`)
            if (isB && e.sc != null) html += tipKV('综合分', String(e.sc))
            if (!isB && e.why) html += `<div class="ek-mute">卖出原因：${e.why}</div>`
          }
          return html
        },
      },
      xAxis: {
        type: 'category', data: data.dates, boundaryGap: false,
        axisLine: { lineStyle: { color: '#e4e7ec' } },
        axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#8b94a3', hideOverlap: true },
        splitLine: { show: false },
      },
      yAxis: [{
        type: 'value', scale: true, splitNumber: 5,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#8b94a3' },
        splitLine: { lineStyle: { color: '#eef0f3' } },
      }],
      dataZoom: [
        { type: 'inside', zoomOnMouseWheel: true, moveOnMouseMove: true, start: z0, end: z1 },
        {
          type: 'slider', left: 62, right: 62, bottom: 6, height: 16, start: z0, end: z1,
          borderColor: '#e4e7ec', fillerColor: 'rgba(31,58,138,0.12)',
          handleStyle: { color: '#1f3a8a', borderColor: '#1f3a8a' },
          textStyle: { color: '#8b94a3', fontSize: 10 },
          labelFormatter: (v: number) => String(data.dates[Math.round(v)] ?? ''),
        },
      ],
      series: [
        {
          name: '原始市价（行情软件口径）', type: 'line', data: data.raw, symbol: 'none', z: 2,
          lineStyle: { width: 1, color: C_RAW, type: 'dashed', opacity: 0.8 },
          itemStyle: { color: C_RAW }, connectNulls: false,
        },
        {
          name: '复权连续价（结算口径）', type: 'line', data: data.adj, symbol: 'none', z: 3,
          lineStyle: { width: 1.8, color: C_ADJ },
          itemStyle: { color: C_ADJ }, connectNulls: false,
        },
        {
          name: '买入 B', type: 'scatter', data: bPts, z: 6, symbolSize: 15,
          itemStyle: { color: C_B, borderColor: '#fff', borderWidth: 1.4 },
          label: { show: true, formatter: 'B', color: '#fff', fontSize: 9, fontWeight: 700, position: 'inside' },
        },
        {
          name: '卖出 S', type: 'scatter', data: sPts, z: 6, symbolSize: 15,
          itemStyle: { color: C_S, borderColor: '#fff', borderWidth: 1.4 },
          label: { show: true, formatter: 'S', color: '#fff', fontSize: 9, fontWeight: 700, position: 'inside' },
        },
        {
          name: '份额折算日', type: 'scatter', data: brkPts, z: 7, symbolSize: 13,
          symbol: 'diamond', itemStyle: { color: C_BRK, borderColor: '#fff', borderWidth: 1.2 },
          label: { show: data.breaks.length <= 3, formatter: '折算', color: '#fff', fontSize: 8, position: 'top' },
        },
      ],
    })
    const onResize = () => ch.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      ch.dispose()
      chart.current = null
    }
  }, [data, events, curveDates, curveNav, navIdxOf, focusDate])

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="between" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>
            {data ? `${data.name} ` : ''}
            <span className="muted num" style={{ fontSize: 14 }}>{sid}</span>
            {data?.sw && <span className="muted" style={{ fontSize: 13 }}> · {data.sw}</span>}
          </h2>
          <button className="linkbtn" onClick={onClose}>关闭 (Esc)</button>
        </div>

        {err && <div className="note">走势数据加载失败：{err}</div>}
        {!data && !err && <div className="note">加载走势数据…</div>}

        {/* 到目前为止的总收益摘要：ETF 自身累计 + 系统交易该 ETF 的账本合计 + 在途持仓 */}
        {data && (etfTotal || settled || openPos.length > 0) && (
          <div className="etf-stats">
            {etfTotal && (
              <div className="estat">
                <div className="l">ETF 累计涨跌（复权）</div>
                <div className={`v num ${etfTotal.pct >= 0 ? 'up' : 'down'}`}>{pct(etfTotal.pct, 1)}</div>
                <div className="m">{etfTotal.from} → {etfTotal.to}（数据起止）</div>
              </div>
            )}
            {settled && (
              <div className="estat">
                <div className="l">系统已结算 {settled.n} 笔</div>
                <div className={`v num ${settled.pnl >= 0 ? 'up' : 'down'}`}>
                  {settled.pnl >= 0 ? '+' : ''}{settled.pnl.toFixed(0)} 元
                </div>
                <div className="m">
                  胜率 {pct(settled.winRate, 0, false)} · 单笔平均 {pct(settled.avg, 2)} · 名义 1 万/笔
                </div>
              </div>
            )}
            {openPos.map((e, i) => {
              const fl = lastAdj && e.price ? lastAdj.v / e.price - 1 : null
              return (
                <div className="estat" key={i}>
                  <div className="l">在途持仓（{e.date} 买入）</div>
                  <div className={`v num ${fl != null && fl >= 0 ? 'up' : 'down'}`}>{fl != null ? pct(fl, 2) : '—'}</div>
                  <div className="m">
                    买价 {e.price?.toFixed(4)} → 现价（{lastAdj?.d}）{lastAdj?.v.toFixed(4)} · 未扣卖出成本
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {data && (
          <>
            <div ref={box} style={{ width: '100%', height: 340 }} />

            {data.breaks.length > 0 && (
              <div className="note" style={{ borderLeft: `3px solid ${C_BRK}`, paddingLeft: 10, margin: '10px 0' }}>
                <b>该标的发生过 {data.breaks.length} 次「份额折算」（拆分/合并）</b>——
                折算日单位价格按整数倍跳变（图中原始市价的断崖），但持有人份额同步反向变化、总价值不变。
                结算用「复权连续价」消除这种价值中性跳变（红线上无断点）。
                复权因子 ×{data.breaks[0].factor.toFixed(4)} 起的所有日期，
                <b>复权价 = 原始市价 × 因子</b>，收益按复权价算才正确。
              </div>
            )}

            <div className="tradescroll" style={{ marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>买入日</th>
                    <th style={{ textAlign: 'right' }}>买入价*</th>
                    <th style={{ textAlign: 'right' }}>份额 (1万元)</th>
                    <th>卖出日</th>
                    <th style={{ textAlign: 'right' }}>卖出价*</th>
                    <th style={{ textAlign: 'right' }}>卖出金额</th>
                    <th style={{ textAlign: 'right' }}>毛收益率</th>
                    <th style={{ textAlign: 'right' }}>成本</th>
                    <th style={{ textAlign: 'right' }}>净收益率</th>
                    <th style={{ textAlign: 'right' }}>结算 ret 对账</th>
                    <th>卖出原因</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => {
                    const shares = t.bpx ? NOTIONAL / t.bpx : null
                    const sellAmt = shares != null && t.spx != null ? shares * t.spx : null
                    const net = t.gross != null ? t.gross - COST_RT : null
                    const chk = net != null && t.ret != null ? Math.abs(net - t.ret) : null
                    return (
                      <tr key={i}>
                        <td className="num">{t.buyDate}</td>
                        <td className="num" style={{ textAlign: 'right' }}>{num(t.bpx, 4)}</td>
                        <td className="num" style={{ textAlign: 'right' }}>{shares != null ? num(shares, 2) : '—'}</td>
                        <td className="num">{t.sellDate}</td>
                        <td className="num" style={{ textAlign: 'right' }}>{num(t.spx, 4)}</td>
                        <td className="num" style={{ textAlign: 'right' }}>{sellAmt != null ? sellAmt.toFixed(2) : '—'}</td>
                        <td className="num" style={{ textAlign: 'right', color: (t.gross ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
                          {pct(t.gross, 2)}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>-0.14%</td>
                        <td className="num" style={{ textAlign: 'right', color: (net ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
                          {pct(net, 2)}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>
                          {pct(t.ret, 2)}
                          {chk != null && (
                            // ret 在结算层四舍五入到 5 位小数，与前端重算的最大自然差 ~5e-6
                            <span className="muted" style={{ fontSize: 10, marginLeft: 4 }}>
                              {chk < 1e-5 ? '✓' : `Δ${(chk * 100).toFixed(3)}`}
                            </span>
                          )}
                        </td>
                        <td className="muted" style={{ fontSize: 11.5 }}>{t.why || '—'}</td>
                      </tr>
                    )
                  })}
                  {!trades.length && (
                    <tr><td colSpan={11} className="muted">该标的在结算区间内没有已完成的成交（可能有在途持仓）</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="note" style={{ marginTop: 8 }}>
              * 价格为<b>复权连续价</b>（结算取价口径）。份额折算日之后，复权价 = 原始市价 × 折算因子；
              与行情软件核对请看图中灰色虚线（原始市价）。账本：名义本金 {NOTIONAL.toLocaleString()} 元 →
              份额 = 本金 ÷ 买入价 → 卖出金额 = 份额 × 卖出价；毛收益率 = 卖出价 ÷ 买入价 − 1；
              成本 0.14% = 双边佣金万二 + 双边滑点万五；净收益率 = 毛收益率 − 0.14%，
              <b>「结算 ret 对账」列应与净收益率逐位一致（✓）</b>——这一列就是"钱算得对不对"的直接检验。
            </div>
          </>
        )}
      </div>
    </div>
  )
}
