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
  /** 买入当日该笔占账户总值的比例（与引擎事件同口径：持仓市值 ÷ 账户总值） */
  wShare: number
}

export type SellRow = {
  sw: string; sid: string; name: string
  buyDate: string; buyPx: number; sellPx: number; sellDate: string
  shares: number; amt: number; proceeds: number
  /** 已实现盈亏（已扣成本） */
  pnl: number
  ret: number
  /** 毛收益率（未扣双边成本）= 卖出价 ÷ 买入价 − 1 */
  gross: number
  heldDays: number
  horizon: number
  /** 卖出当日该笔占账户总值的比例（同引擎事件口径） */
  wShare: number
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
  /** 入市起始日（= 顺延后的交易日；本金在该日收盘入账，下一交易日开盘首次建仓） */
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
  /** 入市起始日以来**本账户的全部成交**（含已平仓；positions 是其中尚未到期的那些） */
  buys: Position[]
  sells: SellRow[]
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

/** 带符号金额（元）：+¥12,345 / −¥678 / ¥0（零不带符号，负号用真正的减号） */
export function yuanSigned(x: number): string {
  const r = Math.round(x)
  return `${r > 0 ? '+' : r < 0 ? '−' : ''}¥${Math.abs(r).toLocaleString('zh-CN')}`
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
  let beyondData = false          // 起始日晚于全部数据 → 这本账还没开始
  if (opts.startDate) {
    const hit = idxOf[opts.startDate]
    if (hit !== undefined) sI = hit
    else {
      const nxt = dates.findIndex((d) => d >= (opts.startDate as string))
      if (nxt >= 0) sI = nxt
      else { sI = dates.length - 1; beyondData = true }
    }
  }
  // 入市起始日 = dates[sI]（顺延后的交易日）：本金在该日**收盘**入账，同日收盘的结论
  // 在**下一交易日开盘**建仓（与全系统的 T 收盘出结论 → T+1 开盘买入一致）。
  // 所以第一个可能建仓的交易日是 sI+1；曲线基点也落在起始日本身，不再多出前一日。
  const firstK = beyondData ? dates.length : Math.max(sI, 0) + 1
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
    { d: dates[Math.max(0, sI)], v: capital },
  ]
  // 只保留**选中日当天**的成交（逐日重置，避免把整段重放的成交都堆在这里）
  let todayBuys: Position[] = []
  let todaySells: SellRow[] = []
  // 全区间成交史（弹窗按入市起始日复核本账户在这个标的上到底做了什么，用得到）
  const allBuys: Position[] = []
  const allSells: SellRow[] = []
  let realised = 0, fees = 0, nBuys = 0, nSells = 0, cashBlocked = 0, slotBlocked = 0
  let peak = capital, maxdd = 0
  let todaySignalDate: string | null = null
  let todayCands = 0, todaySlotsFree = 0

  for (let k = firstK; k <= asOfI; k++) {
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
          pnl: 0, pnlPct: 0, wShare: 0,
        }
        // 组合占比：与引擎同一口径（settlement.py 事件里的 w_share）——
        // 该笔当日收盘市值 ÷ 账户总值（现金 + 全部持仓当日收盘市值）。
        const mvBuy = shares * (closeOf(sid, k) ?? px)
        const valBuy = cash + holdings.reduce(
          (a, h) => a + h.shares * (closeOf(h.sid, k) ?? h.buyPx), 0) + mvBuy
        pos.wShare = valBuy > 0 ? mvBuy / valBuy : 0
        holdings.push(pos)
        todayBuys.push(pos)
        allBuys.push(pos)
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
          const gross = h.buyPx > 0 ? px / h.buyPx - 1 : 0
          // 卖出占比同引擎：该笔卖出市值 ÷ 卖出后账户总值（所得已计入现金）
          const valSell = cash - proceeds + h.shares * px
          const sellRow: SellRow = {
            sw: h.sw, sid: h.sid, name: h.name, buyDate: h.buyDate, buyPx: h.buyPx,
            sellPx: px, sellDate: d, shares: h.shares, amt: h.amt, proceeds, pnl,
            ret: px / h.buyPx - 1 - COST, gross, heldDays: k - h.buyI, horizon: h.horizon,
            wShare: valSell > 0 ? (h.shares * px) / valSell : 0,
          }
          todaySells.push(sellRow)
          allSells.push(sellRow)
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
      // 投影必须与引擎真实建仓同规则：**逐笔扣减现金**，否则两笔各自的"上限=全部现金"
      // 会让买入合计超过可用现金（页面上就会出现买 ¥8,885 但现金只有 ¥7,324）。
      let cashLeft = cash
      for (const p of adds) {
        const budget = (value * expo * (p[2] / wbar)) / K
        const amount = Math.min(budget, cashLeft)
        if (amount <= 1e-9 || (budget > 0 && amount < budget * 0.05)) continue
        cashLeft -= amount
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
      let cashLeftQ = cash          // 同理：队列内的现金也逐笔递减
      for (const p of prio) {
        const budget = (value * expo * (p[2] / wbar2)) / K
        const px = closeOf(p[1], asOfI) ?? openOf(p[1], asOfI)
        const canPay = Math.min(budget, cashLeftQ)
        cashLeftQ -= canPay
        nextQueue.push({
          sw: p[0], sid: p[1], name: nameOf(p[1]), action: p[6], w: p[2], score: p[3],
          horizon: p[5], amount: budget,
          cashCut: budget > 0 ? 1 - canPay / budget : 0,
          lots: px ? Math.floor(canPay / px / 100) : 0,
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
    startDate: beyondData ? (opts.startDate as string) : dates[Math.max(0, sI)],
    asOf: dates[asOfI], asOfI,
    nextDate, nextDateEstimated, window,
    capital, cash, mv, value, totalPnl: value - capital,
    floatPnl: mv - holdings.reduce((a, h) => a + h.amt, 0),
    realised, fees, peak, maxdd,
    exposure: value > 0 ? mv / value : 0,
    positions: holdings, todayBuys, todaySells, nextBuys, nextQueue, nextSells, nextBlocked,
    buys: allBuys, sells: allSells,
    todaySignalDate,
    todayBlocked: todaySignalDate === null ? null
      : (todayCands === 0 ? 'no_candidates'
        : todaySlotsFree === 0 ? 'no_slots'
        : todayBuys.length === 0 ? 'no_cash' : null),
    nBuys, nSells, cashBlocked, slotBlocked,
  }
}

/**
 * 本账户在某只 ETF 上的成交（B/S 事件），喂给「标的走势核验弹窗」。
 *
 * 与复盘页的区别：复盘页的事件来自引擎的**名义账本**（初始本金 1.0、按目标权重
 * 折算的金额），而这里的事件来自「我的持仓」按**你的本金与入市起始日**重放出来的
 * 真账——金额是元、份数是实际份数、已实现盈亏是真钱。
 *
 * 之所以必须换成本账户的成交：入市起始日一改，账户当天的现金与槽位就与引擎默认
 * 账本不同，之后买的标的/时点都会分叉。若弹窗仍画引擎的 B/S，就和页面上的
 * 「当前持仓」对不上了。
 */
export type BookEvent = {
  k: 'B' | 'S'
  date: string
  sw: string
  sid: string
  /** 复权连续价（与弹窗图上的结算口径一致） */
  price: number
  /** S 事件：买入日 / 买入价 */
  bd?: string
  bpx?: number
  h?: number
  a?: string | null
  sc?: number
  w?: number
  /** 占账户总值比例（引擎同口径） */
  w_share?: number
  /** B：实际付出的现金（元）；S：该笔原始成本（元） */
  amt?: number
  /** S：卖出金额（元，已扣成本） */
  proceeds?: number
  /** 份数（B 为建仓份数，S 为卖出份数） */
  sh?: number
  gross?: number
  ret?: number
  /** S：已实现盈亏（元，已扣双边成本） */
  pnl?: number
  why?: string
  /** B：到期日（未平仓行用它显示"还没卖、什么时候卖"）；endEst = 推算（数据末端之外） */
  end?: string | null
  endEst?: boolean
}

export function bookEvents(acct: SimResult, sid: string): BookEvent[] {
  const out: BookEvent[] = []
  for (const b of acct.buys) {
    if (b.sid !== sid) continue
    out.push({
      k: 'B', date: b.buyDate, sw: b.sw, sid: b.sid, price: b.buyPx,
      h: b.horizon, a: b.action, sc: b.score, w: b.w, w_share: b.wShare,
      amt: b.amt, sh: b.shares, end: b.endDate, endEst: b.endEstimated,
    })
  }
  for (const s of acct.sells) {
    if (s.sid !== sid) continue
    out.push({
      k: 'S', date: s.sellDate, sw: s.sw, sid: s.sid, price: s.sellPx,
      bd: s.buyDate, bpx: s.buyPx, h: s.horizon, gross: s.gross, ret: s.ret,
      w_share: s.wShare, amt: s.amt, proceeds: s.proceeds, sh: s.shares,
      pnl: s.pnl, why: '持有到期（本账户没有提前卖出）',
    })
  }
  return out.sort((a, b) => (a.date === b.date ? (a.k === b.k ? 0 : a.k === 'B' ? -1 : 1)
    : a.date.localeCompare(b.date)))
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
