import React, { useEffect, useMemo, useState } from 'react'
import { NavChart } from './chart'
import { Bars, Card, Chip, Marker, MarkerLine, Metric, num, pct } from './ui'
import { useEtfModal } from './etf-modal-host'

const REGIME_TXT: Record<string, string> = { ATTACK: '进攻', NEUTRAL: '中性', DEFEND: '防守', PANIC: '恐慌' }
const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏' }
const PHASE_TXT: Record<string, string> = { BULL: '上行段', BEAR: '下行段' }

function verdictChip(v: string) {
  if (v === 'RIGHT') return <Chip tone="up">做对了</Chip>
  if (v === 'WRONG') return <Chip tone="down">做错了</Chip>
  if (v === 'PENDING') return <Chip>持有中</Chip>
  return <Chip>中性</Chip>
}

function CaseTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <div className="empty">暂无样本</div>
  return (
    <table>
      <thead>
        <tr>
          <th>日期</th><th>行业</th><th>类型</th><th>周期</th>
          <th style={{ textAlign: 'right' }}>区间收益</th>
          <th style={{ textAlign: 'right' }}>超额</th>
          <th>为什么</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c, i) => (
          <tr key={i}>
            <td className="num">
              {c.date}
              <div className="small muted">{c.basis === 'ETF' ? (c.etf || 'ETF') : '指数口径'}</div>
            </td>
            <td><b>{c.name}</b></td>
            <td>{ACT_TXT[c.action] || c.action}</td>
            <td className="num">{c.horizon}日</td>
            <td className="num" style={{ textAlign: 'right', color: (c.ret || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
              {pct(c.ret)}
            </td>
            <td className="num" style={{ textAlign: 'right', color: (c.ex_ew || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
              {pct(c.ex_ew)}
            </td>
            <td className="muted" style={{ fontSize: 11.5 }}>
              {(c.attribution || []).join('；') || '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function Review({ data, hist }: { data: any; hist?: any }) {
  const o = data.overview || {}
  const st = data.settlement || {}
  const sts = st.stats || {}
  const ry = (sts as any).rolling_1y || null
  // 独立起账的一年分布（每个交易日都当作入市日，各起一本新账）：预期管理口径
  const rf = (sts as any).rolling_1y_fresh || null
  const curve = st.curve || {}
  const rc = st.risk_control || {}
  const floorScan: any[] = rc.floor_scan || []
  // 集中度上限：同一只 ETF 同时能占几个槽位。只作取舍展示，未进生产配置。
  const conc = st.concentration || {}
  const concScan: any[] = conc.scan || []
  const eff = sts.effective || {}
  const attr = data.attribution || {}
  const wrong = attr.wrong_distribution || []
  const succ = attr.success_distribution || []
  const impr = data.improvements || []
  const cases = data.cases || {}
  const recent = data.recent || []
  const [basisOpen, setBasisOpen] = useState(false)
  // 标的走势核验弹窗：宿主在 App 层（EtfModalHost），本页只拿 open 句柄
  const { open: openEtf } = useEtfModal()
  // 全池份额折算断点清单（web/public/data/etf/_breaks.json，由 engine/build_etf_webdata.py 生成）
  const [breaks, setBreaks] = useState<any[] | null>(null)
  useEffect(() => {
    fetch('data/etf/_breaks.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => setBreaks(j.breaks || []))
      .catch(() => setBreaks([]))
  }, [])

  const phases = (st.by_phase || []).filter((p: any) => p.n > 0)
  const coverage = (st.coverage || []).filter((c: any) => Number(c.year) >= 2018)

  // ==================== 净值曲线上的买卖点（B/S） ====================
  // 事件全部来自结算层真实成交回执：B = T+1 开盘买入，S = 到期收盘卖出。
  const allDates: string[] = curve.dates || []
  const allNav: number[] = curve.nav || []
  const allBench: number[] = curve.bench || []
  const events: any[] = st.events || []
  const etfNames: Record<string, string> = st.etf_names || {}
  // 辅助轴数据源：逐日持仓笔数（右轴「持仓仓位」= 笔数/容量）。
  // 只回答「是否持仓、持仓多少」；实际投入的资金比例由波动目标决定，不在这条轴上表达。
  const pos = st.position || {}
  const posHoldings: number[] = pos.holdings || []
  const posExposure: number[] = pos.exposure || []
  const posMax: number = pos.max_holdings || 32
  // 平均持仓时间：组合口径（真实成交的那批）与信号口径（全部可结算结论）
  const hold = st.holding || {}
  const hp = hold.portfolio || {}
  const hs = hold.per_signal || {}
  const hd = hp.dist || {}
  const hdTop = Object.entries(hd).sort((a: any, b: any) => b[1] - a[1])[0] as
    | [string, number] | undefined
  const hz = (hp.by_horizon || {}) as Record<string, any>
  const hzTxt = Object.keys(hz).length
    ? Object.entries(hz).map(([k, v]: any) => `${k}日 ${num(v.avg, 0)}日`).join(' · ')
    : undefined

  // 每年在曲线上的索引区间，由真实交易日推出
  const yrRange: Record<string, [number, number]> = {}
  allDates.forEach((d, i) => {
    const y = d.slice(0, 4)
    if (!yrRange[y]) yrRange[y] = [i, i]
    yrRange[y][1] = i
  })
  // 年份页签只保留"真有行情"的年份：曲线的第 0 个点是估值基点（如 2017-12-29 仅 1 天），
  // 单独成 tab 会点进去看到一条空图。阈值取 20 个交易日（不足一个月的残年同样不列）。
  const yrList = Object.keys(yrRange)
    .filter((y) => yrRange[y][1] - yrRange[y][0] + 1 >= 20)
  const [year, setYear] = useState<string>(yrList.length ? yrList[yrList.length - 1] : 'all')

  const [lo, hi] = year === 'all'
    ? [0, Math.max(0, allDates.length - 1)]
    : (yrRange[year] || [0, Math.max(0, allDates.length - 1)])
  // 切到某一年时把曲线归一化到该年起点，否则长周期会把年度波动压平
  const navBase = year === 'all' ? 1 : (allNav[lo - 1] || allNav[lo] || 1)
  const benchBase = year === 'all' ? 1 : (allBench[lo - 1] || allBench[lo] || 1)
  const viewDates = allDates.slice(lo, hi + 1)
  const viewNav = allNav.slice(lo, hi + 1).map((v) => v / navBase)
  const viewBench = allBench.slice(lo, hi + 1).map((v) => v / benchBase)

  // ==================== 分年度（账户口径） ====================
  // 账户收益/年化/基准/超额来自同一条资金曲线；平均投出、平均持仓来自逐日仓位序列；
  // 结论数/胜率/结论均收益来自信号口径 by_year；成交 = 该年建仓（B）事件数。
  // 与年份页签同一过滤规则：不足 20 个交易日的残年不列（曲线第 0 点是 2017-12-29 估值基点）。
  const yearly = React.useMemo(() => {
    const mean = (a: number[]) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0)
    const out: any[] = []
    let i = 0
    while (i < allDates.length) {
      const y = allDates[i].slice(0, 4)
      let j = i
      while (j + 1 < allDates.length && allDates[j + 1].slice(0, 4) === y) j++
      const days = j - i + 1
      if (days >= 20) {
        const base = allNav[i - 1] ?? allNav[i]
        const bbase = allBench[i - 1] ?? allBench[i]
        const ret = base > 0 ? allNav[j] / base - 1 : 0
        const bret = bbase > 0 ? allBench[j] / bbase - 1 : 0
        const v = (st.by_year || {})[y] || {}
        out.push({
          y, ret, bench: bret, excess: ret - bret,
          ann: ret > -1 ? Math.pow(1 + ret, 243 / days) - 1 : 0,
          dep: mean(posExposure.slice(i, j + 1)), hold: mean(posHoldings.slice(i, j + 1)),
          n: v.n, win: v.win_rate, avgRet: v.avg_ret, avgEx: v.avg_excess,
          buys: events.filter((e: any) => e.k === 'B' && e.i >= i && e.i <= j).length,
          partial: days < 200,
        })
      }
      i = j + 1
    }
    return out
  }, [allDates, allNav, allBench, posExposure, posHoldings, events, st.by_year])

  // 明细表右上角的年化收益：全区间直接用结算层 cagr（引擎同一来源）；
  // 选具体年份则用该年区间净值实时年化（起点 = 上一年末收盘，与图表归一化口径一致）
  const segAnn: number | null = (() => {
    if (year === 'all') return typeof sts.cagr === 'number' ? sts.cagr : null
    const n0 = allNav[lo - 1] ?? allNav[lo]
    const n1 = allNav[hi]
    if (!n0 || !n1 || !allDates[hi]) return null
    const days = (new Date(allDates[hi]).getTime() - new Date(allDates[lo]).getTime()) / 86400000 + 1
    if (days <= 0) return null
    return Math.pow(n1 / n0, 365 / days) - 1
  })()

  const idxOfDate: Record<string, number> = {}
  allDates.forEach((d, i) => { idxOfDate[d] = i })

  // ==================== 每日结论（历史结论库，与曲线逐日对齐） ====================
  // 结论 ≠ 成交：容量上限（最多同时持有 N 笔、每日最多建 2 笔）会挡下大部分结论。
  // 把每天的结论也标到图上，才能直观看出「发出了结论」与「真实建了仓」之间的差。
  const conclByDate = useMemo(() => {
    const m: Record<string, any> = {}
    for (const d of (hist?.days || [])) m[d.date] = d
    return m
  }, [hist])
  const viewConcl = useMemo(
    () => viewDates.map((d) => conclByDate[d] || null),
    [viewDates.join('|'), conclByDate],
  )
  const hasConcl = viewConcl.some((c) => !!(c && c.picks && c.picks.length))
  const nConclDays = viewConcl.filter((c) => !!(c && c.picks && c.picks.length)).length

  // 卖出原因的短标签与全称：结算层给的 why 是事前定义的口径，不是事后编的
  const shortReason = (w?: string): { tag?: string; tone?: MarkerLine['tagTone'] } => {
    if (!w) return {}
    if (w.includes('失效条件')) return { tag: '失效退出', tone: 'exit' }
    if (w.includes('顺延')) return { tag: '顺延平仓', tone: 'amb' }
    if (w.includes('到期')) return { tag: '持有到期', tone: 'mute' }
    return { tag: '已平仓', tone: 'mute' }
  }

  // 同一天的成交合并成一个标记点，悬浮一次看全。
  // 买入统一用红圈字母 B（不区分入手/埋伏——两者都是「买」），
  // 是入手还是埋伏在悬浮明细里逐笔标注，图上只保留「买 / 卖」两个语义。
  const markers: Marker[] = (() => {
    const groups = new Map<string, any[]>()
    for (const e of events) {
      if (e.i < lo || e.i > hi) continue
      const key = `${e.i}|${e.k}`
      const arr = groups.get(key)
      if (arr) arr.push(e)
      else groups.set(key, [e])
    }
    const out: Marker[] = []
    for (const [key, arr] of groups) {
      const [ii, k] = key.split('|')
      const i0 = Number(ii)
      const buy = k === 'B'
      const inv = posHoldings[i0]
      const hs = Array.from(new Set(arr.map((e: any) => e.h).filter((x: any) => x != null)))
      const reasons = Array.from(
        new Set(arr.map((e: any) => e.why).filter((x: any) => !!x)),
      ) as string[]
      const nAmb = arr.filter((e: any) => e.a === 'AMBUSH').length
      const mix = buy && nAmb > 0 && nAmb < arr.length
        ? `（入手 ${arr.length - nAmb} / 埋伏 ${nAmb}）`
        : (buy && nAmb > 0 ? '（埋伏）' : '')
      out.push({
        i: i0 - lo,
        k: buy ? 'B' : 'S',
        title: `${allDates[i0]} · ${buy ? '买入' : '卖出'} ${arr.length} 笔${mix}`,
        foot: buy
          ? (hs.length ? `建议持有周期 ${hs.join(' / ')} 个交易日` : undefined)
          : (reasons.length ? `卖出原因：${reasons.join('；')}` : undefined),
        pos: (inv != null && !Number.isNaN(inv))
          ? `${pct(inv, 2, false)}（持仓 ${posHoldings[i0] ?? '—'}/${posMax} 笔）`
          : undefined,
        lines: (() => {
          // 同一天可能有多条结论落到同一只 ETF（不同细分赛道共用代理标的），
          // 合并成一行并标注笔数，否则看起来像是重复记录。
          const bySid = new Map<string, any[]>()
          for (const e of arr) {
            const key = buy ? e.sid : `${e.sid}|${e.ret}`
            const g = bySid.get(key)
            if (g) g.push(e)
            else bySid.set(key, [e])
          }
          const out2: Marker['lines'] = []
          for (const g of bySid.values()) {
            const e = g[0]
            const nm = etfNames[e.sid] || e.sid
            const code = String(e.sid).split('.')[1] || ''
            const times = g.length > 1 ? ` ×${g.length}` : ''
            if (buy) {
              const amb = e.a === 'AMBUSH'
              out2.push({
                text: `买 ${nm} ${code}${times}`,
                tag: amb ? '埋伏' : '入手',
                tagTone: amb ? 'amb' : 'in',
                sub: `@${num(e.price, 3)}`,
                tone: 'mute',
              })
            } else {
              const r = e.ret
              const sr = shortReason(e.why)
              const held = e.bd ? (i0 - (idxOfDate[e.bd] ?? i0)) : null
              out2.push({
                text: `卖 ${nm} ${code}${times}`,
                tag: sr.tag,
                tagTone: sr.tone,
                sub: `${held != null ? `持${held}日 ` : ''}${pct(r)}`,
                tone: (r ?? 0) >= 0 ? 'up' : 'down',
              })
            }
          }
          return out2
        })(),
      })
    }
    out.sort((a, b) => a.i - b.i)
    return out
  })()

  // 悬浮：图上一次悬浮给出「这一天净值多少 / 当天结论是什么 / 当天成交了什么」。
  // 同一天可能同时有买入和卖出（到期平仓与新开仓撞在一天），B/S 是两个 Marker，
  // 必须按数组收纳再逐个渲染——用单值 Map 会让后写的一方覆盖先写的一方，
  // 悬浮就只剩一边的成交信息。
  const mkByI = new Map<number, Marker[]>()
  for (const mk of markers) {
    const arr = mkByI.get(mk.i)
    if (arr) arr.push(mk)
    else mkByI.set(mk.i, [mk])
  }
  const tradeHtml = (i: number): string => {
    const mks = mkByI.get(i)
    if (!mks || !mks.length) return ''
    return mks.map((mk) => {
      const rows = mk.lines.map((l) => `<div class="ek-pick">
        <span class="ek-g">${l.tag ? `<em class="ek-tag ${l.tagTone || 'mute'}">${l.tag}</em>` : ''}<b>${l.text}</b></span>
        <span class="ek-dim ${l.tone === 'up' ? 'ek-up' : l.tone === 'down' ? 'ek-down' : ''}">${l.sub || ''}</span>
      </div>`).join('')
      return `<div class="ek-sec">当日成交 · ${mk.k === 'B' ? '买入' : '卖出'}</div>${rows}
        ${mk.foot ? `<div class="ek-mute">${mk.foot}</div>` : ''}`
    }).join('')
  }

  // 逐笔成交明细（一行 = 一次完整交易，取自卖出回执）
  const tradeRows = events
    .filter((e: any) => e.k === 'S' && e.i >= lo && e.i <= hi)
    .map((e: any) => ({
      buyDate: e.bd, buyPx: e.bpx, sellDate: allDates[e.i], sellPx: e.price,
      sw: e.sw, sid: e.sid, code: String(e.sid).split('.')[1] || '',
      etf: etfNames[e.sid] || e.sid, h: e.h, act: e.a, why: e.why,
      held: e.bd ? (e.i - (idxOfDate[e.bd] ?? e.i)) : null,
      ret: e.ret,
      ex: e.ex,
    }))
    .sort((a, b) => (b.sellDate || '').localeCompare(a.sellDate || ''))
  const shownTrades = tradeRows.slice(0, 400)


  return (
    <>
      <Card
        title="真实交易结算（ETF）"
        extra={
          <button className="linkbtn" onClick={() => setBasisOpen(!basisOpen)}>
            {basisOpen ? '收起口径说明' : '查看口径说明'}
          </button>
        }
      >
        <div className="grid g4" style={{ marginBottom: 12 }}>
          <Metric k={`年化收益（${sts.years || '—'} 年）`} v={pct(sts.cagr)}
            n={`基准 ${pct(sts.bench_cagr)} · 年化超额 ${pct(sts.excess_cagr)}`}
            tone={(sts.excess_cagr || 0) >= 0 ? 'up' : 'down'} />
          <Metric k="最大回撤" v={pct(sts.maxdd, 2, false)}
            n={`基准 ${pct(sts.bench_maxdd, 2, false)} · 最长水下 ${sts.max_underwater_days || '—'} 日`}
            tone="down" />
          <Metric k="方向判断胜率" v={pct(st.win_rate_excess, 2, false)}
            n={`跑赢基准口径 · 绝对盈利胜率 ${pct(st.win_rate_abs, 2, false)}`}
            tone={(st.win_rate_excess || 0) >= 0.5 ? 'up' : undefined} />
          <Metric k="Calmar / 夏普" v={`${num(sts.calmar, 2)} / ${num(sts.sharpe, 2)}`}
            n={`年化 ÷ |最大回撤|：每承受 1 份回撤换到的年化 · 月度胜率 ${pct(sts.monthly_win_rate, 2, false)}`} />
        </div>

        {(rf || ry) && (
          <div style={{ marginBottom: 12 }}>
            <div className="grid g4">
              <Metric k="随机入场 · 持有一年（中位）" v={pct((rf || ry).median)}
                n={rf && rf.bench_median != null
                  ? `同期基准中位 ${pct(rf.bench_median)} · 样本 ${rf.n} 个入市日`
                  : `均值 ${pct((rf || ry).mean)} · 样本 ${(rf || ry).n} 个入市日`}
                tone={rf && rf.bench_median != null
                  ? ((rf.median || 0) >= rf.bench_median ? 'up' : 'down')
                  : ((rf || ry).median || 0) >= 0 ? 'up' : 'down'} />
              <Metric k="随机入场 · 持有一年（均值）" v={pct((rf || ry).mean)}
                n={rf && rf.bench_mean != null
                  ? `同期基准均值 ${pct(rf.bench_mean)} · 均值跑赢，中位未必`
                  : '分布右偏：少数年份的收益把均值抬起来'}
                tone={rf && rf.bench_mean != null
                  ? ((rf.mean || 0) >= rf.bench_mean ? 'up' : 'down')
                  : ((rf || ry).mean || 0) >= 0 ? 'up' : 'down'} />
              <Metric k="正收益概率" v={pct((rf || ry).win_rate, 2, false)}
                n={rf && rf.beat_rate != null
                  ? `跑赢同期买入持有基准 ${pct(rf.beat_rate, 2, false)} 的入市日`
                  : '任意交易日入市、持有 252 个交易日为正的比例'}
                tone={((rf || ry).win_rate || 0) >= 0.5 ? 'up' : 'down'} />
              <Metric k="最差 5%（p5）/ 最差" v={`${pct((rf || ry).p5)} / ${pct((rf || ry).worst)}`}
                n={`最好 ${pct((rf || ry).best)} · 这就是「半山腰进场」的下限`} tone="down" />
            </div>
            <div className="note" style={{ marginTop: 8 }}>
              <b>这是预期管理最重要的一行：随机挑一天入市、只持有一年，中位结果是
              {pct((rf || ry).median)}，{rf && rf.bench_median != null
                ? <>而同期买入持有基准的中位是 {pct(rf.bench_median)}——<b>中位跑输</b>；
                  均值 {pct(rf.mean)} 才高于基准的 {pct(rf.bench_mean)}，
                  跑赢概率 {pct(rf.beat_rate, 2, false)}</>
                : <>并不稳定为正</>}。</b>
              分布右偏：多数年份平淡甚至亏损，收益集中在少数年份——<b>不要把年化 12% 当成"每年都能拿到"</b>。
              {rf && <> 口径：{rf.n} 个入市日各自<b>从现金重新起一本账</b>（账户状态也重来），
                持有 {rf.horizon} 个交易日（T+1 开盘成交、双边成本、只买不卖、无再平衡），
                与「从数据起点一路跟随」的滚动一年（中位 {pct(ry && ry.median)}）是两个不同的问题。</>}
              {!rf && ' 注意：当前页面拿到的是净值曲线自身的 252 日滚动窗口（连续跟随口径），'
                + '不是"每天各自起账"的独立口径，请以最新一次完整引擎运行为准。'}
              历史模拟，不含样本外折扣，不构成收益承诺。
            </div>
          </div>
        )}

        <div className="grid g4" style={{ marginBottom: 12 }}>
          <Metric k="累计收益" v={pct(sts.total_return)} tone={(sts.total_return || 0) >= 0 ? 'up' : 'down'} />
          <Metric k={`基准（${st.bench_etf || '沪深300ETF'}）`} v={pct(sts.bench_total)} />
          <Metric k="可结算结论" v={`${st.n_settled || 0}`}
            n={`结论总数 ${st.n_total || 0} 条，其余 ${st.n_signal_only || 0} 条无对应可交易 ETF`} />
          <Metric k="单笔平均" v={pct(st.avg_ret)}
            n={`平均超额 ${pct(st.avg_excess)} · 平均最大回撤 ${pct(st.avg_maxdd)}`}
            tone={(st.avg_ret || 0) >= 0 ? 'up' : 'down'} />
        </div>

        {hp.avg_days != null && (
          <div className="grid g4" style={{ marginBottom: 12 }}>
            <Metric k="平均持仓时间（实际成交）" v={`${num(hp.avg_days, 1)} 日`}
              n={`中位 ${num(hp.median_days, 1)} 日 · 约 ${num(hp.avg_months, 1)} 个月 · 样本 ${hp.n} 笔`} />
            <Metric k="最短 / 最长持仓" v={`${hp.min_days ?? '—'} / ${hp.max_days ?? '—'} 日`}
              n={hdTop ? `最集中的一档：${hdTop[0]}（${hdTop[1]} 笔）` : undefined} />
            <Metric k="分建议周期" v={`${Object.keys(hz).length} 档`} n={hzTxt} />
            <Metric k="若执行失效条件提前退出" v={`${num(hold.early?.avg_days, 1)} 日`}
              n={`对照口径 · 同一批结论（${hold.early?.n ?? 0} 笔）· 信号口径 ${num(hs.avg_days, 1)} 日`} />
          </div>
        )}

        {(rc.why || floorScan.length > 1 || concScan.length > 1) && (
          <div className="rcbox">
            {floorScan.length > 1 && (
              <>
                <div className="rc-sub" style={{ fontSize: 12, color: 'var(--ink-2)', margin: '12px 0 4px' }}>
                  <b>最低仓位旋钮</b>——回撤与年化的取舍是显式的，不是藏起来的（策略运行期口径）：
                </div>
                <table className="rctab">
                  <thead>
                    <tr>
                      <th>最低仓位</th><th>年化</th><th>最大回撤</th>
                      <th>夏普</th><th>最长水下</th><th>平均暴露</th>
                    </tr>
                  </thead>
                  <tbody>
                    {floorScan.map((r: any) => (
                      <tr key={r.floor}
                        className={Math.abs(r.floor - (rc.config?.vol_floor ?? 0.8)) < 1e-9 ? 'hl' : ''}>
                        <td>{pct(r.floor, 2, false)}</td>
                        <td className="num">{pct(r.eff_cagr)}</td>
                        <td className="num">{pct(r.eff_maxdd, 2, false)}</td>
                        <td className="num">{num(r.eff_sharpe)}</td>
                        <td className="num">{r.eff_underwater}</td>
                        <td className="num">{pct(r.avg_exposure, 2, false)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {concScan.length > 1 && (
              <>
                <div className="rc-sub" style={{ fontSize: 12, color: 'var(--ink-2)', margin: '14px 0 4px' }}>
                  <b>集中度上限</b>——同一只 ETF 同时能占几个槽位。
                  这一档我们<b>试过但没有采纳</b>，把账摊开让你自己判断：
                </div>
                <table className="rctab">
                  <thead>
                    <tr>
                      <th>同 ETF 槽位上限</th><th>年化</th><th>最大回撤</th>
                      <th>夏普</th><th>方向胜率</th><th>成交笔数</th><th>挡下</th>
                    </tr>
                  </thead>
                  <tbody>
                    {concScan.map((r: any) => (
                      <tr key={String(r.cap)} className={r.cap == null ? 'hl' : ''}>
                        <td>{r.cap == null ? '不限制（当前）' : `≤ ${r.cap}`}</td>
                        <td className="num">{pct(r.cagr)}</td>
                        <td className="num">{pct(r.maxdd, 2, false)}</td>
                        <td className="num">{num(r.sharpe)}</td>
                        <td className="num">{pct(r.win_excess, 2, false)}</td>
                        <td className="num">{r.n}</td>
                        <td className="num">{r.blocked ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="note" style={{ marginTop: 8 }}>
                  <b>为什么不采纳</b>：{conc.verdict}
                  <br />
                  <b>它针对的问题</b>：{conc.why}
                </div>
              </>
            )}

            <div className="note" style={{ marginTop: 10 }}>
              <b>为什么是这两条规则</b>：{rc.why}
              <br />
              <b>为什么不会动到结论</b>：{rc.invariant}
              {eff.cagr != null && (
                <>
                  <br />
                  <b>运行期口径</b>（2019 年 ETF 数据可用起，剔除前期空仓期）：
                  年化 <b>{pct(eff.cagr)}</b>，最大回撤 <b>{pct(eff.maxdd, 2, false)}</b>，
                  夏普 <b>{num(eff.sharpe)}</b>。
                </>
              )}
            </div>
          </div>
        )}

        {basisOpen && (
          <div className="note" style={{ marginBottom: 12 }}>
            <b>结算口径</b>：{st.basis}
            <br />
            <b>与信号口径的区别</b>：方向判断本身用行业指数衡量（不可交易），收益一律用真实 ETF 衡量。
            <b>资金曲线是有限资金池的可执行账户</b>：初始本金一笔钱，卖出所得是唯一后续资金来源；
            建仓金额 = min(账户总值 × 入场闸门 ÷ 目标持仓笔数，可用现金)，预算再按 1/σ（σ 取
            {rc.config?.rp_window ?? 60} 日已实现波动）× 综合分倾斜。
            <b>持仓权重建仓后随价格漂移、不再被拉回目标——没有每日再平衡</b>；卖出只在持有到期时发生，
            当日所得次日才可用。{(rc.config?.vol_target ?? null) === null
              && !(rc.config?.exposure_cap?.enabled)
              ? <>入场闸门（波动目标 / 暴露上限）<b>已全部停用</b>（可执行口径下它们只压新仓预算、
                  实测为负贡献），建仓预算不再随波动缩放；<b>风险旋钮改用目标持仓笔数 K</b>。</>
              : <>入场闸门（波动目标 {pct(rc.config?.vol_target, 2, false)}
                  、暴露下限 {pct(rc.config?.vol_floor, 2, false)}，叠加 F5 暴露上限）
                  只决定<b>新仓预算的大小</b>，无法降低存量暴露。</>}
            未投入现金计现金收益。
            净值可由事件日志独立重放（账本对账偏差 &lt; 5e-4）。
          </div>
        )}

        <div className="note" style={{ marginBottom: 12 }}>
          <b>怎么读这组数字</b>：胜率 {pct(st.win_rate_excess, 2, false)}（跑赢基准）低于一半，
          但年化 {pct(sts.cagr)} 高于基准的 {pct(sts.bench_cagr)} ——
          这说明收益不是靠「猜对次数多」，而是靠少数判断错得少、对得多的不对称分布
          （单笔平均 {pct(st.avg_ret)}、平均超额 {pct(st.avg_excess)}）。
          换句话说：<b>它更像是「错的时候及时少亏、对的时候吃到大段」，而不是「十次里赢六次」。</b>
          这恰好也是它最大的脆弱点——收益依赖少数大赢家能否出现。
        </div>

        {sts.executed_trades != null && st.n_settled != null && (
          <div className="note" style={{ marginBottom: 12 }}>
            <b>结论数 ≠ 成交数（口径必须分开看）</b>：
            全区间 {st.n_settled} 条结论能对应到可交易 ETF，
            方向胜率、单笔平均收益统计的是这全部结论；
            但资金曲线受「最多同时持有 {sts.max_holdings} 笔」的容量约束，
            实际只成交 <b>{sts.executed_trades}</b> 笔，
            另有 {sts.blocked_by_capacity} 笔因没有槽位未建仓。
            也就是说：曲线只反映真正能执行的那部分，下面每个 B/S 点都是这 {sts.executed_trades} 笔之一。
            {(sts.open_at_end ?? 0) > 0 && (
              <>数据末端还有 <b>{sts.open_at_end}</b> 笔在途持仓（已买入、卖出日未到，
              图上只有 B 没有 S，属正常在场，不算缺失）。</>
            )}
          </div>
        )}

        {allDates.length > 1 && (
          <>
            <div className="yearbar">
              {yrList.map((y) => {
                const n = events.filter((e: any) => allDates[e.i]?.startsWith(y)).length
                return (
                  <button key={y} className={year === y ? 'on' : ''} onClick={() => setYear(y)}>
                    {y}<span style={{ opacity: 0.62 }}> · {n}</span>
                  </button>
                )
              })}
              <button className={year === 'all' ? 'on' : ''} onClick={() => setYear('all')}>
                全部 · {events.length}
              </button>
            </div>
            <NavChart
              key={year}
              dates={viewDates}
              nav={viewNav}
              bench={viewBench}
              benchName={st.bench_etf || '沪深300ETF'}
              markers={markers}

              holdings={posHoldings.length >= allDates.length ? posHoldings.slice(lo, hi + 1) : []}
              maxHoldings={posMax}
              conclusions={viewConcl}
              markerTip={(i: number) => mkByI.get(i) || []}
              tradeTip={tradeHtml}
            />
            <div className="note" style={{ marginTop: 8 }}>
              <b>怎么看这张图</b>：
              <span className="mklegend"><i className="dot-b" />B = 买入（红圈，画在净值线下方）</span>
              <span className="mklegend"><i className="dot-s" />S = 卖出（蓝圈，画在净值线上方）</span>
              <span className="mklegend"><i className="dot-c buy" />结论含入手</span>
              <span className="mklegend"><i className="dot-c amb" />结论仅埋伏</span>
              <span className="mklegend"><i className="dot-c none" />空仓等待</span>
              —— 圆点在结算层逐笔记录，不是事后标注；买入不区分入手与埋伏，是入手还是埋伏在悬浮明细里逐笔标注。
              <br />
              <b>底部是每日结论带（不是成交）</b>：每个交易日一格，颜色就是当天系统给出的结论档位。
              鼠标停在任意一天上，会一次给出当天净值与实际投出比例、当天的结论（环境 / 热度 / 标的与建议周期 /
              能否直接买到）、以及当天真实成交了什么（成交价、净收益、持有天数、卖出原因）。
              结论数远多于成交数 —— 容量上限会挡下大部分结论，<b>色带与 B/S 圆点的差就是这道约束</b>。
              <br />
              <b>右侧辅助轴 = 持仓仓位</b>：= 持仓笔数 ÷ {posMax} 个容量槽位，
              图上的浅黄色块就是它的逐日走势（画在曲线下方作背景层），块顶高度就是当天在场几笔仓位——
              <b>「当前是否持仓、持仓多少」一眼可见</b>（0% = 空仓，100% = 槽位占满）。
              同一只 ETF 被不同日期的结论反复加仓会各占一笔，所以块高只表示笔数、不等于持股分散度；
              实际投入的资金比例由波动目标决定，不在这一轴上表达。
              当前区间平均持仓 <b>{num(
                (year === 'all' ? posHoldings : posHoldings.slice(lo, hi + 1))
                  .reduce((a, b) => a + (b || 0), 0) /
                Math.max(1, (year === 'all' ? posHoldings : posHoldings.slice(lo, hi + 1)).filter((v) => v != null).length),
                1)}/{posMax} 笔</b>。
              <br />
              <b>可缩放</b>：在图上滚轮 / 双指直接缩放，或拖动底部滑条；也可按住拖动平移。
              「全部」看长期结构，放大到单年、单月后 B/S 会显成带字母的徽标，便于逐个核对。
              按钮上的数字是当年实际成交笔数；切换年份会把曲线归一化到该年起点，便于看单年波动。
              当前区间 <b>{markers.length}</b> 个成交日、
              <b>{year === 'all' ? events.length : events.filter((e: any) => e.i >= lo && e.i <= hi).length}</b> 笔成交
              {hasConcl && <>，其中 <b>{nConclDays}</b> 个交易日给出了结论（含入场与埋伏，其余交易日为空仓等待）</>}。
            </div>
          </>
        )}
      </Card>

      {/* ==================== 逐笔成交明细 ==================== */}
      {events.length > 0 && (
        <Card
          title="逐笔成交明细（曲线上的每个点都能在这里对上）"
          extra={
            <span className="muted">
              {year === 'all' ? '全区间' : year} · 共 {tradeRows.length} 笔
              {shownTrades.length < tradeRows.length ? `，显示前 ${shownTrades.length} 笔` : ''}
              {segAnn != null && (
                <> · 年化{' '}
                  <b style={{ color: segAnn >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(segAnn)}</b>
                </>
              )}
            </span>
          }
        >
          <div className="tradescroll">
            <table>
              <thead>
                <tr>
                  <th>买入日</th><th>行业</th><th>可交易标的</th>
                  <th style={{ textAlign: 'right' }}>买入价</th>
                  <th>卖出日</th>
                  <th style={{ textAlign: 'right' }}>卖出价</th>
                  <th style={{ textAlign: 'right' }}>持有</th>
                  <th>卖出原因</th>
                  <th style={{ textAlign: 'right' }}>净收益</th>
                  <th style={{ textAlign: 'right' }}>超额</th>
                </tr>
              </thead>
              <tbody>
                {shownTrades.map((t, i) => (
                  <tr key={i}>
                    <td className="num">{t.buyDate || '—'}</td>
                    <td><b>{t.sw}</b>{t.act === 'AMBUSH' && <span className="muted"> 埋伏</span>}</td>
                    <td>
                      <button
                        className="linkbtn"
                        title="查看该 ETF 走势与这笔交易的买卖点标注"
                        onClick={() => openEtf(t.sid, t.buyDate)}
                      >
                        {t.etf}
                      </button>{' '}
                      <span className="muted num">{t.code}</span>
                    </td>
                    <td className="num" style={{ textAlign: 'right' }}>{num(t.buyPx, 3)}</td>
                    <td className="num">{t.sellDate}</td>
                    <td className="num" style={{ textAlign: 'right' }}>{num(t.sellPx, 3)}</td>
                    <td className="num" style={{ textAlign: 'right' }}>{t.held ?? '—'} 日</td>
                    <td className="muted" style={{ fontSize: 11.5 }}>
                      {t.why || (t.h ? `持有到期（建议周期 ${t.h} 个交易日）` : '—')}
                    </td>
                    <td className="num" style={{ textAlign: 'right', color: (t.ret ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(t.ret)}</td>
                    <td className="num" style={{ textAlign: 'right', color: (t.ex ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(t.ex)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="note" style={{ marginTop: 8 }}>
            买入价 = 信号次日开盘价；卖出价 = 持有到期日收盘价；净收益已扣双边成本。
            超额为同期相对基准 ETF 的超额。这张表与上方曲线的成交点同源，逐笔可核对。
            <b>点击「可交易标的」可弹出该 ETF 走势图</b>：买卖点直接标在图上（含原因、权重、
            名义账本与收益对账），发生过份额拆分的标的还会标出折算日。
          </div>
        </Card>
      )}

      {/* ==================== 份额折算核验样本 ==================== */}
      {breaks && breaks.length > 0 && (
        <Card
          title="份额折算核验样本（拆分 / 合并）"
          extra={<span className="muted">共 {breaks.length} 处 · 点击任意一行弹出走势图逐个核验</span>}
        >
          <div className="note" style={{ marginBottom: 8 }}>
            这些 ETF 在样本期内发生过「份额折算」：单位价格按整数倍跳变（如 1 拆 2 当日原始价 −50%），
            但持有人份额同步翻倍、总价值不变——<b>不应记为盈亏</b>。
            结算层用「参照收益」（同日行业指数或全池中位数）识别这类价值中性跳变并复权还原
            （engine/qfq.py，检出阈值 25%）。下面每一处都可以点开走势图，
            对着「原始市价 vs 复权连续价」的断口，核对该日附近成交的钱是否算对。
          </div>
          <div className="tradescroll">
            <table>
              <thead>
                <tr>
                  <th>折算日</th><th>可交易标的</th>
                  <th style={{ textAlign: 'right' }}>原始价格跳变</th>
                  <th style={{ textAlign: 'right' }}>参照真实收益</th>
                  <th>参照来源</th>
                  <th style={{ textAlign: 'right' }}>复权因子</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {breaks.map((b, i) => (
                  <tr key={i} style={{ cursor: 'pointer' }}
                      onClick={() => openEtf(b.sid, b.date)}>
                    <td className="num">{b.date}</td>
                    <td><b>{b.name}</b> <span className="muted num">{b.code}</span></td>
                    <td className="num" style={{ textAlign: 'right', color: (b.raw_ret ?? 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
                      {pct(b.raw_ret, 2)}
                    </td>
                    <td className="num" style={{ textAlign: 'right' }}>{pct(b.ref_ret, 2)}</td>
                    <td className="muted" style={{ fontSize: 11.5 }}>
                      {b.ref_src === 'index' ? '行业指数' : '全池中位数'}
                    </td>
                    <td className="num" style={{ textAlign: 'right' }}>×{num(b.factor, 4)}</td>
                    <td><button className="linkbtn">查看走势</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* ==================== 执行口径对照 ==================== */}
      {(st.early_exit || st.gated) && (
        <Card title="执行口径对照（同一批结论、同一成本）"
          extra={<span className="muted">复盘驱动的改进尝试，如实呈现成败</span>}>
          <table>
            <thead>
              <tr>
                <th>执行方式</th>
                <th style={{ textAlign: 'right' }}>年化</th>
                <th style={{ textAlign: 'right' }}>累计</th>
                <th style={{ textAlign: 'right' }}>最大回撤</th>
                <th style={{ textAlign: 'right' }}>夏普</th>
                <th style={{ textAlign: 'right' }}>Calmar</th>
                <th style={{ textAlign: 'right' }}>交易数</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ background: 'var(--accent-soft)' }}>
                <td><b>A · 固定持有到建议周期</b><div className="small muted">当前主口径</div></td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.cagr)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.total_return)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.maxdd, 2, false)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{num(sts.sharpe, 2)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{num(sts.calmar, 2)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{sts.executed_trades ?? '—'}</td>
              </tr>
              {st.early_exit && (
                <tr>
                  <td>B · 执行失效条件<div className="small muted">连续 3 日跌破 MA20 即退出</div></td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.early_exit.stats?.cagr)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.early_exit.stats?.total_return)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.early_exit.stats?.maxdd, 2, false)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{num(st.early_exit.stats?.sharpe, 2)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{num(st.early_exit.stats?.calmar, 2)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{st.early_exit.n ?? '—'}</td>
                </tr>
              )}
              {st.gated && (
                <tr>
                  <td>C · 市场趋势门控<div className="small muted">基准 ETF 站上 MA200 才建仓</div></td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.gated.stats?.cagr)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.gated.stats?.total_return)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(st.gated.stats?.maxdd, 2, false)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{num(st.gated.stats?.sharpe, 2)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{num(st.gated.stats?.calmar, 2)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{st.gated.stats?.executed_trades ?? '—'}</td>
                </tr>
              )}
              <tr>
                <td>基准<div className="small muted">{st.bench_etf || '沪深300ETF'}</div></td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.bench_cagr)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.bench_total)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(sts.bench_maxdd, 2, false)}</td>
                <td className="num" style={{ textAlign: 'right' }}>—</td>
                <td className="num" style={{ textAlign: 'right' }}>—</td>
                <td className="num" style={{ textAlign: 'right' }}>—</td>
              </tr>
            </tbody>
          </table>
          <div className="note" style={{ marginTop: 8 }}>
            <b>这是一次失败的改进尝试，如实记录：</b>
            {' '}B 口径（连续 3 日跌破 MA20 就止损）触发率 {pct(st.early_exit?.exit_share, 2, false)}、
            平均只持有 {st.early_exit?.avg_held_days ?? '—'} 个交易日就把仓位洗光，
            年化反而降到 {pct(st.early_exit?.stats?.cagr)}、回撤扩大到 {pct(st.early_exit?.stats?.maxdd, 2, false)}；
            C 口径（等基准站上 MA200 再动手）同样跑输，年化 {pct(st.gated?.stats?.cagr)}、
            回撤 {pct(st.gated?.stats?.maxdd, 2, false)}。
            原因不神秘：行业 ETF 波动远大于宽基，均线类止损在 60-100 日的持有周期里几乎必然被噪音打穿；
            而 A 股的趋势拐点往往先于宽基均线信号，等门控打开时最肥的一段已经走完。
            <b>结论：不加这两条「看起来合理」的规则，反而更好。</b>
          </div>
        </Card>
      )}

      <Card title="跨牛熊分段验证"
        extra={<span className="muted">按基准 ETF 从历史高点回撤 20% 客观划分，非人工贴标签</span>}>
        {phases.length === 0 ? <div className="empty">暂无样本</div> : (
          <table>
            <thead>
              <tr>
                <th>市场阶段</th><th>区间</th>
                <th style={{ textAlign: 'right' }}>结论数</th>
                <th style={{ textAlign: 'right' }}>胜率</th>
                <th style={{ textAlign: 'right' }}>平均收益</th>
                <th style={{ textAlign: 'right' }}>平均超额</th>
                <th style={{ textAlign: 'right' }}>平均回撤</th>
              </tr>
            </thead>
            <tbody>
              {phases.map((p: any, i: number) => (
                <tr key={i}>
                  <td>
                    <Chip tone={p.phase === 'BEAR' ? 'down' : 'up'}>{PHASE_TXT[p.phase] || p.phase}</Chip>
                  </td>
                  <td className="num small">{p.start} → {p.end}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{p.n}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(p.win_rate, 2, false)}</td>
                  <td className="num" style={{ textAlign: 'right', color: (p.avg_ret || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(p.avg_ret)}</td>
                  <td className="num" style={{ textAlign: 'right', color: (p.avg_excess || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(p.avg_excess)}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(p.avg_maxdd, 2, false)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="note" style={{ marginTop: 8 }}>
          这一段是本次改造的重点：如果结论只在上行段有效、下行段失效，说明它本质是 beta 而非能力。
        </div>
      </Card>

      <Card title="分年度表现（账户口径 · 真实 ETF 结算）"
        extra={<span className="muted">账户 = 有限资金池可执行账户 · 基准 = 沪深300ETF</span>}>
        <table>
          <thead>
            <tr>
              <th>年份</th>
              <th style={{ textAlign: 'right' }}>账户收益</th>
              <th style={{ textAlign: 'right' }}>年化</th>
              <th style={{ textAlign: 'right' }}>基准</th>
              <th style={{ textAlign: 'right' }}>超额</th>
              <th style={{ textAlign: 'right' }}>平均投出</th>
              <th style={{ textAlign: 'right' }}>平均持仓</th>
              <th style={{ textAlign: 'right' }}>结论 / 胜率</th>
              <th style={{ textAlign: 'right' }}>结论均收益</th>
              <th style={{ textAlign: 'right' }}>成交</th>
            </tr>
          </thead>
          <tbody>
            {yearly.map((r: any) => (
              <tr key={r.y}>
                <td>{r.y}{r.partial ? <span className="muted small">（未完）</span> : ''}</td>
                <td className="num" style={{ textAlign: 'right', fontWeight: 700, color: r.ret >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(r.ret)}</td>
                <td className="num" style={{ textAlign: 'right', color: r.ann >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(r.ann)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(r.bench)}</td>
                <td className="num" style={{ textAlign: 'right', color: r.excess >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(r.excess)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(r.dep, 1, false)}</td>
                <td className="num" style={{ textAlign: 'right' }}>{num(r.hold, 1)} 笔</td>
                <td className="num" style={{ textAlign: 'right' }}>{r.n ?? 0} / {r.n ? pct(r.win, 1, false) : '—'}</td>
                <td className="num" style={{ textAlign: 'right', color: (r.avgRet || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>{r.n ? pct(r.avgRet) : '—'}</td>
                <td className="num" style={{ textAlign: 'right' }}>{r.buys}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="note" style={{ marginTop: 8 }}>
          <b>账户列</b>（收益 / 年化 / 基准 / 超额 / 平均投出 / 平均持仓 / 成交）来自同一条
          可执行账户资金曲线：年化按该年交易日数年化（未完年份显示"（未完）"）；
          「平均投出」= 持仓市值 ÷ 账户总值 的年内均值，它决定上涨市里天然跑输满仓基准的幅度。
          <b>结论列</b>（结论数 / 胜率=跑赢基准的比例 / 结论均收益）是信号口径，与账户列分母不同——
          两者的落差正是"钱有没有跟上信号"的度量。2018 年有结论但 0 成交，是因为当年该行业
          还没有可交易的 ETF（覆盖度见下表）。逐年归因（2024 信号饥荒、2025 现金拖累）见
          <code>docs/年度收益归因-2022-2026.md</code>。
        </div>
      </Card>

      <Card title="可交易标的覆盖度" extra={<span className="muted">每年末可结算行业数</span>}>
          <Bars
            rows={coverage.map((c: any) => ({
              label: c.year, value: c.tradeable, text: `${c.tradeable} 个`,
              color: c.tradeable >= 20 ? '#1b8a4b' : c.tradeable >= 10 ? '#d97706' : '#c62828',
            }))}
          />
          <div className="note" style={{ marginTop: 8 }}>
            行业 ETF 是真实约束：2018 年之前市场上只有个位数行业有可交易的 ETF，
            早期样本天然稀少且参考价值低，因此复盘窗口与覆盖度统计均从 2018 年起。
            越靠近当下，覆盖越完整，结论参考价值越高。
        </div>
      </Card>

      <div className="grid g2">
        <Card title="做错了什么（归因分布）" extra={<span className="muted">共 {o.wrong || 0} 条错误结论</span>}>
          {wrong.length === 0 ? <div className="empty">暂无</div> : (
            <Bars
              rows={wrong.map((w: any) => ({
                label: (w.label || w.code).split('：')[0], value: w.count,
                text: `${w.count} 条`, color: '#c62828',
              }))}
            />
          )}
          <div style={{ marginTop: 10 }}>
            {wrong.slice(0, 4).map((w: any, i: number) => (
              <div key={i} className="small" style={{ marginBottom: 3, color: 'var(--ink-2)' }}>
                <b>{w.code}</b> · {w.label}（占错误 {pct(w.share, 2, false)}）
              </div>
            ))}
          </div>
        </Card>

        <Card title="做对了什么（成功因素）" extra={<span className="muted">共 {o.right || 0} 条正确结论</span>}>
          {succ.length === 0 ? <div className="empty">暂无</div> : (
            <Bars
              rows={succ.map((w: any) => ({
                label: (w.label || w.code).split('：')[0], value: w.count, text: `${w.count} 条`, color: '#1b8a4b',
              }))}
            />
          )}
          <div style={{ marginTop: 10 }}>
            {succ.slice(0, 4).map((w: any, i: number) => (
              <div key={i} className="small" style={{ marginBottom: 3, color: 'var(--ink-2)' }}>
                <b>{w.code}</b> · {w.label}
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card title="如何改进（基于历史结论的反事实统计）">
        {impr.length === 0 ? (
          <div className="empty">样本不足，暂不给出参数调整建议</div>
        ) : impr.map((it: any, i: number) => (
          <div className="impr" key={i}>
            <div className="t">{i + 1}. {it.title}</div>
            <div className="s">{it.suggestion}</div>
            <div className="e">预期影响：{it.expected_impact}</div>
            {it.evidence?.buckets && (
              <div className="buckets">
                {it.evidence.buckets.map((b: any, j: number) => (
                  <span className="bucket" key={j}>
                    {b.bucket} · n={b.n}
                    {b.win_rate !== undefined ? ` · 胜率 ${pct(b.win_rate, 2, false)} · 超额 ${pct(b.avg_excess)}` : ''}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
        <div className="note" style={{ marginTop: 8 }}>
          以上结论均由「若当时收紧某条件，历史样本表现如何变化」的切分统计得出，不是主观建议。
        </div>
      </Card>

      <div className="grid g2">
        <Card title="最该记住的失败案例">
          <CaseTable rows={cases.worst || []} />
        </Card>
        <Card title="最成功的判断">
          <CaseTable rows={cases.best || []} />
        </Card>
      </div>

      <Card title="逐日复盘（最近交易日的结论与事后验证）">
        {recent.length === 0 ? <div className="empty">暂无</div> : recent.slice().reverse().map((d: any, i: number) => (
          <div key={i} style={{ padding: '10px 0', borderBottom: '1px dashed var(--line)' }}>
            <div className="between">
              <div className="row" style={{ gap: 8 }}>
                <b className="num">{d.date}</b>
                <Chip>{REGIME_TXT[d.regime] || d.regime}</Chip>
                {verdictChip(d.verdict)}
                {d.avg_excess !== null && d.avg_excess !== undefined && (
                  <span className="small num" style={{ color: d.avg_excess >= 0 ? 'var(--up)' : 'var(--down)' }}>
                    平均超额 {pct(d.avg_excess)}
                  </span>
                )}
              </div>
            </div>
            <div className="small muted" style={{ margin: '3px 0 5px' }}>{d.summary}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {(d.picks || []).map((p: any, j: number) => (
                <span className="chip" key={j} style={{ borderColor: (p.ex_ew || 0) >= 0 ? '#f3c9c9' : '#bfe3cd' }}>
                  {p.name} · {ACT_TXT[p.action] || p.action} · {p.horizon}日 ·{' '}
                  <b className="num" style={{ color: (p.ex_ew || 0) >= 0 ? 'var(--up)' : 'var(--down)' }}>
                    {pct(p.ex_ew)}
                  </b>
                </span>
              ))}
              {(d.pending || []).map((p: any, j: number) => (
                <span className="chip" key={`p${j}`}>{p.name} · {ACT_TXT[p.action] || p.action} · {p.horizon}日 · 持有中</span>
              ))}
            </div>
          </div>
        ))}
      </Card>

      <Card title="对照：方向判断的信号口径（行业指数，不可交易）"
        extra={<span className="muted">仅用于评估信号质量本身</span>}>
        <div className="grid g4" style={{ marginBottom: 10 }}>
          <Metric k="已验证结论" v={`${o.evaluated || 0}`} n={`另有 ${o.pending || 0} 条持有中未到期`} />
          <Metric k="方向判断胜率" v={pct(o.win_rate, 2, false)}
            n={`对 ${o.right || 0} / 错 ${o.wrong || 0} / 中性 ${o.neutral || 0}`}
            tone={(o.win_rate || 0) >= 0.5 ? 'up' : undefined} />
          <Metric k="平均超额收益" v={pct(o.avg_excess)}
            n="相对行业等权，按各自建议周期持有"
            tone={(o.avg_excess || 0) >= 0 ? 'up' : 'down'} />
          <Metric k="平均区间最大回撤" v={pct(o.avg_maxdd)} n="持有期内最坏时点" tone="down" />
        </div>
        <table>
          <thead><tr><th>维度</th><th style={{ textAlign: 'right' }}>n</th><th style={{ textAlign: 'right' }}>胜率</th><th style={{ textAlign: 'right' }}>平均超额</th></tr></thead>
          <tbody>
            {Object.entries(o.by_horizon || {}).map(([k, v]: any) => (
              <tr key={`h${k}`}><td>持有 {k} 日</td><td className="num" style={{ textAlign: 'right' }}>{v.n}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(v.win_rate, 2, false)}</td>
                <td className="num" style={{ textAlign: 'right', color: v.avg_excess >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(v.avg_excess)}</td></tr>
            ))}
            {Object.entries(o.by_regime || {}).map(([k, v]: any) => (
              <tr key={`r${k}`}><td>环境 {REGIME_TXT[k] || k}</td><td className="num" style={{ textAlign: 'right' }}>{v.n}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(v.win_rate, 2, false)}</td>
                <td className="num" style={{ textAlign: 'right', color: v.avg_excess >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(v.avg_excess)}</td></tr>
            ))}
            {Object.entries(o.by_action || {}).map(([k, v]: any) => (
              <tr key={`a${k}`}><td>{ACT_TXT[k] || k}</td><td className="num" style={{ textAlign: 'right' }}>{v.n}</td>
                <td className="num" style={{ textAlign: 'right' }}>{pct(v.win_rate, 2, false)}</td>
                <td className="num" style={{ textAlign: 'right', color: v.avg_excess >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(v.avg_excess)}</td></tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  )
}
