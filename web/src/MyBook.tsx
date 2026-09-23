import React from 'react'
import Calendar from './Calendar'
import { Chip, num, pct } from './ui'
import { useEtfModal } from './etf-modal-host'
import { fetchSim, runSim, yuan, type SimData, type SimResult } from './sim'

/**
 * 「我的持仓」：**先回答"今天买什么、卖什么"**，再回答"这本账现在什么样"。
 *
 * 页面顺序刻意如此：
 *   ① 今日操作 —— 买入清单 / 卖出清单（金额、约合手数、什么时候下单）
 *   ② 账户概览 —— 总值 / 市值 / 现金 / 盈亏
 *   ③ 当前持仓 —— 逐笔成本、现价、市值、浮盈、到期日
 *   ④ 对账 —— 当日已经成交的那些（只用于核对，不需要操作）
 *
 * 口径：与引擎记账规则逐位一致（见 sim.ts 顶部说明）——有限资金池、只买不卖、
 * 卖出所得次日才可用、T+1 开盘建仓、持仓到期收盘卖出、现金计息、成本在卖出时扣除、
 * 权重随价格漂移（没有任何每日再平衡）。因此金额是**真实存在的钱**，不是目标权重折算。
 */

const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏' }

function Stat({ k, v, n, tone }: { k: string; v: string; n?: string; tone?: 'up' | 'down' }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className={`v num ${tone || ''}`}>{v}</div>
      {n && <div className="n">{n}</div>}
    </div>
  )
}

export default function MyBook({ data, days, selected, onSelect }: {
  data: any
  days: any[]
  selected: string
  onSelect: (d: string) => void
}) {
  const { open } = useEtfModal()

  const [capital, setCapital] = React.useState<number>(() => {
    const v = Number(window.localStorage.getItem('ark.capital'))
    return Number.isFinite(v) && v > 0 ? v : 100000
  })
  const [startFrom, setStartFrom] = React.useState<string>(
    () => window.localStorage.getItem('ark.followFrom') || '',
  )
  const [sim, setSim] = React.useState<SimData | null>(null)
  const [failed, setFailed] = React.useState(false)
  const [byEtf, setByEtf] = React.useState(false)

  const date = data?.date || selected
  React.useEffect(() => { window.localStorage.setItem('ark.capital', String(capital)) }, [capital])
  React.useEffect(() => { window.localStorage.setItem('ark.followFrom', startFrom) }, [startFrom])
  React.useEffect(() => {
    let alive = true
    fetchSim().then((s) => {
      if (!alive) return
      if (!s) setFailed(true)
      setSim(s)
    })
    return () => { alive = false }
  }, [])

  const acct: SimResult | null = React.useMemo(
    () => (sim && date ? runSim(sim, { capital, startDate: startFrom, asOf: date }) : null),
    [sim, capital, startFrom, date],
  )

  const started = !!acct && (!startFrom || acct.asOf >= acct.startDate)
  const multi = acct && acct.capital > 0 ? acct.value / acct.capital : null
  const costOf = (acct?.positions || []).reduce((a, p) => a + p.amt, 0)
  const todayBuyTotal = (acct?.todayBuys || []).reduce((a, b) => a + b.amt, 0)
  const todaySellTotal = (acct?.todaySells || []).reduce((a, b) => a + b.proceeds, 0)
  const todaySellPnl = (acct?.todaySells || []).reduce((a, b) => a + b.pnl, 0)
  // 下一个腾出槽位的时点（最早到期的持仓）：槽位满时告诉用户"什么时候才能再买"
  const nextFree = (acct?.positions || []).map((p) => p.endDate).filter(Boolean).sort()[0] || null

  // 按 ETF 汇总视图（同一只 ETF 会被反复建仓，各笔分开记）
  const grouped = React.useMemo(() => {
    if (!acct) return []
    const m = new Map<string, {
      sid: string; name: string; sw: string; n: number; amt: number; mv: number
      pnl: number; firstBuy: string; lastBuy: string; endDate: string | null
      endEstimated: boolean
    }>()
    for (const p of acct.positions) {
      const g = m.get(p.sid)
      if (!g) {
        m.set(p.sid, {
          sid: p.sid, name: p.name, sw: p.sw, n: 1, amt: p.amt, mv: p.mv, pnl: p.pnl,
          firstBuy: p.buyDate, lastBuy: p.buyDate, endDate: p.endDate,
          endEstimated: p.endEstimated,
        })
      } else {
        g.n += 1; g.amt += p.amt; g.mv += p.mv; g.pnl += p.pnl
        if (p.buyDate < g.firstBuy) g.firstBuy = p.buyDate
        if (p.buyDate > g.lastBuy) g.lastBuy = p.buyDate
        if (p.endDate && (!g.endDate || p.endDate < g.endDate)) {
          g.endDate = p.endDate
          g.endEstimated = p.endEstimated
        }
      }
    }
    // 与逐笔表一致：最近建仓的排前面；同日建仓按市值大者优先
    return [...m.values()].sort((a, b) => (a.lastBuy === b.lastBuy
      ? b.mv - a.mv
      : (a.lastBuy < b.lastBuy ? 1 : -1)))
  }, [acct])

  const EtfCell = ({ sid, d }: { sid: string; d?: string }) => (
    <span className="etflink" onClick={() => open(sid, d)} title="点击查看走势核验">
      {sim?.meta[sid]?.n || sid}
      <span className="muted small num" style={{ marginLeft: 5 }}>{String(sid).split('.')[1]}</span>
    </span>
  )

  // 今天没买成的原因（口径 = 上一交易日的结论 + 今天开盘时的账户状态）
  const todayBuyReason = (() => {
    if (!acct || !started) return ''
    if (!acct.todaySignalDate) {
      return `入市起始日之前没有可用的结论（今天的单子来自上一交易日的结论）。`
    }
    if (acct.todayBlocked === 'no_candidates') {
      return `${acct.todaySignalDate} 的结论里没有可交易的新建候选，今天不买。`
    }
    if (acct.todayBlocked === 'no_slots') {
      return `今天开盘时槽位已满（${sim?.max_holdings}/${sim?.max_holdings} 笔）`
        + (nextFree ? `，最早 ${nextFree} 有持仓到期腾位，之后才会新建。` : '，要等持仓到期腾位。')
    }
    if (acct.todayBlocked === 'no_cash') {
      return `今天开盘可用现金不足以达到单笔预算的 5%，系统按规则跳过（绝不卖出腾挪）。`
    }
    return '今天没有买入。'
  })()

  // 今日结论（= 明天的单子）只用一行说明，不做成表格——页面主角是"今天做什么"
  const tomorrowHint = (() => {
    if (!acct || !acct.nextDate) return null
    const names = acct.nextQueue.map((b) => `${b.name}（${ACT_TXT[b.action || ''] || b.action || '—'} ${num(b.score, 1)}）`)
    if (!names.length) {
      if (acct.nextBlocked === 'no_candidates') return `${date} 收盘的新结论里没有可交易候选。`
      return null
    }
    const why = acct.nextBlocked === 'no_slots'
      ? `当前 ${acct.positions.length}/${sim?.max_holdings} 无空槽`
      : acct.nextBlocked === 'no_cash' ? '当前现金不足' : ''
    return `${date} 收盘的新结论：${names.join('、')}——按规则在下一交易日开盘建仓`
      + (why ? `（${why}，届时是否建仓取决于空槽与现金）。` : '。')
  })()

  return (
    <>
      <Calendar days={days} selected={selected} onSelect={onSelect} />

      <div className="card">
        <div className="colhead">
          <h2>我的持仓 · {date}</h2>
          <span className="n">本金 / 入市起始日只影响本页，与今日研判的系统评估数据完全隔离</span>
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, marginBottom: 12 }}>
          <label className="small muted">
            本金
            <input
              type="number" min={0} step={10000} value={capital}
              onChange={(e) => setCapital(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 110, marginLeft: 6, padding: '3px 6px', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
            />
          </label>
          <label className="small muted">
            入市起始日
            <input
              type="date" value={startFrom}
              onChange={(e) => setStartFrom(e.target.value)}
              style={{ marginLeft: 6, padding: '3px 6px', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
            />
          </label>
          <span className="small muted">
            （留空 = 从数据起点 {sim?.dates?.[0] || '—'} 开始跟随；非交易日自动顺延到下一交易日）
          </span>
          {startFrom && (
            <button className="linkbtn" onClick={() => setStartFrom('')} style={{ marginLeft: 'auto' }}>
              从数据起点起算
            </button>
          )}
        </div>

        {data?.realtime_fill && (
          <div className="warnbar">
            <b>数据含实时兜底日</b>
            {Object.keys(data.realtime_fill.days).join('、')}：
            官方申万日线尚未发布（停在 {data.realtime_fill.official_last}），收盘价取自实时行情、
            <b>量额为估算</b>；官方出数后重跑 <code>ark.py daily</code> 会自动覆盖，结论与持仓会随之微调。
          </div>
        )}

        {failed && <div className="note">影子账户数据缺失（`data/sim.json`），请先运行一次完整引擎（`python engine/pipeline.py --all`）。</div>}
        {!failed && !sim && <div className="muted small">正在重放账户（首次加载 `data/sim.json`）…</div>}

        {acct && started && (
          <>
            {/* ============ ① 今天要做什么（今天买入 / 今天卖出） ============ */}
            <div className="colhead" style={{ marginBottom: 6 }}>
              <h2 style={{ fontSize: 16 }}>今天要做什么 · {date}</h2>
              <span className="n">
                <b>今天买入</b> = {acct.todaySignalDate || '—'} 收盘给出的结论，在<b>今天开盘</b>成交；
                <b> 今天卖出</b> = 今天到期的持仓，在<b>今天收盘</b>成交
              </span>
            </div>

            <div className="opbar">
              <div className="opbar-item">
                <span className="opbar-k">今天买</span>
                <span className={`opbar-v num ${acct.todayBuys.length ? 'up' : 'mute'}`}>
                  {acct.todayBuys.length} 笔 · {yuan(todayBuyTotal)}
                </span>
              </div>
              <div className="opbar-item">
                <span className="opbar-k">今天卖</span>
                <span className={`opbar-v num ${acct.todaySells.length ? 'down' : 'mute'}`}>
                  {acct.todaySells.length} 笔 · {yuan(todaySellTotal)}
                </span>
              </div>
              <div className="opbar-item">
                <span className="opbar-k">账户</span>
                <span className="opbar-v num">
                  {yuan(acct.value)} · 现金 {yuan(acct.cash)} · 持仓 {acct.positions.length}/{sim?.max_holdings}
                </span>
              </div>
            </div>

            {/* —— 今天买入 —— */}
            <div className="colhead" style={{ margin: '10px 0 6px' }}>
              <h2 style={{ fontSize: 15 }}>今天买入（{acct.todayBuys.length} 笔）</h2>
              <span className="n">
                {acct.todaySignalDate ? `${acct.todaySignalDate} 的结论 · ` : ''}
                今天开盘按金额买入；现金不够就少买，绝不卖出腾挪
              </span>
            </div>
            {acct.todayBuys.length === 0 ? (
              <div className="muted small" style={{ padding: '2px 0 10px' }}>{todayBuyReason}</div>
            ) : (
              <table style={{ marginBottom: 12 }}>
                <thead>
                  <tr>
                    <th>ETF</th><th>行业</th><th>动作</th>
                    <th style={{ textAlign: 'right' }}>买入金额</th>
                    <th style={{ textAlign: 'right' }}>买入价</th>
                    <th style={{ textAlign: 'right' }}>份数</th>
                    <th style={{ textAlign: 'right' }}>占组合</th>
                    <th>到期日</th>
                  </tr>
                </thead>
                <tbody>
                  {[...acct.todayBuys].sort((a, b) => b.amt - a.amt).map((b, i) => (
                    <tr key={i}>
                      <td><EtfCell sid={b.sid} d={b.buyDate} /></td>
                      <td className="muted">{b.sw}</td>
                      <td><Chip tone={b.action === 'BUY_NOW' ? 'up' : 'ambush'}>{ACT_TXT[b.action || ''] || b.action || '—'}</Chip></td>
                      <td className="num buyamt" style={{ textAlign: 'right' }}>{yuan(b.amt)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{num(b.buyPx, 3)}</td>
                      <td className="num muted" style={{ textAlign: 'right' }}>{num(b.shares, 0)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>
                        {pct(acct.value > 0 ? b.mv / acct.value : 0, 2, false)}
                      </td>
                      <td className="num muted" title={b.endEstimated ? '按周末推算（未计节假日）' : ''}>
                        {b.endEstimated ? '~' : ''}{b.endDate || '—'}
                      </td>
                    </tr>
                  ))}
                  <tr>
                    <td colSpan={3} style={{ textAlign: 'right' }} className="muted">合计</td>
                    <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>{yuan(todayBuyTotal)}</td>
                    <td colSpan={4} />
                  </tr>
                </tbody>
              </table>
            )}

            {/* —— 今天卖出 —— */}
            <div className="colhead" style={{ margin: '10px 0 6px' }}>
              <h2 style={{ fontSize: 15 }}>今天卖出（{acct.todaySells.length} 笔）</h2>
              <span className="n">持有到建议周期、今天到期的持仓，今天收盘卖出（所得次交易日才可用）</span>
            </div>
            {acct.todaySells.length === 0 ? (
              <div className="muted small" style={{ padding: '2px 0 10px' }}>今天没有到期持仓，不用卖。</div>
            ) : (
              <table style={{ marginBottom: 12 }}>
                <thead>
                  <tr>
                    <th>ETF</th><th>行业</th><th>买入日</th>
                    <th style={{ textAlign: 'right' }}>持有</th>
                    <th style={{ textAlign: 'right' }}>买入价 → 卖出价</th>
                    <th style={{ textAlign: 'right' }}>成本</th>
                    <th style={{ textAlign: 'right' }}>卖出金额</th>
                    <th style={{ textAlign: 'right' }}>实现盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {[...acct.todaySells].sort((a, b) => (a.buyDate === b.buyDate
                    ? b.amt - a.amt
                    : (a.buyDate < b.buyDate ? 1 : -1))).map((s, i) => (
                    <tr key={i}>
                      <td><EtfCell sid={s.sid} d={s.sellDate} /></td>
                      <td className="muted">{s.sw}</td>
                      <td className="num">{s.buyDate}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{s.heldDays} 日</td>
                      <td className="num" style={{ textAlign: 'right' }}>{num(s.buyPx, 3)} → {num(s.sellPx, 3)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{yuan(s.amt)}</td>
                      <td className="num buyamt" style={{ textAlign: 'right' }}>{yuan(s.proceeds)}</td>
                      <td className={`num ${s.pnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                        {s.pnl >= 0 ? '+' : ''}{yuan(s.pnl)}（{pct(s.ret)}）
                      </td>
                    </tr>
                  ))}
                  <tr>
                    <td colSpan={6} style={{ textAlign: 'right' }} className="muted">合计</td>
                    <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>{yuan(todaySellTotal)}</td>
                    <td className={`num ${todaySellPnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                      {todaySellPnl >= 0 ? '+' : ''}{yuan(todaySellPnl)}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}

            {tomorrowHint && (
              <div className="muted small" style={{ marginBottom: 4 }}>
                {tomorrowHint}
              </div>
            )}

            {/* ============ ② 账户概览 ============ */}
            <div className="grid g4" style={{ margin: '14px 0 12px' }}>
              <Stat k="账户总值" v={yuan(acct.value)}
                n={`本金 ${yuan(acct.capital)} · 净值 ${num(multi, 3)} · 累计 ${pct(acct.totalPnl / (acct.capital || 1))}`} />
              <Stat k={`持仓市值（${acct.positions.length} 笔）`} v={yuan(acct.mv)}
                n={`实际投出 ${pct(acct.exposure, 1, false)} · 浮动盈亏 ${acct.floatPnl >= 0 ? '+' : ''}${yuan(acct.floatPnl)}`} />
              <Stat k="可用现金" v={yuan(acct.cash)}
                n={`占 ${pct(acct.value > 0 ? acct.cash / acct.value : 0, 1, false)} · `
                  + (sim?.cash_yield ? `按 ${pct(sim.cash_yield, 2, false)} 逐日计息` : '不计息（按你的口径）')} />
              <Stat k="已实现盈亏" v={`${acct.realised >= 0 ? '+' : ''}${yuan(acct.realised)}`}
                n={`已卖出 ${acct.nSells} 笔 · 累计成本 ${yuan(acct.fees)} · 账户最大回撤 ${pct(acct.maxdd, 1, false)}`}
                tone={acct.realised >= 0 ? 'up' : 'down'} />
            </div>

            {/* ============ ③ 当前持仓 ============ */}
            <div className="colhead" style={{ marginBottom: 6 }}>
              <h2 style={{ fontSize: 15 }}>当前持仓（{acct.positions.length} 笔）</h2>
              <span className="n">按 {date} 收盘价盯市 · <b>买入日倒序</b> · 成本 = 买入时实际付出的现金</span>
              <button className="linkbtn" style={{ marginLeft: 'auto' }} onClick={() => setByEtf(!byEtf)}>
                {byEtf ? '按笔查看' : '按 ETF 汇总'}
              </button>
            </div>
            {acct.positions.length === 0 ? (
              <div className="muted small" style={{ padding: '2px 0 10px' }}>
                账本为空：入市起始日之后还没有建仓（或起始日设得比选中日还晚）。
              </div>
            ) : (
              <div className="tradescroll">
                <table>
                  {byEtf ? (
                    <>
                      <thead>
                        <tr>
                          <th>ETF</th><th>行业</th>
                          <th style={{ textAlign: 'right' }}>笔数</th>
                          <th style={{ textAlign: 'right' }}>成本</th>
                          <th style={{ textAlign: 'right' }}>市值</th>
                          <th style={{ textAlign: 'right' }}>浮动盈亏</th>
                          <th style={{ textAlign: 'right' }}>占比</th>
                          <th>最早建仓</th>
                          <th>最近建仓</th>
                          <th>最近到期</th>
                        </tr>
                      </thead>
                      <tbody>
                        {grouped.map((g, i) => (
                          <tr key={i}>
                            <td><EtfCell sid={g.sid} /></td>
                            <td className="muted">{g.sw}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{g.n}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(g.amt)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(g.mv)}</td>
                            <td className={`num ${g.pnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                              {g.pnl >= 0 ? '+' : ''}{yuan(g.pnl)}（{pct(g.amt > 0 ? g.mv / g.amt - 1 : 0)}）
                            </td>
                            <td className="num" style={{ textAlign: 'right' }}>{pct(acct.value > 0 ? g.mv / acct.value : 0, 2, false)}</td>
                            <td className="num muted">{g.firstBuy}</td>
                            <td className="num">{g.lastBuy}</td>
                            <td className="num muted" title={g.endEstimated ? '按周末推算（未计节假日）' : ''}>
                              {g.endEstimated ? '~' : ''}{g.endDate || '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </>
                  ) : (
                    <>
                      <thead>
                        <tr>
                          <th>ETF</th><th>行业</th><th>买入日</th>
                          <th style={{ textAlign: 'right' }}>持有</th>
                          <th style={{ textAlign: 'right' }}>买入价</th>
                          <th style={{ textAlign: 'right' }}>现价</th>
                          <th style={{ textAlign: 'right' }}>份数</th>
                          <th style={{ textAlign: 'right' }}>成本</th>
                          <th style={{ textAlign: 'right' }}>市值</th>
                          <th style={{ textAlign: 'right' }}>浮动盈亏</th>
                          <th style={{ textAlign: 'right' }}>占比</th>
                          <th>到期日</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...acct.positions].sort((a, b) => (a.buyDate === b.buyDate
                          ? (b.buyI - a.buyI || b.amt - a.amt)
                          : (a.buyDate < b.buyDate ? 1 : -1))).map((p, i) => (
                          <tr key={i}>
                            <td><EtfCell sid={p.sid} d={p.buyDate} /></td>
                            <td className="muted">{p.sw}</td>
                            <td className="num">{p.buyDate}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{Math.max(0, acct.asOfI - p.buyI)} 日</td>
                            <td className="num" style={{ textAlign: 'right' }}>{num(p.buyPx, 3)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{num(p.lastPx, 3)}</td>
                            <td className="num muted" style={{ textAlign: 'right' }}>{num(p.shares, 0)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(p.amt)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(p.mv)}</td>
                            <td className={`num ${p.pnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                              {p.pnl >= 0 ? '+' : ''}{yuan(p.pnl)}（{pct(p.pnlPct)}）
                            </td>
                            <td className="num" style={{ textAlign: 'right' }}>{pct(acct.value > 0 ? p.mv / acct.value : 0, 2, false)}</td>
                            <td className="num muted" title={p.endEstimated ? '按周末推算（未计节假日）' : ''}>
                              {p.endEstimated ? '~' : ''}{p.endDate || '—'}
                            </td>
                          </tr>
                        ))}
                        <tr>
                          <td colSpan={7} className="muted" style={{ textAlign: 'right' }}>合计</td>
                          <td className="num" style={{ textAlign: 'right' }}>{yuan(costOf)}</td>
                          <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>{yuan(acct.mv)}</td>
                          <td className={`num ${acct.floatPnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                            {acct.floatPnl >= 0 ? '+' : ''}{yuan(acct.floatPnl)}（{pct(costOf > 0 ? acct.mv / costOf - 1 : 0)}）
                          </td>
                          <td colSpan={2} />
                        </tr>
                      </tbody>
                    </>
                  )}
                </table>
              </div>
            )}

            <div className="note" style={{ marginTop: 10 }}>
              <b>这是重放出来的真账，不是目标权重折算</b>：引擎的资金规则（`simulate_cash_account`）
              在本页被逐日重放——本金 {yuan(acct.capital)}，{acct.startDate} 收盘入账，
              之后每一次建仓都发生在信号日的<b>下一交易日开盘</b>，金额 = min(预算, 可用现金)，
              预算 = 昨收账户总值 × 建仓闸门 × (w ÷ w̄) ÷ K（K = {sim?.K}）；
              <b>现金不够就少买，绝不卖出腾挪</b>。
              卖出只在持有到建议周期结束时发生（当日收盘、所得次日可用），
              权重随价格漂移、<b>没有任何每日再平衡</b>；未投入现金
              {sim?.cash_yield ? `按 ${pct(sim.cash_yield, 2, false)} 年化逐日计息` : <b>不计息</b>}。
              <br />
              <b>与实盘的差异</b>：① 引擎按份数（可小数）记账，实盘按 100 份/手取整，上表「约合手数」仅供参考；
              ② 未计你的实际佣金档位与冲击成本（引擎统一按双边 {(Number(sim?.cost || 0) * 100).toFixed(2)}% 扣）；
              ③ 若你某天没跟单或提前卖出，账面就会与这里分叉——差异本身就是执行偏差的度量。
              <br />
              <b>当日约束的真实情况</b>：本次重放中因现金不足被缩量/跳过的建仓 {acct.cashBlocked} 次、
              因容量（{sim?.max_holdings} 槽）已满被挡掉的候选 {acct.slotBlocked} 笔
              ——这两项就是"固定投入比例"在真实资金下必须付出的代价。
            </div>
          </>
        )}

        {acct && !started && (
          <div className="muted small">
            入市起始日（{startFrom}）晚于当前选中日（{date}），此区间内账户尚未开始跟随。
          </div>
        )}
      </div>
    </>
  )
}
