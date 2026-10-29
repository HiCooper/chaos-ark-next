import React, { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { num, pct, Chip } from './ui'
import { yuan, yuanSigned, addWeekdays } from './sim'

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
 *
 * 两种口径（由 scope 决定）：
 *   · 复盘页 / 今日研判页：引擎的**全历史名义账本**（初始本金 1.0，金额是权重折算出来的）；
 *   · 我的持仓页：**本账户的真账**——按你的本金与入市起始日重放，金额是元、
 *     份数是实际份数、盈亏是已扣成本的真钱。统计窗口 = 入市起始日 → 选中日，
 *     图上的 B/S 也只有这本账的成交（见 sim.ts: bookEvents）。
 */

const NOTIONAL = 10000                 // 单笔账本的名义本金（元），只作核验基准
const COST_RT = 0.0014                 // 双边成本 = 2×(佣金万二 + 滑点万五)，与结算层同源
const C_ADJ = '#c62828'                // 复权价（与主曲线同色系 = 结算口径）
const C_RAW = '#8b94a3'                // 原始市价
const C_B = '#c62828'
const C_S = '#1565c0'
const C_BRK = '#b45309'                // 份额折算断点（琥珀）
const C_DIP = '#7a5af8'                // 抄底兜底（紫，与「份额折算」的琥珀菱形区分开）

export type SidEvent = {
  k: 'B' | 'S'
  /** 在组合曲线 dates 中的索引（引擎事件用；「我的持仓」的事件直接给 date） */
  i?: number
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
  /** 未归一化的相对权重（1/σ × 分数倾斜）：中间量，界面上不再展示 */
  w?: number
  /** 组合占比 = 该笔成交当天占组合的资金比例（引擎算好，已含当日暴露） */
  w_share?: number
  /** 以下字段只有「我的持仓」的本账户事件才有（真实人民币金额） */
  amt?: number
  proceeds?: number
  sh?: number
  pnl?: number
  /** B：到期日（未平仓行显示"还没卖、什么时候卖"）；endEst = 按周末推算（未计节假日） */
  end?: string | null
  endEst?: boolean
}

/**
 * 统计窗口（「我的持仓」专用）。
 *
 * 复盘页看的是引擎的**全历史名义账本**，没有窗口概念；而「我的持仓」这本账是从
 * 你填的**入市起始日**才开始的，所以弹窗里的一切统计——ETF 自身涨跌、已结算笔数、
 * 在途持仓、账本表——都必须在这个窗口里算，否则会和页面上的持仓对不上。
 */
export type EtfScope = {
  /** 入市起始日（统计起算日） */
  from?: string
  /** 当前选中日（统计截止、盯市日） */
  to?: string
  /** 本金（元），仅用于文案 */
  capital?: number
  /** 事件里的 amt/sh/proceeds/pnl 是**真实人民币**（本账户重放账本） */
  realMoney?: boolean
}

export type EtfModalInput = {
  sid: string
  /** 组合曲线（B/S 悬浮里显示当日净值用） */
  curveDates: string[]
  curveNav: number[]
  /** curveNav 是账户金额（元）而非净值倍数 */
  curveIsMoney?: boolean
  /** 该标的的全部 B/S 事件（父层从 review.json events 里筛好） */
  events: SidEvent[]
  /** 打开弹窗时聚焦的日期（如某笔交易的买入日或某个折算日） */
  focusDate?: string
  /** 统计窗口（给了就按窗口口径统计与文案） */
  scope?: EtfScope
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

const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏', DIP: '抄底' }
const REF_TXT: Record<string, string> = { index: '行业指数', median: '全池中位数' }

/** 账本明细的一行：open=false 为已结算（有 S），open=true 为在途（只有 B） */
type LedgerRow = {
  open: boolean
  buyDate: string
  sellDate?: string
  bpx: number | null
  spx: number | null
  gross: number | null
  /** 结算层净收益率（已扣双边成本）；在途行为 null */
  ret: number | null
  grossEv: number | null
  why?: string
  wBuy?: number
  wSell?: number
  act?: string
  shBuy: number | null
  amtBuy: number | null
  /** 卖出金额（在途行 = 按估值价的市值） */
  proceeds: number | null
  /** 已实现盈亏（元，仅本账户口径的已结算行） */
  pnl: number | null
  /** 浮动盈亏（元，仅在途行；本账户口径为真钱） */
  floatPnl: number | null
  floatPct: number | null
  endDate: string | null
  endEst: boolean
}

/**
 * 该标的的成交配对表（已结算行）：一行 = 一笔完整交易（以 S 事件为准）。
 *
 * 还没卖出的 B 点由 openRowsOf() 生成，两者合并成"账本明细"——每一笔买入都要看得见，
 * 无论它的结局是卖出还是仍在途。
 */
function pairTrades(events: SidEvent[]): LedgerRow[] {
  const settled: LedgerRow[] = events
    .filter((e) => e.k === 'S' && e.bd)
    .map((s) => {
      const b = events.find((e) => e.k === 'B' && e.sid === s.sid && e.date === s.bd)
      const bpx = s.bpx ?? b?.price ?? null
      const spx = s.price ?? null
      const gross = spx != null && bpx ? spx / bpx - 1 : null
      return {
        open: false,
        buyDate: s.bd as string,
        sellDate: s.date,
        bpx,
        spx,
        gross,
        ret: s.ret ?? null,
        grossEv: s.gross ?? null,
        why: s.why,
        wBuy: b?.w,
        wSell: s.w,
        act: s.a,
        // 本账户口径（真实金额）：只有「我的持仓」的事件才带
        shBuy: s.sh ?? null,
        amtBuy: b?.amt ?? null,
        proceeds: s.proceeds ?? null,
        pnl: s.pnl ?? null,
        floatPnl: null,
        floatPct: null,
        endDate: null,
        endEst: false,
      }
    })
  return settled.sort((a, b) => b.buyDate.localeCompare(a.buyDate)) // 买入日倒序
}

/** 在途 B 点（有 B 无 S）→ 账本明细行；估值价由调用方给（窗口末端的复权价） */
function openRowsOf(events: SidEvent[], px: number | null, money: boolean): LedgerRow[] {
  if (px == null) return []
  const soldBd = new Set(events.filter((e) => e.k === 'S' && e.bd).map((e) => e.bd))
  return events
    .filter((e) => e.k === 'B' && e.date && !soldBd.has(e.date) && e.price)
    .map((e) => {
      const bpx = e.price as number
      const gross = px / bpx - 1
      const amtBuy = money ? (e.amt ?? null) : NOTIONAL
      const shBuy = money ? (e.sh ?? null) : NOTIONAL / bpx
      const mv = shBuy != null ? shBuy * px : null
      return {
        open: true,
        buyDate: e.date as string,
        bpx,
        spx: px,
        gross,
        ret: null,
        grossEv: null,
        why: undefined,
        wBuy: e.w,
        act: e.a,
        shBuy,
        amtBuy,
        proceeds: mv,
        pnl: null,
        floatPnl: mv != null && amtBuy != null ? mv - amtBuy : null,
        floatPct: gross,
        endDate: e.end ?? null,
        endEst: !!e.endEst,
      }
    })
}

/** 序列首末非空值的累计涨跌幅；给了窗口只算窗口内（起点取 ≥from 的首个有效值） */
function seriesTotal(a: (number | null)[], dates: string[], from?: string, to?: string):
  { pct: number; from: string; to: string } | null {
  let i0 = -1, i1 = -1
  for (let i = 0; i < a.length; i++) {
    if (a[i] == null) continue
    if (from && dates[i] < from) continue
    if (to && dates[i] > to) continue
    if (i0 < 0) i0 = i
    i1 = i
  }
  if (i0 < 0 || i1 <= i0 || a[i0] == null || a[i1] == null) return null
  return { pct: a[i1]! / a[i0]! - 1, from: dates[i0], to: dates[i1] }
}

export function EtfModal(props: EtfModalInput) {
  const { sid, curveDates, curveNav, curveIsMoney, events, focusDate, scope, onClose } = props
  const [data, setData] = useState<EtfSeries | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  // 账户口径：统计与账本按入市起始日 → 选中日算，金额是真实人民币
  const scoped = !!scope && (!!scope.from || !!scope.to)
  const money = !!scope?.realMoney

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    let alive = true
    // 展示层根目录：view/（不是数据层 data/）。由 engine/build_etf_webdata.py
    // 从结算账簿切片而来，价格与结算取价同源。
    fetch(`view/etf/${sid}.json`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => { if (alive) setData(j) })
      .catch((e) => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [sid])

  // 窗口过滤：调用方（我的持仓）给的已经是本账户的成交，这里再兜一层——
  // 万一父层给的是引擎事件，也不会把入市起始日之前的成交算进来。
  const inWin = (d?: string) => {
    if (!d) return false
    if (scope?.from && d < scope.from) return false
    if (scope?.to && d > scope.to) return false
    return true
  }
  const winEvents = useMemo(
    () => (scoped ? events.filter((e) => inWin(e.date)) : events),
    [events, scoped, scope?.from, scope?.to],
  )

  const trades = useMemo(() => pairTrades(winEvents), [winEvents])

  // ---- 到目前为止的总收益摘要 ----
  // 引擎页：名义账本口径（每笔独立 1 万元本金）
  // 我的持仓：本账户口径（真实金额、真实份数、真实已实现盈亏）
  const settled = useMemo(() => {
    const rows = trades.filter((t) => t.ret != null)
    if (!rows.length) return null
    const win = rows.filter((t) => (t.ret as number) > 0).length
    const avg = rows.reduce((a, t) => a + (t.ret as number), 0) / rows.length
    if (money) {
      const cost = rows.reduce((a, t) => a + (t.amtBuy || 0), 0)
      const pnl = rows.reduce((a, t) => a + (t.pnl || 0), 0)
      return { n: rows.length, winRate: win / rows.length, avg, cost, pnl, money: true as const }
    }
    return {
      n: rows.length, winRate: win / rows.length, avg,
      pnl: rows.reduce((a, t) => a + (t.ret as number) * NOTIONAL, 0), money: false as const,
    }
  }, [trades, money])

  // 最新复权价 / 窗口末端的复权价（在途持仓估值用）
  const lastAdj = useMemo(() => {
    if (!data) return null
    for (let i = data.adj.length - 1; i >= 0; i--) {
      if (!inWin(data.dates[i])) continue          // 窗口口径：不超过选中日
      if (data.adj[i] != null) return { v: data.adj[i]!, d: data.dates[i] }
    }
    return null
  }, [data, scoped, scope?.from, scope?.to])

  // 在途持仓：B 事件没有任何 S 事件与之配对（S.bd = 买入日）。聚合为一张卡，也进账本明细。
  const openPos = useMemo(() => {
    const soldBd = new Set(winEvents.filter((e) => e.k === 'S').map((e) => e.bd))
    return winEvents.filter((e) => e.k === 'B' && e.date && !soldBd.has(e.date))
  }, [winEvents])
  const openPosAgg = useMemo(() => {
    if (!openPos.length || !lastAdj) return null
    const dates = openPos.map((e) => e.date!).sort()
    if (money) {
      const rows = openPos.filter((e) => e.amt != null && e.sh != null)
      if (!rows.length) return null
      const cost = rows.reduce((a, e) => a + (e.amt as number), 0)
      const mv = rows.reduce((a, e) => a + (e.sh as number) * lastAdj.v, 0)
      return {
        n: rows.length, cost, mv, pnl: mv - cost,
        avg: cost > 0 ? mv / cost - 1 : 0,
        from: dates[0], to: dates[dates.length - 1], money: true as const,
      }
    }
    const fls = openPos
      .map((e) => (e.price ? lastAdj.v / e.price - 1 : null))
      .filter((f): f is number => f != null)
    if (!fls.length) return null
    return {
      n: fls.length,
      avg: fls.reduce((a, b) => a + b, 0) / fls.length,
      pnl: fls.reduce((a, f) => a + f * NOTIONAL, 0),
      cost: 0, mv: 0, money: false as const,
      from: dates[0],
      to: dates[dates.length - 1],
    }
  }, [openPos, lastAdj, money])

  // 账本明细 = 已结算行 + **在途行**（还没卖出的 B 点也要看得见），统一按买入日倒序。
  // 在途行的估值价取窗口末端复权价；引擎口径下没有 end 字段时，用交易日历按 H 推算到期日。
  const ledger = useMemo(() => {
    const openRows = openRowsOf(winEvents, lastAdj?.v ?? null, money)
    if (!money) {
      // 引擎事件不带到期日：买入日索引 + 建议周期 = 到期交易日（与结算层 end = buy_i + H 一致）；
      // 落在数据末端之外时按周末推算（标 ~，与「我的持仓」的到期日同一算法）
      const cal = new Map<string, number>()
      curveDates.forEach((d, i) => cal.set(d, i))
      for (const r of openRows) {
        const ev = winEvents.find((e) => e.k === 'B' && e.date === r.buyDate)
        const i = cal.get(r.buyDate)
        if (!ev?.h || i == null) continue
        const j = i + ev.h
        if (j < curveDates.length) r.endDate = curveDates[j]
        else {
          r.endDate = addWeekdays(curveDates[curveDates.length - 1], j - (curveDates.length - 1))
          r.endEst = true
        }
      }
    }
    return [...trades, ...openRows]
      .sort((a, b) => (a.buyDate === b.buyDate ? 0 : (a.buyDate < b.buyDate ? 1 : -1)))
  }, [trades, winEvents, lastAdj, money, curveDates])

  // ETF 自身累计涨跌（复权口径）：引擎页 = 数据起点 → 最新；账户页 = 入市起始日 → 选中日
  const etfTotal = useMemo(
    () => (data ? seriesTotal(data.adj, data.dates, scoped ? scope?.from : undefined,
      scoped ? scope?.to : undefined) : null),
    [data, scoped, scope?.from, scope?.to],
  )

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
    for (const e of winEvents) {
      const d = e.k === 'B' ? e.date : e.date
      if (d) (evByDate[d] = evByDate[d] || []).push(e)
    }
    const brkByDate: Record<string, any> = {}
    for (const b of data.breaks) brkByDate[b.date] = b

    // 买入点拆两类：结论驱动（红 B）与抄底兜底（琥珀「抄」）——后者非结论驱动，必须一眼可辨
    const isDip = (e: SidEvent) => e.k === 'B' && e.a === 'DIP'
    const bPts = winEvents.filter((e) => e.k === 'B' && !isDip(e) && idxOf[e.date!] !== undefined)
      .map((e) => ({ value: [e.date, data.adj[idxOf[e.date!]]], date: e.date }))
    const dipPts = winEvents.filter((e) => isDip(e) && idxOf[e.date!] !== undefined)
      .map((e) => ({ value: [e.date, data.adj[idxOf[e.date!]]], date: e.date }))
    const sPts = winEvents.filter((e) => e.k === 'S' && idxOf[e.date!] !== undefined)
      .map((e) => ({ value: [e.date, data.adj[idxOf[e.date!]]], date: e.date }))
    const brkPts = data.breaks.filter((b) => idxOf[b.date] !== undefined)
      .map((b) => ({ value: [b.date, data.adj[idxOf[b.date]]], date: b.date }))

    // 初始视窗：focusDate（复盘页点击定位）为主；否则默认看最新时间段
    // （末端往前 W 个交易日）——不落到第一处折算日，避免默认打开是历史段。
    // 「我的持仓」带统计窗口时，默认落在窗口内（窗口比 W 长就取窗口末端往前 W）。
    const nD = data.dates.length
    const fi = focusDate && idxOf[focusDate] !== undefined ? idxOf[focusDate] : null
    const W = 240
    let z0: number, z1: number
    if (fi != null) {
      z0 = Math.max(0, fi - W) / (nD - 1) * 100
      z1 = Math.min(nD - 1, fi + W) / (nD - 1) * 100
    } else if (scoped) {
      const atOrAfter = (d?: string) => {
        if (!d) return 0
        const i = data.dates.findIndex((x) => x >= d)
        return i < 0 ? nD - 1 : i
      }
      const atOrBefore = (d?: string) => {
        if (!d) return nD - 1
        let i = -1
        for (let j = 0; j < nD; j++) if (data.dates[j] <= d) i = j
        return i < 0 ? 0 : i
      }
      const i0 = atOrAfter(scope?.from)
      const i1 = Math.max(i0, atOrBefore(scope?.to))
      const a = Math.max(i0, i1 - W)
      z0 = Math.max(0, a) / (nD - 1) * 100
      z1 = i1 / (nD - 1) * 100
    } else {
      z0 = Math.max(0, nD - 1 - W) / (nD - 1) * 100
      z1 = 100
    }

    // 与复盘页「真实交易结算（ETF）」走势图同一套悬浮样式：外层必须包 .ektip，
    // 否则 .ektip .ek-* 那组排版规则（键值左右分栏、灰色标签、分隔线、等宽数字）全都不生效。
    const tipKV = (k: string, v: string, tone?: 'up' | 'down' | 'warn') => {
      const cls = tone === 'up' ? 'ek-up' : tone === 'down' ? 'ek-down' : tone === 'warn' ? 'ek-dim warn' : ''
      return `<div class="ek-kv"><span>${k}</span><b${cls ? ` class="${cls}"` : ''}>${v}</b></div>`
    }

    ch.setOption({
      animation: false,
      grid: { left: 62, right: 62, top: 34, height: 240 },
      legend: {
        top: 2, left: 54, itemWidth: 14, itemHeight: 8, itemGap: 14,
        textStyle: { fontSize: 11, color: '#4b5563' },
        data: ['复权连续价（结算口径）', '原始市价（行情软件口径）', '买入 B', '抄底 DIP', '卖出 S', '份额折算日'],
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
          // ---- 内容同样对齐复盘页走势图：ek-h 表头 + ek-kv 键值 + ek-sec 分段 + ek-mute 备注 ----
          // 表头只放日期：标的名称在弹窗标题里已有，重复一遍既冗余又把行撑长
          let html = `<div class="ek-h">${date}</div>`
          html += tipKV('原始市价', raw != null ? raw.toFixed(3) : '停牌/无数据')
          html += tipKV('复权连续价', adj != null ? adj.toFixed(4) : '—')
          const ni = navIdxOf[date]
          if (ni != null && curveNav[ni] != null) {
            html += curveIsMoney
              ? tipKV('账户总值', `¥${Math.round(curveNav[ni]).toLocaleString('zh-CN')}`)
              : tipKV('组合净值', curveNav[ni].toFixed(4))
          }
          const brk = brkByDate[date]
          if (brk) {
            html += `<div class="ek-sec">份额折算日 ⚠</div>`
            html += tipKV('原始价格跳变', `${(brk.raw_ret * 100).toFixed(2)}%`, 'warn')
            html += tipKV('参照真实收益', `${(brk.ref_ret * 100).toFixed(2)}%（${REF_TXT[brk.ref_src] || brk.ref_src}）`)
            html += tipKV('复权因子', `×${brk.factor.toFixed(4)}`)
            html += `<div class="ek-mute">价值中性事件：持有人份额×价格不变，不应记为盈亏。</div>`
          }
          const evs = evByDate[date] || []
          for (const e of evs) {
            const isB = e.k === 'B'
            const sec = !isB ? '卖出 S' : e.a === 'DIP' ? '抄底 DIP' : '买入 B'
            const actTag = e.a && e.a !== 'DIP' ? `（${ACT_TXT[e.a] || e.a}）` : ''
            html += `<div class="ek-sec">${sec} · ${e.sw}${actTag}</div>`
            html += tipKV(isB ? '买入价（T+1开盘）' : '卖出价（到期收盘）', (e.price ?? 0).toFixed(4))
            if (!isB) {
              const tone = (x: number | null | undefined) => (x == null ? undefined : x >= 0 ? 'up' as const : 'down' as const)
              html += tipKV('毛收益率', e.gross != null ? pct(e.gross, 2) : '—', tone(e.gross))
              html += tipKV('净收益率（扣双边成本）', e.ret != null ? pct(e.ret, 2) : '—', tone(e.ret))
              // 相对基准超额只有引擎事件有（本账户的重放不带基准序列），没算就不显示
              if (e.ex != null) html += tipKV('相对基准超额', pct(e.ex, 2), tone(e.ex))
            }
            // 组合占比：引擎已把「未归一化权重 ÷ 当日 Σw × 当日暴露」算好（settlement.py 的 w_share），
            // 这里直接给结果，不再显示公式——原始权重 w 是中间量，用户看它没有意义。
            if (e.w_share != null) html += tipKV('组合占比', `${(e.w_share * 100).toFixed(2)}%`)
            if (isB && e.h) html += tipKV('建议周期', `${e.h} 个交易日`)
            if (isB && e.sc != null) html += tipKV('综合分', String(e.sc))
            if (isB && e.a === 'DIP') {
              html += `<div class="ek-mute">抄底兜底：主策略当日零候选 + 该行业超跌且已止跌回升，`
              html += `非结论驱动（见 docs/抄底兜底通道.md）。</div>`
            }
            if (!isB && e.why) html += `<div class="ek-mute">卖出原因：${e.why}</div>`
          }
          return `<div class="ektip">${html}</div>`
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
          name: '抄底 DIP', type: 'scatter', data: dipPts, z: 6, symbolSize: 16,
          symbol: 'diamond',
          itemStyle: { color: C_DIP, borderColor: '#fff', borderWidth: 1.4 },
          label: { show: true, formatter: '抄', color: '#fff', fontSize: 9, fontWeight: 700, position: 'inside' },
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
  }, [data, winEvents, curveDates, curveNav, navIdxOf, focusDate, scoped, scope?.from, scope?.to,
      curveIsMoney])

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

        {/* 口径声明：「我的持仓」打开的弹窗按你的入市起始日统计，与复盘页的全历史口径不同 */}
        {data && (scoped || money) && (
          <div className="note" style={{ marginTop: 0, marginBottom: 8 }}>
            <b>「我的持仓」口径统计</b>：入市起始日 {scope?.from || '数据起点'}
            {scope?.capital ? `（本金 ${scope.capital.toLocaleString('zh-CN')} 元）` : ''}
            {scope?.to ? ` → 盯市至 ${scope.to}` : ''}。
            下方 ETF 涨跌、已结算/在途统计、B/S 成交点与账本<b>都只算这本账</b>
            （金额是真实人民币），不含入市之前的引擎回测；走势图仍画该标的的完整历史，可自行缩放。
          </div>
        )}

        {/* 到目前为止的总收益摘要：ETF 自身累计 + 系统交易该 ETF 的账本合计 + 在途持仓 */}
        {data && (etfTotal || settled || openPos.length > 0) && (
          <div className="etf-stats">
            {etfTotal && (
              <div className="estat">
                <div className="l">{scoped ? '入市以来 ETF 涨跌（复权）' : 'ETF 累计涨跌（复权）'}</div>
                <div className={`v num ${etfTotal.pct >= 0 ? 'up' : 'down'}`}>{pct(etfTotal.pct, 2)}</div>
                <div className="m">{etfTotal.from} → {etfTotal.to}
                  {!scoped
                    ? '（数据起止）'
                    : etfTotal.to === scope?.to
                      ? '（入市起始日 → 选中日）'
                      // 该标的在选中日没有行情（停牌/退市/数据缺口）时如实说明，
                      // 不把"最后一个行情日"含糊地写成"选中日"
                      : `（入市起始日 → 该标的最后一个行情日；选中日 ${scope?.to} 无该标的行情）`}
                </div>
              </div>
            )}
            {settled && (
              <div className="estat">
                <div className="l">{settled.money ? `本账户已结算 ${settled.n} 笔` : `系统已结算 ${settled.n} 笔`}</div>
                <div className={`v num ${settled.pnl >= 0 ? 'up' : 'down'}`}>
                  {settled.money ? yuanSigned(settled.pnl) : `${settled.pnl >= 0 ? '+' : ''}${settled.pnl.toFixed(0)} 元`}
                </div>
                <div className="m">
                  胜率 {pct(settled.winRate, 2, false)} · 单笔平均 {pct(settled.avg, 2)} ·{' '}
                  {settled.money
                    ? `累计投入成本 ${yuan(settled.cost)}（已实现盈亏为真钱）`
                    : '名义 1 万/笔'}
                </div>
              </div>
            )}
            {openPosAgg && (
              <div className="estat">
                <div className="l">
                  {openPosAgg.money ? `本账户在途持仓 · ${openPosAgg.n} 笔（未平仓）` : `在途持仓 · ${openPosAgg.n} 笔（未平仓）`}
                </div>
                <div className={`v num ${openPosAgg.pnl >= 0 ? 'up' : 'down'}`}>
                  {openPosAgg.money ? yuanSigned(openPosAgg.pnl) : `${openPosAgg.pnl >= 0 ? '+' : ''}${openPosAgg.pnl.toFixed(0)} 元`}
                </div>
                <div className="m">
                  {openPosAgg.money
                    ? `浮动 ${pct(openPosAgg.avg, 2)} · 成本 ${yuan(openPosAgg.cost)} → 市值 ${yuan(openPosAgg.mv)}（未扣卖出成本）`
                    : `平均浮动 ${pct(openPosAgg.avg, 2)} · 名义 1 万/笔`} · 买入 {openPosAgg.from} ~ {openPosAgg.to} ·
                  估值截至 {lastAdj?.d}
                </div>
              </div>
            )}
          </div>
        )}

        {data && (
          <>
            <div ref={box} style={{ width: '100%', height: 340 }} />

            {data.breaks.length > 0 && (
              <div className="note" style={{ borderLeft: `3px solid ${C_BRK}`, paddingLeft: 10, margin: '10px 0' }}>
                <b>该标的发生过 {data.breaks.length} 次「份额折算」（拆分/合并）</b>——
                折算日单位价格按整数倍跳变（图中原始市价的断崖），但持有人份额同步反向变化、总价值不变。
                结算用「复权连续价」消除这种价值中性跳变（红线上无断点）：
                {data.breaks.map((b, i) => (
                  <span key={i}>{i > 0 ? '、' : ' '}{b.date} ×{b.factor.toFixed(4)}
                    （{REF_TXT[b.ref_src] || b.ref_src}参照）</span>
                ))}
                ；<b>复权价 = 原始市价 × 当日适用的累计因子</b>——多次折算要连乘，
                最近一次之后为 ×{data.breaks.reduce((a, b) => a * b.factor, 1).toFixed(4)}。
                收益按复权价算才正确。
              </div>
            )}

            <div className="tradescroll" style={{ marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>买入日</th>
                    <th style={{ textAlign: 'right' }}>买入价*</th>
                    <th style={{ textAlign: 'right' }}>{money ? '份数' : '份额 (1万元)'}</th>
                    {money && <th style={{ textAlign: 'right' }}>买入金额</th>}
                    <th>卖出日</th>
                    <th style={{ textAlign: 'right' }}>卖出价*</th>
                    <th style={{ textAlign: 'right' }}>卖出金额</th>
                    <th style={{ textAlign: 'right' }}>毛收益率</th>
                    <th style={{ textAlign: 'right' }}>成本</th>
                    <th style={{ textAlign: 'right' }}>净收益率</th>
                    <th style={{ textAlign: 'right' }}>{money ? '已实现 / 浮动盈亏' : 'ret 对账 / 浮动'}</th>
                    <th>卖出原因</th>
                  </tr>
                </thead>
                <tbody>
                  {ledger.map((t, i) => {
                    // 名义账本（引擎）：每笔固定 1 万元本金；本账户：用真实成交份数与金额
                    const shares = money ? t.shBuy : (t.bpx ? NOTIONAL / t.bpx : null)
                    const sellAmt = money
                      ? t.proceeds
                      : (shares != null && t.spx != null ? shares * t.spx : null)
                    const net = t.gross != null ? t.gross - COST_RT : null
                    const chk = net != null && t.ret != null ? Math.abs(net - t.ret) : null
                    // 在途行：卖出日/价/金额都是「按窗口末端收盘价估值」的假想值，逐格标注
                    const est = (
                      <span className="muted" style={{ fontSize: 10, marginLeft: 3 }}
                            title={`按 ${lastAdj?.d} 收盘价估值，不是成交价`}>估</span>
                    )
                    return (
                      <tr key={i} className={t.open ? 'ledger-open' : ''}>
                        <td className="num">
                          {t.buyDate}
                          {t.act === 'DIP' && (
                            <Chip tone="warn" title="抄底兜底：非结论驱动（主策略零候选 + 超跌回升）">抄底</Chip>
                          )}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>{num(t.bpx, 4)}</td>
                        <td className="num" style={{ textAlign: 'right' }}>{shares != null ? num(shares, 2) : '—'}</td>
                        {money && (
                          <td className="num" style={{ textAlign: 'right' }}>
                            {t.amtBuy != null ? yuan(t.amtBuy) : '—'}
                          </td>
                        )}
                        <td className="num">
                          {t.open
                            ? <Chip tone="mute" title={`未平仓：到期日约 ${t.endDate || '—'}${t.endEst ? '（按周末推算，未计节假日）' : ''}`}>在途</Chip>
                            : t.sellDate}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>
                          {num(t.spx, 4)}{t.open && est}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>
                          {sellAmt != null ? sellAmt.toFixed(2) : '—'}{t.open && est}
                        </td>
                        <td className="num" style={{ textAlign: 'right', color: (t.gross ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
                          {pct(t.gross, 2)}{t.open && <span className="muted" style={{ fontSize: 10, marginLeft: 3 }}>浮动</span>}
                        </td>
                        <td className="num" style={{ textAlign: 'right' }}>{t.open ? '—' : '-0.14%'}</td>
                        <td className="num" style={{ textAlign: 'right', color: t.open ? 'inherit' : ((net ?? 0) >= 0 ? 'var(--up)' : 'var(--down)') }}>
                          {t.open ? '—' : pct(net, 2)}
                        </td>
                        <td className={`num ${(t.open ? t.floatPnl : money ? t.pnl : t.ret) != null
                          ? ((t.open ? t.floatPnl : money ? t.pnl : t.ret) as number) >= 0 ? 'up' : 'down' : ''}`}
                            style={{ textAlign: 'right' }}>
                          {t.open ? (
                            // 在途行没有已实现盈亏：给"按当前价的浮动盈亏"，金额与比例都标出来
                            <span title={`未平仓，按 ${lastAdj?.d} 收盘价估值，未扣卖出成本`}>
                              <b>{money
                                ? (t.floatPnl != null ? yuanSigned(t.floatPnl) : '—')
                                : (t.gross != null ? `${t.gross >= 0 ? '+' : '−'}¥${Math.abs(t.gross * NOTIONAL).toFixed(0)}` : '—')}</b>
                              <span className="muted" style={{ fontSize: 10, marginLeft: 4 }}>浮动</span>
                            </span>
                          ) : money ? (
                            <b>{t.pnl != null ? yuanSigned(t.pnl) : '—'}</b>
                          ) : (
                            <>
                              {pct(t.ret, 2)}
                              {chk != null && (
                                // ret 在结算层四舍五入到 5 位小数，与前端重算的最大自然差 ~5e-6
                                <span className="muted" style={{ fontSize: 10, marginLeft: 4 }}>
                                  {chk < 1e-5 ? '✓' : `Δ${(chk * 100).toFixed(3)}`}
                                </span>
                              )}
                            </>
                          )}
                        </td>
                        <td className="muted" style={{ fontSize: 11.5 }}>
                          {t.open
                            ? `未平仓 · 到期日 ${t.endEst ? '~' : ''}${t.endDate || '—'}`
                            : (t.why || '—')}
                        </td>
                      </tr>
                    )
                  })}
                  {!ledger.length && (
                    <tr>
                      <td colSpan={money ? 12 : 11} className="muted">
                        {money
                          ? '这本账在该标的上没有任何成交（你还没跟到它的买点）'
                          : '该标的在结算区间内没有任何成交'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {money ? (
              <div className="note" style={{ marginTop: 8 }}>
                * 价格为<b>复权连续价</b>（结算取价口径）。份额折算日之后，复权价 = 原始市价 × 折算因子；
                与行情软件核对请看图中灰色虚线（原始市价）。本表是<b>这本账的全部买入</b>，每笔一行：
                买入金额 = 建仓时实际付出的现金（= min(预算, 可用现金)，受你的本金与入市起始日约束），
                卖出金额 = 份数 × 到期日收盘价 − 双边成本；
                净收益率 = 毛收益率 − 0.14%，<b>已实现盈亏 = 卖出金额 − 买入金额</b>（真钱，单位：元）。
                <br />
                <b>「在途」行 = 已经买入、还没卖出的持仓</b>（到建议周期结束才卖）：
                卖出日一栏是到期日，卖出价/卖出金额/毛收益率都打「估」字——
                按 {lastAdj?.d} 收盘价估的值，不是成交；成本与净收益率留空，最后一列给的是
                <b>浮动盈亏</b>（未扣卖出成本），与上方「本账户在途持仓」卡一致。
              </div>
            ) : (
              <div className="note" style={{ marginTop: 8 }}>
                * 价格为<b>复权连续价</b>（结算取价口径）。份额折算日之后，复权价 = 原始市价 × 折算因子；
                与行情软件核对请看图中灰色虚线（原始市价）。账本：名义本金 {NOTIONAL.toLocaleString()} 元 →
                份额 = 本金 ÷ 买入价 → 卖出金额 = 份额 × 卖出价；毛收益率 = 卖出价 ÷ 买入价 − 1；
                成本 0.14% = 双边佣金万二 + 双边滑点万五；净收益率 = 毛收益率 − 0.14%，
                <b>「结算 ret 对账」列应与净收益率逐位一致（✓）</b>——这一列就是"钱算得对不对"的直接检验。
                <br />
                <b>「在途」行 = 已建仓、还没到期的买入</b>（结算层还没产生 S 事件）：
                卖出日/价/金额按 {lastAdj?.d} 收盘价估值（打「估」字），最后一列是名义浮动盈亏；
                它不出现在上面的「系统已结算」统计里。
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
