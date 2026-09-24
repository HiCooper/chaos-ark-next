import React, { useState } from 'react'
import { Chip, num, pct, DataFillBar } from './ui'
import { useEtfModal } from './etf-modal-host'

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

const HZ_KEYS = ['60', '80', '100'] as const

/**
 * 可点击的 ETF 名称：弹出标的走势核验弹窗（全局宿主在 App 层，含总收益摘要）。
 * focusDate：把弹窗走势图初始视窗聚焦到该交易日（当日结论即结论日，~±240 个交易日）；
 * 不传则默认看最新时间段。
 */
function EtfLink({ name, code, focusDate }: {
  name?: string | null
  code?: string | null
  focusDate?: string
}) {
  const { open, sidOf } = useEtfModal()
  if (!name) return null
  const sid = sidOf(code)
  return (
    <button
      className="linkbtn etf-link"
      title={sid ? '点击查看走势、成交与折算核验' : '该标的暂无走势数据'}
      onClick={() => sid && open(sid, focusDate)}
      style={{ cursor: sid ? 'pointer' : 'default', fontWeight: 700, fontSize: 'inherit' }}
    >
      {name}
    </button>
  )
}

/** 相似样本前瞻统计统一读法：兼容完整 stats（mean_excess/win_rate/mean_maxdd）与紧凑 hz（excess/win/dd） */
function hzCell(s: any) {
  if (!s) return null
  return {
    excess: s.excess !== undefined ? s.excess : s.mean_excess,
    win: s.win !== undefined ? s.win : s.win_rate,
    dd: s.dd !== undefined ? s.dd : s.mean_maxdd,
    samples: s.samples,
    insufficient: s.insufficient,
  }
}

function PickCard({ p, date, plan }: { p: any; date?: string; plan?: DayPlan }) {
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
        {p.stage && <Chip tone="dim">阶段 · {STAGE_TXT[p.stage] || p.stage}</Chip>}
        {hz.low_confidence && <Chip tone="warn">样本偏少</Chip>}
        <div className="pick-score">
          <div className="v num">{num(p.composite, 1)}</div>
          <div className="muted">综合分</div>
        </div>
      </div>

      {/* 真实可交易标的：结论必须落到能买的东西上；点名称弹出走势核验 */}
      <div className="tradable">
        {p.etf ? (
          <>
            <span className="tl">可直接交易</span>
            <EtfLink name={p.etf} code={p.etf_code} focusDate={date} />
            <span className="num muted">{p.etf_code}</span>
            {/* 当日口径的排队位置：只用信号日及之前的数据，不展示 T+1 的成交结果
                （否则历史页面会随新交易日而变，等于把未来数据前置到当日页面上） */}
            {plan && plan.state !== 'unknown' && (
              <Chip
                tone={plan.state === 'ready' ? 'accent' : plan.state === 'queued' ? 'dim' : 'warn'}
                title={plan.detail}
              >
                {plan.state === 'full'
                  ? '当日满仓 · 无空槽'
                  : `第 ${plan.rank}/${plan.total} 位 · ${plan.state === 'ready' ? '名额内' : '名额外'}`}
              </Chip>
            )}
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
            相似样本：胜率 <b className="num">{pct(hz.win_rate, 2, false)}</b>
            {' · '}平均超额{' '}
            <b className="num" style={{ color: (hz.mean_excess || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
              {pct(hz.mean_excess)}
            </b>
            {hz.mean_maxdd !== undefined && ` · 平均回撤 ${pct(hz.mean_maxdd)}`}
          </div>
        )}
      </div>

      {/* 深入：不同持有周期下的相似样本预测（60/80/100 择优的依据摊开看） */}
      {hz.stats && HZ_KEYS.some((H) => hzCell(hz.stats[H])) && (
        <div className="hz-deep">
          <div className="hzd-title">
            持有周期敏感性 · 相似样本实测（超额相对行业等权，★ 为建议周期）
          </div>
          <table className="hzd-table">
            <thead>
              <tr><th>持有周期</th><th>预测超额</th><th>胜率</th><th>平均回撤</th><th>样本</th></tr>
            </thead>
            <tbody>
              {HZ_KEYS.map((H) => {
                const s = hzCell(hz.stats[H])
                if (!s || s.insufficient) return null
                const on = hz.horizon === Number(H)
                return (
                  <tr key={H} className={on ? 'on' : ''}>
                    <td>{H} 日{on ? ' ★' : ''}</td>
                    <td className={`num ${s.excess >= 0 ? 'up' : 'down'}`}>{pct(s.excess)}</td>
                    <td className="num">{pct(s.win, 2, false)}</td>
                    <td className="num down">{pct(s.dd)}</td>
                    <td className="num muted">{s.samples ?? '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

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

const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏', AVOID: '回避', WATCH: '观察' }

/** 一笔在场（或待出手）持仓。数据全部来自结算层对历史结论的重放，不是前端推算。 */
function PosRow({ x, tone }: { x: any; tone?: 'sell' | 'warn' }) {
  const hasRet = x.ret !== null && x.ret !== undefined && !Number.isNaN(x.ret)
  const up = hasRet && x.ret >= 0
  return (
    <div className={`posrow ${tone || ''}`}>
      <div className="r1">
        <span className="nm">{x.sw}</span>
        <span className="etf"><EtfLink name={x.etf} code={x.etf_code} /></span>
        <span className="cd num">{x.etf_code || '—'}</span>
        {x.action && (
          <Chip tone={x.action === 'BUY_NOW' ? 'up' : 'ambush'}>
            {ACT_TXT[x.action] || x.action}
          </Chip>
        )}
        {x.invalid && <Chip tone="warn">失效预警</Chip>}
        {/* 盈亏比例固定在右上角 */}
        <span className={`rt ${hasRet ? (up ? 'up' : 'down') : ''}`}>{pct(x.ret)}</span>
      </div>
      <div className="r2">
        <span>买入 {x.buy_date} @{px(x.buy_price)}</span>
        <span>现价 {px(x.price)}</span>
        <span>已持有 {x.held} 个交易日</span>
      </div>
      <div className="r3">
        {x.exit_hint && <span className="due">命中规则：{x.exit_hint}</span>}
        {x.invalid_note ? ` · ${x.invalid_note}` : ''}
        {x.score !== undefined && x.score !== null ? ` · 综合分 ${num(x.score, 1)}` : ''}
      </div>
    </div>
  )
}

/** 今日出手：只关注当日到期的出手标的。 */
function ActionToday({ a }: { a: any }) {
  const MAIN = '主口径：持有到建议周期结束当日收盘卖出'
  if (!a) {
    return (
      <div className="card">
        <div className="colhead">
          <h2>当日出手</h2>
          <span className="n">需要 ETF 结算数据</span>
        </div>
        <div className="empty">未生成持仓回执（缺少 ETF 结算数据）</div>
      </div>
    )
  }
  const sell = a.sell_today || []
  // 当日无出手时，版式与「当日结论」一致：标题 + 右上角 note + 居中提示
  return (
    <div className="card">
      <div className="colhead">
        <h2>当日出手 · {a.as_of}</h2>
        <span className="n">{sell.length > 0 ? `${sell.length} 笔 · ${MAIN}` : MAIN}</span>
      </div>

      {sell.length === 0 ? (
        <div className="empty">当日无到期标的。主口径下持仓一律持有到建议周期结束，到期日才出手。</div>
      ) : (
        <div className="actsec">
          <div className="sh"><i className="dot sell" />当日到期卖出
            <span className="cnt">{sell.length} 笔</span>
          </div>
          {sell.map((x: any, i: number) => <PosRow key={i} x={x} tone="sell" />)}
        </div>
      )}
    </div>
  )
}

const ACT_TONE: Record<string, 'up' | 'ambush' | 'warn' | 'dim'> = {
  BUY_NOW: 'up', AMBUSH: 'ambush', AVOID: 'warn', WATCH: 'dim',
}

/** 行业周期阶段（引擎 scoring.compute_stage 的展示层标注，仅描述不参与结论） */
const STAGE_TXT: Record<string, string> = {
  SPROUT: '萌发期', CONFIRM: '确认期', MID: '中期', MATURE: '成熟期',
}

/**
 * 行业全景表：当日全部候选行业的评分与前瞻统计。
 * ranking 由引擎逐日导出（ark-1.2.0+）；老日文件用 picks/avoid/watch_top 拼降级版。
 */
function useRankingRows(data: any): any[] {
  return React.useMemo(() => {
    if (data.ranking && data.ranking.length) return data.ranking
    const rows: any[] = [
      ...(data.picks || []).map((p: any) => ({
        code: p.code, name: p.name, level: p.level, composite: p.composite,
        action: p.action, etf: p.etf, etf_code: p.etf_code,
        hz: p.horizon && p.horizon.stats ? p.horizon.stats : null,
        hz_picked: p.horizon ? p.horizon.horizon : null,
      })),
      ...(data.avoid || []).map((a: any) => ({
        code: a.code, name: a.name, level: a.level, composite: a.composite,
        action: 'AVOID', etf: null, etf_code: null, hz: null, hz_picked: null,
      })),
      ...(data.watch_top || []).map((w: any) => ({
        code: w.code, name: w.name, level: w.level, composite: w.composite,
        action: 'WATCH', etf: null, etf_code: null, hz: null, hz_picked: null,
      })),
    ]
    return rows.sort((a, b) => b.composite - a.composite)
  }, [data])
}

function RankingTable({ data }: { data: any }) {
  const rows = useRankingRows(data)
  if (!rows.length) return null
  return (
    <div className="card">
      <div className="colhead">
        <h2>行业全景 · {data.date}</h2>
        <span className="n">{rows.length} 个候选 · 按综合分排序</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>行业</th><th>综合分</th><th>动作</th><th>阶段</th><th>可交易 ETF</th>
            <th>预测超额（建议周期）</th><th className="num">60 日</th><th className="num">80 日</th><th className="num">100 日</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const best = r.hz_picked ? String(r.hz_picked) : null
            const bestCell = best && r.hz ? hzCell(r.hz[best]) : null
            return (
              <tr key={r.code || i} className={r.action === 'BUY_NOW' || r.action === 'AMBUSH' ? 'rank-hit' : ''}>
                <td>
                  {r.name} <span className="muted small">{r.level === 'L1' ? '一级' : '细分'}</span>
                </td>
                <td className="num">{num(r.composite, 1)}</td>
                <td><Chip tone={ACT_TONE[r.action] || 'dim'}>{ACT_TXT[r.action] || r.action || '观察'}</Chip></td>
                <td className="muted">{STAGE_TXT[r.stage] || '—'}</td>
                <td className="muted">
                  {r.etf ? (<><EtfLink name={r.etf} code={r.etf_code} /> <span className="num">{r.etf_code || ''}</span></>) : '—'}
                </td>
                {bestCell ? (
                  <td className={`num ${bestCell.excess >= 0 ? 'up' : 'down'}`}>
                    {pct(bestCell.excess)} <span className="muted small">/{best} 日</span>
                  </td>
                ) : (
                  <td className="muted">—</td>
                )}
                {HZ_KEYS.map((H) => {
                  const s = r.hz ? hzCell(r.hz[H]) : null
                  if (!s || s.insufficient) return <td key={H} className="num muted">—</td>
                  return (
                    <td key={H} className={`num ${s.excess >= 0 ? 'up' : 'down'}${best === H ? ' rank-best' : ''}`}>
                      {pct(s.excess)}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 一条结论「当日」的建仓排队位置（只用当日及之前的数据，不含 T+1 成交结果） */
type DayPlan = {
  /** ready = 在当日新建名额内；queued = 排在名额外；full = 当日已满仓；unknown = 数据缺失 */
  state: 'ready' | 'queued' | 'full' | 'unknown'
  rank?: number
  total?: number
  detail: string
}

/**
 * 用当日 day 文件里的 action_today（持仓/容量）与实际新建上限，算出每条结论当日排第几。
 * 只是**当日口径**：有没有空槽、综合分排第几——刻意不回答「T+1 到底买了没有」，
 * 那是未来数据（同一份 day/<date>.json 会因后续数据而渲染出不同结果），口径上也不该前置。
 */
function buildDayPlan(picks: any[], at: any, maxNewPerDay: number): DayPlan[] {
  const list = picks || []
  const nOpen: number | null = at?.n_open ?? null
  const cap: number | null = at?.max_holdings ?? null
  if (nOpen == null || cap == null) return list.map(() => ({ state: 'unknown' as const, detail: '' }))
  const room = Math.max(0, cap - nOpen)
  const quota = Math.min(room, maxNewPerDay)
  // 名次只在「有可交易标的」的结论之间排：没有标的的结论结算层不会建仓
  const rows = list
    .map((p, i) => ({ i, tradable: !!p.etf_code, score: Number(p.composite) || 0 }))
    .filter((r) => r.tradable)
    .sort((a, b) => b.score - a.score)
  const total = rows.length
  const out: DayPlan[] = list.map(() => ({ state: 'unknown' as const, detail: '' }))
  rows.forEach((r, n) => {
    const rank = n + 1
    const base = `当日结算层 ${nOpen}/${cap} 笔持仓、空槽 ${room} 个，每日最多新建 ${maxNewPerDay} 笔；`
      + `本条综合分第 ${rank}/${total}。`
    if (room === 0) {
      out[r.i] = {
        state: 'full', rank, total,
        detail: base + '没有空槽：新结论要等已有仓位到期释放槽位后才能建仓'
          + '（建仓发生在结论日的下一交易日开盘）。以上只用当日收盘数据，不含成交结果。',
      }
    } else if (rank <= quota) {
      out[r.i] = {
        state: 'ready', rank, total,
        detail: base + '在当日新建名额内——按规则会在下一交易日（T+1）开盘买入，'
          + '实际成交取决于那时的槽位状态。以上只用当日收盘数据。',
      }
    } else {
      out[r.i] = {
        state: 'queued', rank, total,
        detail: base + '排在当日名额之外，当日不建仓。以上只用当日收盘数据。',
      }
    }
  })
  return out
}

function Small({ children }: { children: React.ReactNode }) {
  return <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--ink-3)', marginLeft: 2 }}>{children}</span>
}

export default function Today({ data }: { data: any }) {
  const ri = data.regime_info || {}
  const comps = ri.components || null
  const offline = !!data._offline
  const { maxNewPerDay } = useEtfModal()
  // 当日结算层的在途仓位占用（day 文件里的 action_today，当日收盘口径）
  const at = data.action_today || {}
  const cap: number | null = at.max_holdings ?? null
  const nOpen: number | null = at.n_open ?? null
  const capFull = cap != null && nOpen != null && nOpen >= cap
  // 每条结论「当日」的排队位置：与容量条同源（都用 action_today），保证页面内数字一致
  const dayPicks = data?.picks || []
  const plan = React.useMemo(
    () => buildDayPlan(dayPicks, at, maxNewPerDay),
    [dayPicks, at, maxNewPerDay],
  )
  if (data.error) return <div className="card"><div className="empty">{data.date} 无有效结论：{data.error}</div></div>

  return (
    <>
      {<DataFillBar fill={data.tail_fill} style={{ marginBottom: 12 }} />}
      {offline && (
        <div className="card" style={{ padding: '10px 16px', borderLeft: '3px solid var(--warn)', marginBottom: 14 }}>
          <div className="small" style={{ color: 'var(--ink-2)' }}>
            此历史日的市场环境细分、回避清单、观察区需逐日行情数据，离线无法还原，暂不展示；
            入场结论与当日出手（真实结算）已完整呈现。行情恢复后重跑管线将自动补齐。
          </div>
        </div>
      )}

      <div className="card">
        <div className="env">
          <div className="env-main">
            <div className="muted">市场环境</div>
            <div className="env-regime" style={{ color: data.regime === 'ATTACK' ? 'var(--up)' : data.regime === 'PANIC' ? 'var(--down)' : 'var(--ink)' }}>
              {REGIME_TXT[data.regime] || data.regime}
            </div>
            <div className="env-score">
              热度 {num(ri.env_score, 0)}{ri.confidence != null ? ` · 置信度 ${num(ri.confidence, 2)}` : ''}
              {ri.forced ? ' · 触发极端保护' : ''}
            </div>
          </div>
          {comps ? (
            <>
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
                <div>行业 20 日上涨占比 <b className="num">{pct(ri.breadth20, 2, false)}</b></div>
                <div>行业中位数 20 日 <b className="num">{pct(ri.median_ret20)}</b></div>
                <div>基准 20/60 日 <b className="num">{pct(ri.bench_ret20)}</b> · 回撤 <b className="num">{pct(ri.bench_dd60)}</b></div>
                <div>波动历史分位 <b className="num">{num(ri.vol_pct, 2)}</b></div>
              </div>
            </>
          ) : (
            <div className="small muted" style={{ minWidth: 190 }}>
              环境细分数据暂缺（离线重建）
            </div>
          )}
        </div>
      </div>

      {/* 组合容量：结论层给方向，能否真的建仓还受结算层 32 槽容量约束。
          满宽放在市场环境下方——它同时影响下面的结论栏与出手栏。
          未满仓也展示：让「有槽位 ≠ 一定成交（还要排每日新建名额）」这条口径常驻可见。 */}
      {cap != null && nOpen != null && (
        <div
          className="card"
          style={{ padding: '12px 20px', borderLeft: `3px solid var(--${capFull ? 'warn' : 'accent'})` }}
        >
          <div className="small" style={{ color: 'var(--ink-2)' }}>
            {capFull ? (
              <>
                <b>组合容量已满（{nOpen}/{cap} 笔）</b>：结论层给的是方向，能不能真的建仓由结算层决定——
                没有空槽时，新结论要等已有仓位到期释放槽位后，才能在 T+1 开盘买入；
              </>
            ) : (
              <>
                <b>组合容量 {nOpen}/{cap} 笔（剩 {cap - nOpen} 个空槽）</b>：结论层给的是方向，能不能真的建仓由结算层决定——
                有空槽还要排每日新建名额、按综合分从高到低取，排名在名额外的当日不建仓；
              </>
            )}
            {' '}下面每张结论卡标的是<b>当日口径</b>的排队位置（第 k/m 位、名额内 / 外），
            只用当日收盘数据，不含下一交易日的成交结果——历史页面因此不随新数据变化。
            {' '}「n 笔」是建仓笔数、不去重——同一只 ETF 被反复加仓会各占一笔。
          </div>
        </div>
      )}

      <div className="twocol">
        <div className="card">
          <div className="colhead">
            <h2>当日结论 · {data.date}</h2>
            <span className="n">候选池 {data.universe_size != null ? data.universe_size : '—'} 个行业/赛道</span>
          </div>
          {data.picks.length === 0 ? (
            <div className="empty">当日无行业通过共振门槛。空仓等待本身也是结论。</div>
          ) : (
            data.picks.map((p: any, i: number) => (
              <PickCard key={i} p={p} date={data.date} plan={plan[i]} />
            ))
          )}
        </div>

        <ActionToday a={data.action_today} />
      </div>

      <RankingTable data={data} />

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
