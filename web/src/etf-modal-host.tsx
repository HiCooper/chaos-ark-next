import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { EtfModal, SidEvent, EtfScope } from './etf-modal'

export type OpenOpts = {
  /** 本次要画的 B/S 事件（不给则用结算层 events 里该 sid 的全部事件） */
  events?: SidEvent[]
  /** 统计窗口（我的持仓：入市起始日 → 选中日） */
  scope?: EtfScope
  /** 用本账户的资金曲线替换组合净值（我的持仓：元） */
  curve?: { dates: string[]; nav: number[] }
}

/**
 * 全局唯一的「标的走势核验弹窗」宿主。
 *
 * 复盘页（逐笔明细 / 折算样本）与今日研判页（可直接交易标的 / 在途持仓 / 行业全景表）
 * 都可能出现 ETF，点击即弹出同一套走势核验弹窗。任何页面：
 *   const { open, sidOf } = useEtfModal()
 *   open(sidOf(etfCode) ?? '', focusDate?)   // sidOf：6 位代码 → sid（如 159516 → 0.159516）
 *
 * 「我的持仓」页是**例外**：那本账是按你自己的本金与入市起始日重放的，
 * 与引擎名义账本的买点会分叉。所以该页打开时要带上自己的成交与统计窗口：
 *   open(sid, focusDate, { events: bookEvents(acct, sid), scope, curve })
 *
 * sid 解析优先用 view/etf/_index.json（build_etf_webdata.py 生成的**展示层**切片，
 * 覆盖全池 ETF，含从未成交的）；接口缺失时退回结算事件里出现过的 sid。
 *
 * 另外提供两个**只用当日及之前数据**的口径查询（历史页面不随新交易日变化）：
 *   · investedRatio(date)：当日**实际投出比例**（持仓市值 ÷ 账户总值，可执行账户口径）；
 * 每条结论「当日在容量 + 每日新建名额下排第几位」由页面自己用 day 文件里的
 * action_today 现算（见 Today.tsx）：刻意**不**回答「T+1 到底建仓了没有」——那是未来数据，
 * 会让历史页面随增量更新而变（同一份 day/<date>.json 渲染出不同结果），口径上也不该前置。
 */

type EtfModalCtx = {
  /**
   * 打开走势核验弹窗。
   *   open(sid, focusDate?)                     —— 默认：引擎名义账本口径（复盘页/今日研判页）
   *   open(sid, focusDate, { events, scope, curve })
   *                                             —— 覆盖：本账户口径（我的持仓页）
   * opts.events 直接给出要画的 B/S（我的持仓按入市起始日重放出的真账）；
   * opts.scope 描述统计窗口（起始日/截止日/本金/是否真实金额）；
   * opts.curve 用账户自己的资金曲线替换「组合净值」（我的持仓：元）。
   */
  open: (sid: string, focusDate?: string, opts?: OpenOpts) => void
  /** 6 位 ETF 代码 → sid（"0.159516"）；解析不到返回 null */
  sidOf: (code?: string | null) => string | null
  /** 某交易日结算层的实际投出比例（持仓市值 ÷ 账户总值）；取不到返回 null */
  investedRatio: (date?: string) => number | null
  /** 每日最多新建几笔（config.PORTFOLIO.max_new_per_day，前端据此判断当日排队位置） */
  maxNewPerDay: number
  /** 未投入部分的现金年化（config.RISK_CONTROL.cash_yield），页面文案不写死 */
  cashYield: number
  /** sid（"1.512400"）→ { name, sw }；取不到返回 null */
  metaOf: (sid?: string | null) => { name: string; sw?: string } | null
  /** 模型净值倍数 nav[to] / nav[from]（from 为空 = 数据起点）；用于把本金放大到当前账户总值 */
  navMultiple: (from?: string, to?: string) => number | null
  /** 两个交易日之间的交易日数（b − a） */
  daysBetween: (a?: string, b?: string) => number | null
  /** 上一交易日（用于取「当日到期/卖出」的持仓） */
  prevTradingDay: (date?: string) => string | null
}

const Ctx = createContext<EtfModalCtx>({
  open: () => {},
  sidOf: () => null,
  investedRatio: () => null,
  maxNewPerDay: 2,
  cashYield: 0.01,
  metaOf: () => null,
  navMultiple: () => null,
  daysBetween: () => null,
  prevTradingDay: () => null,
})

export function useEtfModal() {
  return useContext(Ctx)
}

export function EtfModalHost({ settlement, children }: { settlement: any; children: React.ReactNode }) {
  const [target, setTarget] = useState<({ sid: string; focusDate?: string } & OpenOpts) | null>(null)
  // view/etf/_index.json：code → {sid, name, sw}，覆盖面板全部 ETF（展示层切片）
  const [index, setIndex] = useState<Record<string, { sid: string; name: string }> | null>(null)

  useEffect(() => {
    fetch('view/etf/_index.json', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => setIndex(j.etfs || {}))
      .catch(() => setIndex(null))
  }, [])

  const allDates: string[] = settlement?.curve?.dates || []
  const allNav: number[] = settlement?.curve?.nav || []
  const events: any[] = settlement?.events || []
  // 每日新建上限 + 现金收益率（config 口径，展示用）
  const maxNewPerDay: number = settlement?.portfolio?.max_new_per_day ?? 2
  const cashYield: number = settlement?.risk_control?.config?.cash_yield ?? 0.01
  // 逐日实际投出比例（账户口径）：由现金账逐日记账产生，与资金曲线同源
  const exposureSeries: number[] = settlement?.position?.exposure || []

  const dateIdx = useMemo(() => {
    const m: Record<string, number> = {}
    allDates.forEach((d, i) => { m[d] = i })
    return m
  }, [allDates])

  // sid → {name, sw}：账本只存 sid，展示时要查名字
  const metaBySid = useMemo(() => {
    const m = new Map<string, { name: string; sw?: string }>()
    for (const v of Object.values(index || {})) m.set(v.sid, { name: v.name, sw: (v as any).sw })
    return m
  }, [index])

  // 结算事件里出现过的代码 → sid（_index.json 加载失败时的兜底）
  const fallbackByCode = useMemo(() => {
    const m: Record<string, string> = {}
    for (const e of events) {
      if (e.sid) m[String(e.sid).split('.')[1]] = e.sid
    }
    return m
  }, [events])

  const ctx = useMemo<EtfModalCtx>(() => {
    const sidOf = (code?: string | null) => {
      const c = code ? String(code).split('.').pop()! : ''
      if (!c) return null
      return index?.[c]?.sid ?? fallbackByCode[c] ?? null
    }
    return {
      open: (sid: string, focusDate?: string, opts?: OpenOpts) => {
        if (sid) setTarget({ sid, focusDate, ...(opts || {}) })
      },
      sidOf,
      investedRatio: (date?: string) => {
        const i = date != null ? dateIdx[date] : undefined
        if (i == null) return null
        const v = exposureSeries[i]
        return typeof v === 'number' ? v : null
      },
      maxNewPerDay,
      cashYield,
      metaOf: (sid?: string | null) => (sid ? metaBySid.get(sid) ?? null : null),
      navMultiple: (from?: string, to?: string) => {
        if (!allNav.length) return null
        // 非交易日（周末/停市）顺延到下一个交易日；都找不到就退回端点
        const resolve = (d: string | undefined, fallback: number) => {
          if (!d) return fallback
          const exact = dateIdx[d]
          if (exact != null) return exact
          const i = allDates.findIndex((x) => x >= d)
          return i < 0 ? fallback : i
        }
        const iF = resolve(from, 0)
        const iT = resolve(to, allDates.length - 1)
        const a = allNav[iF], b = allNav[iT]
        return a > 0 && b > 0 ? b / a : null
      },
      daysBetween: (a?: string, b?: string) => {
        const i0 = a != null ? dateIdx[a] : undefined
        const i1 = b != null ? dateIdx[b] : undefined
        return i0 == null || i1 == null ? null : i1 - i0
      },
      prevTradingDay: (date?: string) => {
        const i = date != null ? dateIdx[date] : undefined
        return i != null && i > 0 ? allDates[i - 1] : null
      },
    }
  }, [index, fallbackByCode, dateIdx, exposureSeries, maxNewPerDay, cashYield,
      metaBySid, allNav, allDates])

  // 选定标的的全部 B/S 事件（B 事件本身不带日期，按曲线索引补上）
  const sidEvents = useMemo(() => {
    if (!target) return [] as SidEvent[]
    // 调用方直接给了事件（我的持仓：本账户重放出的真账）→ 原样使用
    if (target.events) return target.events
    const out: SidEvent[] = []
    for (const e of events) {
      if (e.sid !== target.sid) continue
      const date = e.date ?? (e.i != null ? allDates[e.i] : undefined)
      out.push({ ...e, date })
    }
    return out
  }, [target, events, allDates])

  return (
    <Ctx.Provider value={ctx}>
      {children}
      {target && (
        <EtfModal
          sid={target.sid}
          focusDate={target.focusDate}
          curveDates={target.curve?.dates ?? allDates}
          curveNav={target.curve?.nav ?? allNav}
          curveIsMoney={!!target.curve}
          events={sidEvents}
          scope={target.scope}
          onClose={() => setTarget(null)}
        />
      )}
    </Ctx.Provider>
  )
}
