import React from 'react'
import Calendar from './Calendar'
import { Chip, num, pct } from './ui'
import { useEtfModal } from './etf-modal-host'
import { fetchSim, runSim, yuan, type SimData, type SimResult } from './sim'

/**
 * 「我的持仓」：把系统信号翻译成**你自己这本账**（档 2：逐日影子账户重放）。
 *
 * 与「今日研判」严格分层：
 *   · 今日研判 = 引擎评估（该不该做、为什么、建议周期），不含任何资金计算；
 *   · 本页 = 我的账户（本金 / 入市起始日）→ 现金余额、逐笔持仓市值、浮盈、买卖清单。
 *
 * 口径：与引擎记账规则**逐位一致**（见 sim.ts 顶部说明）——有限资金池、只买不卖、
 * 卖出所得次日才可用、T+1 开盘建仓、持仓到期收盘卖出、现金计息、成本在卖出时扣除、
 * 权重随价格漂移（没有任何每日再平衡）。因此这里的金额不是"按目标权重折算的应有金额"，
 * 而是**真实存在的钱**：现金就是现金，市值就是按当日收盘价算出来的市值。
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

  // 按 ETF 汇总视图（同一只 ETF 会被反复建仓，各笔分开记）
  const grouped = React.useMemo(() => {
    if (!acct) return []
    const m = new Map<string, {
      sid: string; name: string; sw: string; n: number; amt: number; mv: number
      pnl: number; firstBuy: string; lastBuy: string; endDate: string | null
    }>()
    for (const p of acct.positions) {
      const g = m.get(p.sid)
      if (!g) {
        m.set(p.sid, {
          sid: p.sid, name: p.name, sw: p.sw, n: 1, amt: p.amt, mv: p.mv, pnl: p.pnl,
          firstBuy: p.buyDate, lastBuy: p.buyDate, endDate: p.endDate,
        })
      } else {
        g.n += 1; g.amt += p.amt; g.mv += p.mv; g.pnl += p.pnl
        if (p.buyDate < g.firstBuy) g.firstBuy = p.buyDate
        if (p.buyDate > g.lastBuy) g.lastBuy = p.buyDate
        if (p.endDate && (!g.endDate || p.endDate < g.endDate)) g.endDate = p.endDate
      }
    }
    return [...m.values()].sort((a, b) => b.mv - a.mv)
  }, [acct])

  const EtfCell = ({ sid, d }: { sid: string; d?: string }) => (
    <span className="etflink" onClick={() => open(sid, d)} title="点击查看走势核验">
      {sim?.meta[sid]?.n || sid}
      <span className="muted small num" style={{ marginLeft: 5 }}>{String(sid).split('.')[1]}</span>
    </span>
  )

  return (
    <>
      <Calendar days={days} selected={selected} onSelect={onSelect} />

      <div className="card">
        <div className="colhead">
          <h2>我的持仓 · {date}</h2>
          <span className="n">本金 / 入市起始日只影响本页，与今日研判的系统评估数据完全隔离</span>
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, marginBottom: 10 }}>
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

        {failed && <div className="note">影子账户数据缺失（`data/sim.json`），请先运行一次完整引擎（`python engine/pipeline.py --all`）。</div>}
        {!failed && !sim && <div className="muted small">正在重放账户（首次加载 `data/sim.json`）…</div>}

        {acct && started && (
          <>
            <div className="grid g4" style={{ marginBottom: 12 }}>
              <Stat k="账户总值" v={yuan(acct.value)}
                n={`本金 ${yuan(acct.capital)} · 净值 ${num(multi, 3)} · 累计 ${pct(acct.totalPnl / (acct.capital || 1))}`} />
              <Stat k={`持仓市值（${acct.positions.length} 笔）`} v={yuan(acct.mv)}
                n={`实际投出 ${pct(acct.exposure, 1, false)} · 浮动盈亏 ${acct.floatPnl >= 0 ? '+' : ''}${yuan(acct.floatPnl)}`} />
              <Stat k="可用现金" v={yuan(acct.cash)}
                n={`占 ${pct(acct.value > 0 ? acct.cash / acct.value : 0, 1, false)} · 按 ${pct(sim?.cash_yield, 2, false)} 逐日计息`} />
              <Stat k="已实现盈亏" v={`${acct.realised >= 0 ? '+' : ''}${yuan(acct.realised)}`}
                n={`已卖出 ${acct.nSells} 笔 · 累计成本 ${yuan(acct.fees)} · 账户最大回撤 ${pct(acct.maxdd, 1, false)}`}
                tone={acct.realised >= 0 ? 'up' : 'down'} />
            </div>

            {/* 当日已执行：买入（开盘）+ 卖出（收盘） */}
            <div className="colhead" style={{ marginBottom: 6 }}>
              <h2 style={{ fontSize: 15 }}>当日已执行（{date}）</h2>
              <span className="n">
                买入 = 上一交易日结论在今日<b>开盘</b>成交；卖出 = 持仓到期在今日<b>收盘</b>成交，所得现金下一交易日才可用
              </span>
            </div>
            {(acct.todayBuys.length + acct.todaySells.length) === 0 ? (
              <div className="muted small" style={{ padding: '2px 0 10px' }}>当日没有成交。</div>
            ) : (
              <table style={{ marginBottom: 12 }}>
                <thead>
                  <tr>
                    <th>方向</th><th>ETF</th><th>行业</th><th>动作</th>
                    <th style={{ textAlign: 'right' }}>成交价</th>
                    <th style={{ textAlign: 'right' }}>金额</th>
                    <th style={{ textAlign: 'right' }}>结果</th>
                  </tr>
                </thead>
                <tbody>
                  {acct.todayBuys.map((b, i) => (
                    <tr key={`b${i}`}>
                      <td><Chip tone="up">买入</Chip></td>
                      <td><EtfCell sid={b.sid} d={b.buyDate} /></td>
                      <td className="muted">{b.sw}</td>
                      <td><Chip tone={b.action === 'BUY_NOW' ? 'up' : 'ambush'}>{ACT_TXT[b.action || ''] || b.action || '—'}</Chip></td>
                      <td className="num" style={{ textAlign: 'right' }}>{num(b.buyPx, 3)}</td>
                      <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>{yuan(b.amt)}</td>
                      <td className="muted small" style={{ textAlign: 'right' }}>
                        {num(b.shares, 0)} 份 · 到期 {b.endDate || '—'}
                      </td>
                    </tr>
                  ))}
                  {acct.todaySells.map((s, i) => (
                    <tr key={`s${i}`}>
                      <td><Chip tone="exit">卖出</Chip></td>
                      <td><EtfCell sid={s.sid} d={s.sellDate} /></td>
                      <td className="muted">{s.sw}</td>
                      <td className="muted small">{s.buyDate} 买入 · 持 {s.heldDays} 日</td>
                      <td className="num" style={{ textAlign: 'right' }}>{num(s.buyPx, 3)} → {num(s.sellPx, 3)}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{yuan(s.proceeds)}</td>
                      <td className={`num ${s.pnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                        {s.pnl >= 0 ? '+' : ''}{yuan(s.pnl)}（{pct(s.ret)}）
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* 下一交易日待执行 */}
            {acct.nextDate && (
              <>
                <div className="colhead" style={{ marginBottom: 6 }}>
                  <h2 style={{ fontSize: 15 }}>下一交易日待执行（{acct.nextDate} 开盘）</h2>
                  <span className="n">按 {date} 收盘的账户总值与可用现金算出的实际买入金额（不是目标权重）</span>
                </div>
                {acct.nextBuys.length === 0 ? (
                  <div className="muted small" style={{ padding: '2px 0 10px' }}>
                    {acct.positions.length >= (sim?.max_holdings || 32)
                      ? '当前没有空槽：要等已有持仓到期腾出槽位后才会新建。'
                      : acct.cash <= capital * 0.005
                        ? '可用现金不足（不足单笔预算的 5%），当天不新建。'
                        : '当日结论里没有进入新建名额的标的。'}
                  </div>
                ) : (
                  <table style={{ marginBottom: 12 }}>
                    <thead>
                      <tr>
                        <th>ETF</th><th>行业</th><th>动作</th>
                        <th style={{ textAlign: 'right' }}>综合分</th>
                        <th style={{ textAlign: 'right' }}>组合占比</th>
                        <th style={{ textAlign: 'right' }}>买入金额</th>
                        <th style={{ textAlign: 'right' }}>约合手数</th>
                        <th style={{ textAlign: 'right' }}>建议周期</th>
                      </tr>
                    </thead>
                    <tbody>
                      {acct.nextBuys.map((b, i) => (
                        <tr key={i}>
                          <td><EtfCell sid={b.sid} d={acct.nextDate || undefined} /></td>
                          <td className="muted">{b.sw}</td>
                          <td><Chip tone={b.action === 'BUY_NOW' ? 'up' : 'ambush'}>{ACT_TXT[b.action || ''] || b.action || '—'}</Chip></td>
                          <td className="num" style={{ textAlign: 'right' }}>{num(b.score, 1)}</td>
                          <td className="num" style={{ textAlign: 'right' }}>
                            {pct(acct.value > 0 ? b.amount / acct.value : 0, 2, false)}
                          </td>
                          <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>{yuan(b.amount)}</td>
                          <td className="num muted" style={{ textAlign: 'right' }}>{b.lots > 0 ? `${b.lots} 手` : '不足 1 手'}</td>
                          <td className="num" style={{ textAlign: 'right' }}>{b.horizon} 日</td>
                        </tr>
                      ))}
                      <tr>
                        <td colSpan={5} style={{ textAlign: 'right' }} className="muted">合计</td>
                        <td className="num" style={{ textAlign: 'right', fontWeight: 700 }}>
                          {yuan(acct.nextBuys.reduce((a, b) => a + b.amount, 0))}
                        </td>
                        <td colSpan={2} />
                      </tr>
                    </tbody>
                  </table>
                )}
              </>
            )}

            {/* 当前持仓 */}
            <div className="colhead" style={{ marginBottom: 6 }}>
              <h2 style={{ fontSize: 15 }}>当前持仓（{acct.positions.length} 笔）</h2>
              <span className="n">
                按 {date} 收盘价盯市 · 成本 = 买入时实际付出的现金（含卖出时扣除的双边成本口径）
              </span>
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
                            <td className="num">{g.firstBuy}</td>
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
                        {[...acct.positions].sort((a, b) => b.mv - a.mv).map((p, i) => (
                          <tr key={i}>
                            <td><EtfCell sid={p.sid} d={p.buyDate} /></td>
                            <td className="muted">{p.sw}</td>
                            <td className="num">{p.buyDate}</td>
                            <td className="num" style={{ textAlign: 'right' }}>
                              {Math.max(0, acct.asOfI - p.buyI)} 日
                            </td>
                            <td className="num" style={{ textAlign: 'right' }}>{num(p.buyPx, 3)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{num(p.lastPx, 3)}</td>
                            <td className="num muted" style={{ textAlign: 'right' }}>{num(p.shares, 0)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(p.amt)}</td>
                            <td className="num" style={{ textAlign: 'right' }}>{yuan(p.mv)}</td>
                            <td className={`num ${p.pnl >= 0 ? 'up' : 'down'}`} style={{ textAlign: 'right', fontWeight: 700 }}>
                              {p.pnl >= 0 ? '+' : ''}{yuan(p.pnl)}（{pct(p.pnlPct)}）
                            </td>
                            <td className="num" style={{ textAlign: 'right' }}>{pct(acct.value > 0 ? p.mv / acct.value : 0, 2, false)}</td>
                            <td className="num muted">{p.endDate || '—'}</td>
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
              权重随价格漂移、<b>没有任何每日再平衡</b>；未投入现金按 {pct(sim?.cash_yield, 2, false)} 年化逐日计息。
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
