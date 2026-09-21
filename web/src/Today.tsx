import React, { useState } from 'react'
import { Chip, num, pct } from './ui'

const DIM_LABEL: Record<string, string> = {
  mainline: '长期主线',
  pullback: '回调空间',
  exhaust: '抛压衰竭',
  rebound: '触底确认',
  risk: '风险控制',
  regime_fit: '环境适配',
}
const DIM_COLOR: Record<string, string> = {
  mainline: '#1f3a8a', pullback: '#c62828', exhaust: '#7a5af8',
  rebound: '#1b8a4b', risk: '#b45309', regime_fit: '#0e7490',
}
const REGIME_TXT: Record<string, string> = {
  ATTACK: '进攻', NEUTRAL: '中性', DEFEND: '防守', PANIC: '恐慌',
}

function dimColor(v: number) {
  if (v >= 65) return '#1b8a4b'
  if (v >= 50) return '#1f3a8a'
  if (v >= 35) return '#b45309'
  return '#9aa3b2'
}

function PickCard({ p }: { p: any }) {
  const [open, setOpen] = useState(false)
  const buy = p.action === 'BUY_NOW'
  const hz = p.horizon || {}
  const d = p.dims || {}
  const m = p.metrics || {}
  return (
    <div className={`pick ${buy ? 'buy' : 'ambush'}`}>
      <div className="pick-head">
        <span className="pick-name">{p.name}</span>
        <Chip>{p.level === 'L1' ? '一级行业' : '细分赛道'}</Chip>
        <Chip tone={buy ? 'up' : 'ambush'}>{buy ? '入手' : '埋伏'}</Chip>
        {hz.low_confidence && <Chip tone="warn">样本偏少</Chip>}
        <div className="pick-score">
          <div className="v num">{num(p.composite, 1)}</div>
          <div className="muted">综合分</div>
        </div>
      </div>

      {/* 真实可交易标的：结论必须落到能买的东西上 */}
      <div className="tradable">
        {p.etf ? (
          <>
            <span className="tl">可直接交易</span>
            <b>{p.etf}</b>
            <span className="num muted">{p.etf_code}</span>
            {p.level === 'L2' && (
              <span className="muted small">（细分赛道由该 ETF 承载）</span>
            )}
          </>
        ) : (
          <>
            <span className="tl" style={{ color: 'var(--warn)' }}>暂无可交易标的</span>
            <span className="muted small">该赛道目前没有流动性达标的 ETF，方向可参考但无法直接执行</span>
          </>
        )}
      </div>

      <div className="pick-horizon">
        <div>
          <span className="big num">{hz.horizon ?? '—'}</span> 个交易日后评估
          <span className="muted"> · 约 {((hz.horizon || 0) / 21).toFixed(1)} 个月</span>
        </div>
        {hz.exit_est && (
          <div className="small muted">
            预计 {hz.exit_est}
            {hz.exit_est_is_projection ? '（外推）' : ''} 前后考虑出手
            {hz.samples ? ` · 依据 ${hz.samples} 个历史相似样本` : ''}
          </div>
        )}
        {hz.win_rate !== undefined && hz.win_rate !== null && (
          <div className="small">
            相似样本：胜率 <b className="num">{pct(hz.win_rate, 0, false)}</b>
            {' · '}平均超额{' '}
            <b className="num" style={{ color: (hz.mean_excess || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
              {pct(hz.mean_excess)}
            </b>
            {hz.mean_maxdd !== undefined && ` · 平均回撤 ${pct(hz.mean_maxdd)}`}
          </div>
        )}
      </div>

      <div className="dims">
        {Object.keys(DIM_LABEL).map((k) => (
          <div className="dim" key={k}>
            <div className="l">
              <span>{DIM_LABEL[k]}</span>
              <b className="num">{d[k] !== undefined ? d[k].toFixed(0) : '—'}</b>
            </div>
            <div className="b">
              <i style={{ width: `${Math.max(3, d[k] || 0)}%`, background: DIM_COLOR[k] }} />
            </div>
          </div>
        ))}
      </div>

      {p.trigger && (
        <div className="small" style={{ marginTop: 8, color: 'var(--ink-2)' }}>
          <b>触发：</b>{p.trigger}
        </div>
      )}

      <ul className="reasons">
        {(p.reasons || []).map((r: string, i: number) => (
          <li key={i}>{r}</li>
        ))}
      </ul>

      <div style={{ marginTop: 6 }}>
        <button className="toggle" onClick={() => setOpen(!open)}>
          {open ? '▾ 收起失效条件' : '▸ 失效条件（什么情况下这个结论作废）'}
        </button>
        {open && (
          <ul className="invalid">
            {(p.invalidation || []).map((r: string, i: number) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        )}
      </div>

      <div className="small muted" style={{ marginTop: 8 }}>
        120日 {pct(m.ret_120)} · 250日 {pct(m.ret_250)} · 距MA120 {pct(m.dist_ma120)} ·
        量比 {num(m.vol_ratio)} · 年化波动 {num(m.vol20)}
      </div>
    </div>
  )
}

function px(v?: number | null) {
  return v === null || v === undefined ? '—' : v.toFixed(3)
}

const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏' }

/** 一笔在场（或待出手）持仓。数据全部来自结算层对历史结论的重放，不是前端推算。 */
function PosRow({ x, tone }: { x: any; tone?: 'sell' | 'warn' }) {
  const up = (x.ret ?? 0) >= 0
  return (
    <div className={`posrow ${tone || ''}`}>
      <div className="r1">
        <span className="nm">{x.sw}</span>
        <span className="etf">{x.etf}</span>
        <span className="cd num">{x.etf_code}</span>
        {x.action && (
          <Chip tone={x.action === 'BUY_NOW' ? 'up' : 'ambush'}>
            {ACT_TXT[x.action] || x.action}
          </Chip>
        )}
        {x.invalid && <Chip tone="warn">失效预警</Chip>}
      </div>
      <div className="r2">
        <span>买入 {x.buy_date} @{px(x.buy_price)}</span>
        <span>现价 {px(x.price)}</span>
        <span className={`rt ${up ? 'up' : 'down'}`}>{pct(x.ret)}</span>
        <span>已持有 {x.held} 个交易日</span>
        <span className="due">建议周期 {x.horizon} 日</span>
      </div>
      <div className="r3">
        {x.exit_hint}
        {x.invalid_note ? ` · ${x.invalid_note}` : ''}
        {x.score !== undefined ? ` · 综合分 ${num(x.score, 1)}` : ''}
      </div>
    </div>
  )
}

/** 今日出手：从历史结论重放出的持仓里，挑出命中交易规则需要动手的标的。 */
function ActionToday({ a }: { a: any }) {
  if (!a) {
    return (
      <div className="card">
        <div className="colhead"><h2>今日出手</h2></div>
        <div className="empty">未生成持仓回执（缺少 ETF 结算数据）</div>
      </div>
    )
  }
  const sell = a.sell_today || []
  const warn = a.warn || []
  const soon = a.sell_soon || []
  const open = a.open || []
  return (
    <div className="card">
      <div className="colhead">
        <h2>今日出手 · {a.as_of}</h2>
        <span className="n">在场持仓 {a.n_open} 笔</span>
      </div>

      <div className="actkpi">
        <div className={`k ${sell.length ? 'sell' : ''}`}>
          <div className="kl">今日到期出手</div>
          <div className="kv num">{sell.length}<Small>笔</Small></div>
        </div>
        <div className={`k ${warn.length ? 'warn' : ''}`}>
          <div className="kl">失效条件预警</div>
          <div className="kv num">{warn.length}<Small>笔</Small></div>
        </div>
        <div className="k">
          <div className="kl">{a.soon_days ?? 5} 日内到期</div>
          <div className="kv num">{soon.length}<Small>笔</Small></div>
        </div>
        <div className="k">
          <div className="kl">平均已持有</div>
          <div className="kv num">{a.avg_held ?? '—'}<Small>日</Small></div>
        </div>
      </div>

      <div className="actsec">
        <div className="sh"><i className="dot sell" />今日到期出手
          <span className="cnt">{sell.length} 笔 · 主口径：持有到建议周期结束当日收盘卖出</span>
        </div>
        {sell.length === 0
          ? <div className="muted small" style={{ padding: '4px 0' }}>
              今日无到期标的。主口径下持仓一律持有到建议周期结束，到期日才出手。
            </div>
          : sell.map((x: any, i: number) => <PosRow key={i} x={x} tone="sell" />)}
      </div>

      <div className="actsec">
        <div className="sh"><i className="dot warn" />失效条件预警
          <span className="cnt">{warn.length} 笔 · 结论写明的失效条件已触发（对照口径 B 的离场规则）</span>
        </div>
        {warn.length === 0
          ? <div className="muted small" style={{ padding: '4px 0' }}>无持仓触发失效条件。</div>
          : warn.map((x: any, i: number) => <PosRow key={i} x={x} tone="warn" />)}
      </div>

      <div className="actsec">
        <div className="sh"><i className="dot soon" />{a.soon_days ?? 5} 个交易日内即将出手
          <span className="cnt">{soon.length} 笔</span>
        </div>
        {soon.length === 0
          ? <div className="muted small" style={{ padding: '4px 0' }}>未来 {a.soon_days ?? 5} 个交易日内无持仓到期。</div>
          : soon.map((x: any, i: number) => <PosRow key={i} x={x} />)}
      </div>

      <div className="actsec">
        <div className="sh"><i className="dot hold" />在场持仓全貌
          <span className="cnt">{open.length} 笔 · 按到期日排序</span>
        </div>
        <div className="subscroll">
          {open.length === 0
            ? <div className="muted small">当前无在场持仓。</div>
            : open.map((x: any, i: number) => <PosRow key={i} x={x} />)}
        </div>
      </div>

      <div className="actnote">{a.rule}</div>
    </div>
  )
}

function Small({ children }: { children: React.ReactNode }) {
  return <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--ink-3)', marginLeft: 2 }}>{children}</span>
}

export default function Today({ data }: { data: any }) {
  const ri = data.regime_info || {}
  const comps = ri.components || {}
  if (data.error) return <div className="card"><div className="empty">{data.date} 无有效结论：{data.error}</div></div>

  return (
    <>
      <div className="card">
        <div className="env">
          <div className="env-main">
            <div className="muted">市场环境</div>
            <div className="env-regime" style={{ color: data.regime === 'ATTACK' ? 'var(--up)' : data.regime === 'PANIC' ? 'var(--down)' : 'var(--ink)' }}>
              {REGIME_TXT[data.regime] || data.regime}
            </div>
            <div className="env-score">
              热度 {num(ri.env_score, 0)} · 置信度 {num(ri.confidence, 2)}
              {ri.forced ? ' · 触发极端保护' : ''}
            </div>
          </div>
          <div className="env-parts">
            {[
              ['宽度', comps.breadth], ['趋势', comps.trend], ['动能', comps.momentum],
              ['低波', comps.vol], ['流动性', comps.liquidity],
            ].map(([l, v]: any, i) => (
              <div className="env-part" key={i}>
                <div className="l">{l}</div>
                <div className="b"><i style={{ width: `${Math.max(3, v || 0)}%` }} /></div>
              </div>
            ))}
          </div>
          <div style={{ minWidth: 190 }} className="small muted">
            <div>行业 20 日上涨占比 <b className="num">{pct(ri.breadth20, 0, false)}</b></div>
            <div>行业中位数 20 日 <b className="num">{pct(ri.median_ret20)}</b></div>
            <div>基准 20/60 日 <b className="num">{pct(ri.bench_ret20)}</b> · 回撤 <b className="num">{pct(ri.bench_dd60)}</b></div>
            <div>波动历史分位 <b className="num">{num(ri.vol_pct, 2)}</b></div>
          </div>
        </div>
      </div>

      <div className="twocol">
        <div className="card">
          <div className="colhead">
            <h2>今日结论 · {data.date}</h2>
            <span className="n">候选池 {data.universe_size} 个行业/赛道</span>
          </div>
          <div className="note" style={{ marginBottom: 14 }}>{data.summary}</div>
          {data.picks.length === 0 ? (
            <div className="empty">今日无行业通过共振门槛。空仓等待本身也是结论。</div>
          ) : (
            data.picks.map((p: any, i: number) => <PickCard key={i} p={p} />)
          )}
        </div>

        <ActionToday a={data.action_today} />
      </div>

      {data.avoid && data.avoid.length > 0 && (
        <div className="card">
          <h2>回避清单（有信号但风险不满足）</h2>
          <table>
            <thead>
              <tr><th>行业</th><th>综合分</th><th>回避原因</th></tr>
            </thead>
            <tbody>
              {data.avoid.map((a: any, i: number) => (
                <tr key={i}>
                  <td>{a.name} <span className="muted">{a.level === 'L1' ? '一级行业' : '细分赛道'}</span></td>
                  <td className="num">{num(a.composite, 1)}</td>
                  <td className="muted">{(a.why || []).join('；')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.watch_top && data.watch_top.length > 0 && (
        <div className="card">
          <h2>观察区（未达共振门槛）</h2>
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            {data.watch_top.map((w: any, i: number) => (
              <Chip key={i}>{w.name} · {num(w.composite, 1)}</Chip>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
