/**
 * 「我的持仓」影子账户（档 2）：把引擎的资金记账规则在浏览器里重放一遍。
 *
 * 引擎（engine/settlement.py: simulate_cash_account）的账户没有每日再平衡：
 *   · 只有一笔本金，之后**卖出所得是唯一资金来源**；
 *   · 建仓在信号日 T 的下一交易日 T+1 **开盘**，金额 = min(预算, 可用现金)；
 *   · 预算 = 昨收账户总值 × 建仓闸门 × (wᵢ / w̄) / K，w = (1/σ)×(综合分/50)^tilt；
 *   · 卖出只在持有到期（T+1+H 个交易日）当日收盘发生，所得现金**次日**才可用；
 *   · 未投入现金按 cash_yield 逐日计息；持仓权重随价格漂移、不再拉回目标；
 *   · 成本按开仓名义额在卖出时一次性扣除（佣金+滑点，双边）。
 *
 * 因此本页给出的不是"按目标权重折算的应有金额"，而是**这本账真实的样子**：
 * 现金余额、逐笔成本与市值、浮盈、已实现收益。行情与候选全部来自引擎导出的
 * data/sim.json（候选权重 w 已用 ≤T 的数据算好），前端不做任何行情计算，
 * 也**不会用到选中日之后的数据**。
 *
 * 与实盘的已知差异（页面上同样写明）：
 *   · 引擎按份数（可小数）记账，实盘需按 100 份/手取整 → 金额小的时候有零头误差；
 *   · 未计 ETF 管理费/托管费（场内 ETF 已在净值里扣除），未计分红再投差异；
 *   · 实盘成交价是"你的开盘价"，与引擎记录的开盘价可能有滑点差异。
 */

export type SimPick = [
  string,            // 行业名
  string,            // sid
  number,            // 建仓权重 w
  number,            // 综合分
  string | null,     // 到期日（null = 未定/超出数据末端）
  number,            // 周期（交易日）
  string | null,     // 动作 BUY_NOW / AMBUSH
]

export type SimDay = { e: number; p: SimPick[] }

export type SimData = {
  version: string | null
  as_of: string
  dates: string[]
  K: number
  max_holdings: number
  max_new_per_day: number
  cost: number
  cash_yield: number
  days: Record<string, SimDay>
  px: { o: Record<string, (number | null)[]>; c: Record<string, (number | null)[]> }
  meta: Record<string, { n: string }>
}

export type Position = {
  sw: string
  sid: string
  name: string
  action: string | null
  w: number
  score: number
  horizon: number
  buyDate: string
  buyI: number
  buyPx: number
  shares: number
  /** 买入时实际付出的现金 */
  amt: number
  endDate: string | null
  /** 到期日在数据末端之后 → 按周末推算（未计节假日） */
  endEstimated: boolean
  endI: number
  lastPx: number
  mv: number
  pnl: number
  pnlPct: number
}

export type SellRow = {
  sw: string; sid: string; name: string
  buyDate: string; buyPx: number; sellPx: number; sellDate: string
  shares: number; amt: number; proceeds: number
  /** 已实现盈亏（已扣成本） */
  pnl: number
  ret: number
  heldDays: number
  horizon: number
}

export type PlanRow = {
  sw: string; sid: string; name: string; action: string | null
  w: number; score: number; horizon: number
  /** 下一交易日开盘的预算金额（= min(预算, 可用现金)） */
  amount: number
  /** 预算被现金约束削减的比例 0~1（0 = 不缺钱） */
  cashCut: number
  lots: number
}

/** 下一交易日收盘到期的持仓（要卖的单；金额按最新收盘价估算，实际以成交价为准） */
export type SellPlanRow = {
  sw: string; sid: string; name: string
  buyDate: string; buyPx: number; lastPx: number; shares: number; amt: number
  estProceeds: number; estPnl: number; estRet: number
  heldDays: number; horizon: number; endDate: string | null; endEstimated: boolean
}

export type SimResult = {
  startDate: string
  asOf: string
  asOfI: number
  /** 下一交易日：数据内为真实日期；数据末端之后按周末推算（节假日以交易所公告为准） */
  nextDate: string | null
  nextDateEstimated: boolean
  window: { d: string; v: number }[]
  capital: number
  cash: number
  mv: number
  value: number
  totalPnl: number
  floatPnl: number
  realised: number
  fees: number
  peak: number
  maxdd: number
  exposure: number
  positions: Position[]
  todayBuys: Position[]
  todaySells: SellRow[]
  /** 今天这批买单的来源（= 上一交易日的结论日）；为空表示起始日之前 */
  todaySignalDate: string | null
  /** 今天没买成的原因（有结论但没建仓时才非空） */
  todayBlocked: 'no_candidates' | 'no_slots' | 'no_cash' | null
  nextBuys: PlanRow[]
  /** 排队候选：当前建不了仓（槽位满/现金不足）时的优先级队列与"若有槽位时的预算" */
  nextQueue: PlanRow[]
  nextSells: SellPlanRow[]
  /** 下一交易日没有买入时的原因（用于把"为什么不用动手"说清楚） */
  nextBlocked: 'no_candidates' | 'no_slots' | 'no_cash' | null
  nBuys: number
  nSells: number
  /** 因现金不足被缩量/跳过的建仓次数（可执行口径的真实约束） */
  cashBlocked: number
  /** 因容量（槽位）不足被挡掉的候选笔数 */
  slotBlocked: number
}

const cache: { data?: Promise<SimData | null> } = {}

export function fetchSim(): Promise<SimData | null> {
  if (!cache.data) {
    cache.data = fetch('data/sim.json')
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
  }
  return cache.data
}

/** 按周末推算 +n 个工作日（不含节假日；用于数据末端之后的到期日/下一交易日） */
export function addWeekdays(iso: string, n: number): string {
  const d = new Date(iso + 'T00:00:00Z')
  let left = n
  while (left > 0) {
    d.setUTCDate(d.getUTCDate() + 1)
    const w = d.getUTCDay()
    if (w !== 0 && w !== 6) left--
  }
  return d.toISOString().slice(0, 10)
}

export function yuan(x: number): string {
  return `¥${Math.round(x).toLocaleString('zh-CN')}`
}

/** 与引擎一致：建仓后各笔权重随价格漂移；这里只做记账，不做任何再平衡。 */
export function runSim(
  sim: SimData, opts: { capital: number; startDate?: string; asOf?: string },
): SimResult {
  const dates = sim.dates
  const idxOf: Record<string, number> = {}
  dates.forEach((d, i) => { idxOf[d] = i })
  const capital = Math.max(0, Number(opts.capital) || 0)
  const K = Math.max(1, Number(sim.K) || Number(sim.max_holdings) || 1)
  const COST = Number(sim.cost) || 0
  const rf = (Number(sim.cash_yield) || 0) / 252
  const maxNew = Number(sim.max_new_per_day) > 0 ? Number(sim.max_new_per_day) : Infinity

  // 起始日：非交易日顺延到下一个交易日（与引擎同规则，绝不静默退回全历史）
  let sI = 0
  if (opts.startDate) {
    const hit = idxOf[opts.startDate]
    if (hit !== undefined) sI = hit
    else {
      const nxt = dates.findIndex((d) => d >= (opts.startDate as string))
      sI = nxt >= 0 ? nxt : dates.length - 1
    }
  }
  const startK = Math.max(sI, 1)
  let asOfI = dates.length - 1
  if (opts.asOf && idxOf[opts.asOf] !== undefined) asOfI = idxOf[opts.asOf]

  const openOf = (sid: string, i: number): number | null => {
    const a = sim.px.o[sid]
    return a ? (a[i] ?? null) : null
  }
  const closeOf = (sid: string, i: number): number | null => {
    const a = sim.px.c[sid]
    return a ? (a[i] ?? null) : null
  }
  const nameOf = (sid: string) => sim.meta[sid]?.n || sid

  let cash = capital
  let holdings: Position[] = []
  const window: { d: string; v: number }[] = [
    { d: dates[Math.max(0, startK - 1)], v: capital },
  ]
  // 只保留**选中日当天**的成交（逐日重置，避免把整段重放的成交都堆在这里）
  let todayBuys: Position[] = []
  let todaySells: SellRow[] = []
  let realised = 0, fees = 0, nBuys = 0, nSells = 0, cashBlocked = 0, slotBlocked = 0
  let peak = capital, maxdd = 0
  let todaySignalDate: string | null = null
  let todayCands = 0, todaySlotsFree = 0

  for (let k = startK; k <= asOfI; k++) {
    const d = dates[k]
    todayBuys = []
    todaySells = []
    const Vprev = window[window.length - 1].v
    const day = k - 1 >= sI ? sim.days[dates[k - 1]] : undefined
    const expo = day ? day.e : 1.0

    // ① 开盘建仓：只用昨收现金；当日到期的持仓当日开盘仍占槽位
    let adds = day ? [...day.p].sort((a, b) => b[3] - a[3]).slice(0, maxNew) : []
    const slotsFree = Math.max(0, sim.max_holdings - holdings.length)
    if (adds.length > slotsFree) slotBlocked += adds.length - slotsFree
    if (k === asOfI) {                   // 选中日：记录当天买单的来源与约束
      todaySignalDate = k - 1 >= sI ? dates[k - 1] : null
      todayCands = day ? day.p.length : 0
      todaySlotsFree = slotsFree
    }
    adds = adds.slice(0, slotsFree)
    if (adds.length) {
      const ws = holdings.map((h) => h.w).concat(adds.map((p) => p[2]))
      const wbar = ws.length ? ws.reduce((a, b) => a + b, 0) / ws.length : 1
      for (const p of adds) {
        const sid = p[1]
        const px = openOf(sid, k)
        if (!px || px <= 0) continue
        const budget = (Vprev * expo * (p[2] / wbar)) / K
        const amt = Math.min(budget, cash)
        if (amt <= 1e-9 || (budget > 0 && amt < budget * 0.05)) { cashBlocked++; continue }
        const shares = amt / px
        cash -= amt
        const eI = p[4] ? (idxOf[p[4]] ?? k + p[5]) : k + p[5]
        const pos: Position = {
          endEstimated: false,          // 稍后统一补（可能落在数据末端之后）
          sw: p[0], sid, name: nameOf(sid), action: p[6], w: p[2], score: p[3],
          horizon: p[5], buyDate: d, buyI: k, buyPx: px, shares, amt,
          endDate: p[4], endI: eI, lastPx: px, mv: shares * px,
          pnl: 0, pnlPct: 0,
        }
        holdings.push(pos)
        todayBuys.push(pos)
        nBuys++
      }
    }

    // ② 收盘卖出（到期；当日所得次日才可用）
    const kept: Position[] = []
    for (const h of holdings) {
      const c = closeOf(h.sid, k)
      if (h.endI <= k) {
        const px = c ?? h.lastPx
        if (px) {
          const proceeds = h.shares * px - h.amt * COST
          const pnl = proceeds - h.amt
          cash += proceeds
          realised += pnl
          fees += h.amt * COST
          nSells++
          todaySells.push({
            sw: h.sw, sid: h.sid, name: h.name, buyDate: h.buyDate, buyPx: h.buyPx,
            sellPx: px, sellDate: d, shares: h.shares, amt: h.amt, proceeds, pnl,
            ret: px / h.buyPx - 1 - COST, heldDays: k - h.buyI, horizon: h.horizon,
          })
        }
        continue
      }
      kept.push(h)
    }
    holdings = kept

    // ③ 现金计息 ④ 收盘盯市
    cash *= 1 + rf
    let mv = 0
    for (const h of holdings) {
      const c = closeOf(h.sid, k)
      if (c) h.lastPx = c
      h.mv = h.shares * h.lastPx
      h.pnl = h.mv - h.amt
      h.pnlPct = h.amt > 0 ? h.mv / h.amt - 1 : 0
      mv += h.mv
    }
    const v = cash + mv
    window.push({ d, v })
    if (v > peak) peak = v
    const dd = peak > 0 ? v / peak - 1 : 0
    if (dd < maxdd) maxdd = dd
  }

  let mv = 0
  for (const h of holdings) {
    const c = closeOf(h.sid, asOfI)
    if (c) h.lastPx = c
    h.mv = h.shares * h.lastPx
    h.pnl = h.mv - h.amt
    h.pnlPct = h.amt > 0 ? h.mv / h.amt - 1 : 0
    mv += h.mv
  }
  const value = cash + mv

  // ---- 到期日：数据内有真实日期；落在数据末端之后按周末推算（标 ~）----
  const lastI = dates.length - 1
  const matDate = (endI: number, real: string | null): [string | null, boolean] => {
    if (real && idxOf[real] !== undefined) return [real, false]
    if (endI <= lastI) return [dates[endI], false]
    return [addWeekdays(dates[lastI], endI - lastI), true]
  }
  for (const h of holdings) {
    const [dd, est] = matDate(h.endI, h.endDate)
    h.endDate = dd
    h.endEstimated = est
  }

  // ---- 「下一交易日我要下的单」：买入 + 卖出，一眼看清今天要做什么 ----
  // 两种情形用同一套规则（信号都取 dates[asOfI]）：
  //   · 选中日在数据中段 → 下一交易日是数据里的真实日期；
  //   · 选中日就是最新交易日 → 数据里没有"明天"，按周末推算日期（这正是最常用的情形）。
  // 金额只取决于账户状态与当日结论（不猜测未来行情），所以盘后就能把单子定下来。
  const rawNext = asOfI + 1 < dates.length ? dates[asOfI + 1] : null
  const nextDate = rawNext ?? addWeekdays(dates[asOfI], 1)
  const nextDateEstimated = rawNext === null
  const nextBuys: PlanRow[] = []
  const nextQueue: PlanRow[] = []
  const nextSells: SellPlanRow[] = []
  let nextBlocked: 'no_candidates' | 'no_slots' | 'no_cash' | null = null
  {
    const day = sim.days[dates[asOfI]]
    const expo = day ? day.e : 1.0
    const cands = day ? day.p.length : 0
    const prio = day ? [...day.p].sort((a, b) => b[3] - a[3]).slice(0, maxNew) : []
    const room = Math.max(0, sim.max_holdings - holdings.length)
    const adds = prio.slice(0, room)
    if (adds.length) {
      const ws = holdings.map((h) => h.w).concat(adds.map((p) => p[2]))
      const wbar = ws.length ? ws.reduce((a, b) => a + b, 0) / ws.length : 1
      for (const p of adds) {
        const budget = (value * expo * (p[2] / wbar)) / K
        const amount = Math.min(budget, cash)
        if (amount <= 1e-9 || (budget > 0 && amount < budget * 0.05)) continue
        const px = closeOf(p[1], asOfI) ?? openOf(p[1], asOfI)
        nextBuys.push({
          sw: p[0], sid: p[1], name: nameOf(p[1]), action: p[6], w: p[2], score: p[3],
          horizon: p[5], amount, cashCut: budget > 0 ? 1 - amount / budget : 0,
          lots: px ? Math.floor(amount / px / 100) : 0,
        })
      }
    }
    if (cands === 0) nextBlocked = 'no_candidates'
    else if (room === 0) nextBlocked = 'no_slots'
    else if (nextBuys.length === 0) nextBlocked = 'no_cash'
    // 建不了仓时，把"排队顺序"摆出来：槽位或现金一释放，系统就按综合分从高到低建仓
    if (nextBuys.length === 0 && prio.length) {
      const ws2 = holdings.map((h) => h.w).concat(prio.map((p) => p[2]))
      const wbar2 = ws2.length ? ws2.reduce((a, b) => a + b, 0) / ws2.length : 1
      for (const p of prio) {
        const budget = (value * expo * (p[2] / wbar2)) / K
        const px = closeOf(p[1], asOfI) ?? openOf(p[1], asOfI)
        nextQueue.push({
          sw: p[0], sid: p[1], name: nameOf(p[1]), action: p[6], w: p[2], score: p[3],
          horizon: p[5], amount: budget, cashCut: 0,
          lots: px ? Math.floor(budget / px / 100) : 0,
        })
      }
    }
    // 下一交易日收盘到期的持仓（当天收盘卖出；参考金额按最新收盘价估算）
    for (const h of holdings) {
      if (h.endI !== asOfI + 1) continue
      const px = closeOf(h.sid, asOfI) ?? h.lastPx
      nextSells.push({
        sw: h.sw, sid: h.sid, name: h.name, buyDate: h.buyDate, buyPx: h.buyPx,
        lastPx: px, shares: h.shares, amt: h.amt,
        estProceeds: h.shares * px - h.amt * COST,
        estPnl: h.shares * px - h.amt * (1 + COST),
        estRet: h.buyPx > 0 ? px / h.buyPx - 1 - COST : 0,
        heldDays: asOfI - h.buyI, horizon: h.horizon,
        endDate: nextDate, endEstimated: nextDateEstimated,
      })
    }
  }

  return {
    startDate: dates[Math.max(0, startK - 1)], asOf: dates[asOfI], asOfI,
    nextDate, nextDateEstimated, window,
    capital, cash, mv, value, totalPnl: value - capital,
    floatPnl: mv - holdings.reduce((a, h) => a + h.amt, 0),
    realised, fees, peak, maxdd,
    exposure: value > 0 ? mv / value : 0,
    positions: holdings, todayBuys, todaySells, nextBuys, nextQueue, nextSells, nextBlocked,
    todaySignalDate,
    todayBlocked: todaySignalDate === null ? null
      : (todayCands === 0 ? 'no_candidates'
        : todaySlotsFree === 0 ? 'no_slots'
        : todayBuys.length === 0 ? 'no_cash' : null),
    nBuys, nSells, cashBlocked, slotBlocked,
  }
}

/** 交易日间隔（用于「持有 N 个交易日」） */
export function tradingDays(sim: SimData, from: string, to: string): number | null {
  const idxOf: Record<string, number> = {}
  sim.dates.forEach((d, i) => { idxOf[d] = i })
  const a = idxOf[from], b = idxOf[to]
  return a === undefined || b === undefined ? null : b - a
}

/** 本地时区的今天（YYYY-MM-DD）——不能用 toISOString（那是 UTC，会差一天） */
export function localToday(now: Date = new Date()): string {
  const m = `${now.getMonth() + 1}`.padStart(2, '0')
  const d = `${now.getDate()}`.padStart(2, '0')
  return `${now.getFullYear()}-${m}-${d}`
}

export type ExecState = 'before' | 'today' | 'stale' | 'none'

/**
 * 页面该以什么口吻说话（决定"操作单"是"今天"还是"下一交易日"）：
 *   before —— 执行日还没到（结论日的当晚/周末）：现在就能把单子定下来
 *   today  —— 执行日当天（盘中或收盘后）：这就是"今天要做什么"
 *   stale  —— 执行日已过、但下一次结论还没出（数据滞后）：给出明确的滞后警示
 */
export function execStateOf(execDay: string | null | undefined, clientToday: string): ExecState {
  if (!execDay) return 'none'
  if (execDay > clientToday) return 'before'
  if (execDay === clientToday) return 'today'
  return 'stale'
}

/** 两个 ISO 日期相差的自然日（b - a） */
export function daysBetweenISO(a: string, b: string): number {
  return Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000)
}
