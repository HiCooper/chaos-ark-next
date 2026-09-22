import React, { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { CustomChart, LineChart as ELineChart, ScatterChart } from 'echarts/charts'
import {
  AxisPointerComponent, DataZoomComponent, GraphicComponent,
  GridComponent, LegendComponent, TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Marker } from './ui'

echarts.use([
  ELineChart, ScatterChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, AxisPointerComponent, GraphicComponent,
  CanvasRenderer,
])

const REGIME_TXT: Record<string, string> = { ATTACK: '进攻', NEUTRAL: '中性', DEFEND: '防守', PANIC: '恐慌' }
const ACT_TXT: Record<string, string> = { BUY_NOW: '入手', AMBUSH: '埋伏' }

// 画布与各分区的像素布局：GRID_H 用来把「B 画在线下方 N 像素」换算成数值空间，
// 必须与 yAxis/grid 的实际像素高度一致，否则标记会跑偏。
const CHART_H = 400
const GRID_TOP = 30
const GRID_H = 208
const STRIP_TOP = 296
const STRIP_H = 14
// 字母徽标（B/S）可容纳的单方向标记数上限：超过此数，字母互相重叠，收成小圆点。
// B 与 S 分别画在净值线下方/上方，是两个独立系列，拥挤度按单方向最多的一侧算——
// 2025 年全年 B 49 个 / S 53 个，字母间距约 21px，完全放得下。
// 之前按「交易日数」判 dense 是错的——单年约 243 个交易日但只有几十个成交点，
// 字母放得下，却被误判成圆点。
const DENSE_MARKERS = 80

/** 单方向最大标记数：窗口内 B、S 各自的点数取较大者 */
function maxSideMarkers(markers: Marker[], i0: number, i1: number): number {
  let b = 0, s = 0
  for (const m of markers) {
    if (m.i < i0 || m.i > i1) continue
    if (m.k === 'B') b++; else s++
  }
  return Math.max(b, s)
}

// 「每日结论」色带：非成交事件，而是当天系统给出的结论档位
const STRIP_COLORS: Record<number, string> = {
  0: '#e3e7ee', // 当日无标的通过共振门槛（空仓等待本身就是结论）
  1: '#7a5af8', // 只有埋伏
  2: '#c62828', // 含入手
}
const C_NAV = '#c62828'
const C_BENCH = '#8b94a3'
const C_AUX = '#5b7fa6'
const C_SELL = '#1565c0'

export type ConclDay = {
  date: string
  regime: string
  env_score: number
  summary: string
  picks: any[]
} | null

export type NavChartProps = {
  dates: string[]
  nav: number[]
  bench: number[]
  benchName: string
  markers: Marker[]
  invested: number[]
  holdings: number[]
  maxHoldings: number
  /** 与 dates 等长：每天的历史结论（无结论日为 null） */
  conclusions: ConclDay[]
  /** 买卖点悬浮明细（按曲线索引）：同一天可能同时有 B 和 S 两个标记，按数组返回 */
  markerTip: (i: number) => Marker[]
  /** 结论悬浮明细：返回 HTML 片段（成交明细部分） */
  tradeTip: (i: number) => string
}

function stripState(c: ConclDay): number {
  if (!c || !(c.picks || []).length) return 0
  return c.picks.some((p: any) => p.action === 'BUY_NOW') ? 2 : 1
}

/** 可见窗口的纵轴范围：留 13% 余量，保证 B（线下方）与 S（线上方）不会顶出画布 */
function navFit(nav: number[], bench: number[], i0: number, i1: number): { lo: number; hi: number } {
  let lo = Infinity
  let hi = -Infinity
  for (let i = i0; i <= i1; i++) {
    const a = nav[i]
    const b = bench[i]
    if (a != null && !Number.isNaN(a)) { lo = Math.min(lo, a); hi = Math.max(hi, a) }
    if (b != null && !Number.isNaN(b)) { lo = Math.min(lo, b); hi = Math.max(hi, b) }
  }
  if (!Number.isFinite(lo)) { lo = 0.99; hi = 1.01 }
  if (hi - lo < 1e-6) { hi += 0.01; lo -= 0.01 }
  const pad = (hi - lo) * 0.13
  return { lo: lo - pad, hi: hi + pad }
}

/** B/S 标记的落点：B 在净值线下方、S 在上方；同日多个买入按 stack 往下错开（与旧 SVG 版同一口径） */
function markerPts(kind: 'B' | 'S', markers: Marker[], dates: string[], nav: number[],
                   fit: { lo: number; hi: number }, dense: boolean, i0: number, i1: number) {
  const span = fit.hi - fit.lo
  const pxVal = (px: number) => (px * span) / GRID_H
  const offPx = dense ? 5.5 : 11.5
  const stepPx = dense ? 8 : 20
  return markers
    // 只画可见窗口内的标记：窗口外的点纵坐标是按窗口量程算的，留着既无意义、
    // 又会让 series 数据量随历史长度线性膨胀（缩放时每次都要重传）。
    .filter((m) => m.k === kind && m.i >= i0 && m.i <= i1)
    .map((m) => {
      const v = nav[m.i]
      if (v === undefined) return null
      const stack = m.stack || 0
      const y = kind === 'B' ? v - pxVal(offPx + stack * stepPx) : v + pxVal(offPx)
      return {
        value: [dates[m.i], Math.max(fit.lo + pxVal(2), Math.min(fit.hi - pxVal(2), y))] as any,
        name: `${kind}:${m.i}`,
      }
    })
    .filter(Boolean)
}

function markSeries(id: 'mkB' | 'mkS', data: any[], dense: boolean) {
  const buy = id === 'mkB'
  return {
    // type 必须显式写出：refit() 会单独把这组 series 交给 setOption，
    // 缺 type 时 ECharts 会报 "Unknown series undefined" 并连带把坐标轴解析搞崩
    // （线上表现为 Cannot read properties of undefined (reading 'axisBuilder')）。
    type: 'scatter' as const,
    id,
    symbolSize: dense ? 5.4 : 16,
    data,
    itemStyle: { color: buy ? C_NAV : C_SELL, borderColor: '#fff', borderWidth: dense ? 0.6 : 1.4 },
    label: {
      show: !dense, formatter: buy ? 'B' : 'S', color: '#fff',
      fontSize: 10, fontWeight: 700, position: 'inside',
    },
  }
}

function conclHtml(c: ConclDay, maxPicks = 6): string {
  if (!c) {
    return `<div class="ek-sec">每日结论</div><div class="ek-mute">历史结论库中无此交易日（早于回放区间）。</div>`
  }
  const picks = c.picks || []
  const nBuy = picks.filter((p: any) => p.action === 'BUY_NOW').length
  const nAmb = picks.length - nBuy
  const head = `<div class="ek-kv"><span>市场环境</span><b>${REGIME_TXT[c.regime] || c.regime} · 热度 ${Number(c.env_score).toFixed(1)}</b></div>`
  if (!picks.length) {
    return `<div class="ek-sec">每日结论</div>${head}
      <div class="ek-sum">${c.summary || '无行业通过共振门槛 —— 空仓等待本身就是结论。'}</div>`
  }
  const rows = picks.slice(0, maxPicks).map((p: any) => {
    const buy = p.action === 'BUY_NOW'
    const etf = p.etf ? `${p.etf}${p.etf_code ? ' ' + p.etf_code : ''}` : '暂无可交易 ETF'
    return `<div class="ek-pick">
      <span class="ek-g"><em class="ek-tag ${buy ? 'in' : 'amb'}">${ACT_TXT[p.action] || p.action}</em><b>${p.name}</b></span>
      <span class="ek-dim">${p.horizon}日 · 综合分 ${Number(p.composite).toFixed(1)}</span>
      <span class="ek-dim ${p.etf ? '' : 'warn'}">${etf}</span>
    </div>`
  }).join('')
  const more = picks.length > maxPicks ? `<div class="ek-mute">另 ${picks.length - maxPicks} 条…</div>` : ''
  return `<div class="ek-sec">每日结论（${nBuy} 入手 / ${nAmb} 埋伏）</div>${head}${rows}${more}`
}

export function NavChart(props: NavChartProps) {
  const { dates, nav, bench, markers, invested, holdings, maxHoldings, conclusions, markerTip } = props
  const box = useRef<HTMLDivElement | null>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  const sigRef = useRef<string>('')
  const propsRef = useRef(props)
  propsRef.current = props
  // 当前视窗（缩放/平移后的可见区间）。显示出来既方便读图，也让「缩放到哪儿了」有据可查。
  const [win, setWin] = useState<{ a: string; b: string; n: number } | null>(null)

  const n = dates.length
  const hasAux = invested.length === n

  // 显式缩放控制：滚轮/双指之外再给一组按钮，避免「不知道能缩放」或触控板不好用时没法操作
  const zoomTo = (start: number, end: number) =>
    chart.current?.dispatchAction({ type: 'dataZoom', start, end })
  const zoomBy = (f: number) => {
    const cur = (chart.current?.getOption()?.dataZoom?.[0] || {}) as any
    const s = cur.start ?? 0
    const en = cur.end ?? 100
    const c = (s + en) / 2
    const half = Math.min(50, ((en - s) / 2) * f)
    zoomTo(Math.max(0, c - half), Math.min(100, c + half))
  }

  const option = useMemo(() => {
    if (n < 2) return null
    const fit = navFit(nav, bench, 0, n - 1)
    // 与 refit() 用同一条密度判定（按单方向标记数），避免两处口径不一致
    const dense = maxSideMarkers(markers, 0, n - 1) > DENSE_MARKERS

    const series: any[] = [
      {
        id: 'aux', name: '仓位（右轴）', type: 'line', xAxisIndex: 0, yAxisIndex: 1,
        data: hasAux ? invested : [], symbol: 'none', z: 1, silent: true,
        lineStyle: { width: 1, color: C_AUX, opacity: 0.5 },
        // origin 用数值 0（y 轴下沿）：ECharts 的 'start'/'end' 指轴像素方向的端点，
        // 在 y 轴上 'start' 落在顶部，会把色块画反。
        areaStyle: { color: 'rgba(91,127,166,0.20)', origin: 0 },
      },
      {
        id: 'nav', name: 'ARK 方向组合（风控口径）', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: nav, symbol: 'none', z: 3, lineStyle: { width: 1.8, color: C_NAV },
        itemStyle: { color: C_NAV },
      },
      {
        id: 'bench', name: props.benchName || '基准 ETF', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: bench, symbol: 'none', z: 2, lineStyle: { width: 1.3, color: C_BENCH },
        itemStyle: { color: C_BENCH },
      },
      {
        id: 'mkB', name: '买入 B', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 5,
        ...markSeries('mkB', markerPts('B', markers, dates, nav, fit, dense, 0, n - 1) as any[], dense),
        emphasis: { scale: 1.45 },
      },
      {
        id: 'mkS', name: '卖出 S', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, z: 5,
        ...markSeries('mkS', markerPts('S', markers, dates, nav, fit, dense, 0, n - 1) as any[], dense),
        emphasis: { scale: 1.45 },
      },
      {
        id: 'strip', name: '每日结论', type: 'custom', xAxisIndex: 1, yAxisIndex: 2,
        data: dates.map((_, i) => [i, stripState(conclusions[i] || null)]), z: 4, silent: true,
        renderItem: (params: any, api: any) => {
          const i = api.value(0)
          const st = api.value(1)
          const p = api.coord([i, 0.5])
          const size = api.size([1, 1])
          const w = Math.max(size[0] * 0.94, 0.6)
          const h = Math.max(Math.min(size[1] * 0.88, 12), 4)
          return {
            type: 'rect',
            shape: { x: p[0] - w / 2, y: p[1] - h / 2, width: w, height: h },
            style: { fill: STRIP_COLORS[st] ?? STRIP_COLORS[0] },
          }
        },
      },
    ]

    const idxOfDate: Record<string, number> = {}
    dates.forEach((d, i) => { idxOfDate[d] = i })

    return {
      animation: false,
      grid: [
        { left: 58, right: 56, top: GRID_TOP, height: GRID_H },
        { left: 58, right: 56, top: STRIP_TOP, height: STRIP_H },
      ],
      legend: {
        top: 0, left: 54, itemWidth: 14, itemHeight: 8, itemGap: 14,
        textStyle: { fontSize: 11, color: '#4b5563' },
        data: ['ARK 方向组合（风控口径）', props.benchName || '基准 ETF', '仓位（右轴）', '每日结论'],
      },
      graphic: [
        { type: 'text', left: 4, top: STRIP_TOP + 1, style: { text: '每日结论', fontSize: 10, fill: '#8b94a3' } },
      ],
      axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
      tooltip: {
        trigger: 'axis',
        confine: true,
        transitionDuration: 0,
        backgroundColor: '#101725',
        borderWidth: 0,
        padding: [8, 10],
        textStyle: { color: '#fff', fontSize: 12 },
        extraCssText: 'border-radius:8px;box-shadow:0 8px 22px rgba(16,23,37,.24);',
        formatter: (ps: any) => {
          const arr = Array.isArray(ps) ? ps : [ps]
          const date = String(arr[0]?.axisValue ?? '')
          const i = idxOfDate[date]
          if (i === undefined) return ''
          const c = conclusions[i] || null
          const inv = hasAux ? invested[i] : null
          const dayMks = markerTip(i)
          const kv = [
            `<div class="ek-kv"><span>组合（自区间起点）</span><b>${((nav[i] - 1) * 100).toFixed(2)}%</b></div>`,
            `<div class="ek-kv"><span>${props.benchName || '基准'}</span><b>${((bench[i] - 1) * 100).toFixed(2)}%</b></div>`,
            inv != null
              ? `<div class="ek-kv"><span>当日仓位</span><b>${(inv * 100).toFixed(0)}%${
                holdings[i] != null ? ` · 持仓 ${holdings[i]}/${maxHoldings} 只` : ''}</b></div>`
              : '',
          ].join('')
          return `<div class="ektip">
            <div class="ek-h">${date} · ${c ? (REGIME_TXT[c.regime] || c.regime) : '—'}${dayMks.length ? ' · 成交日' : ''}</div>
            ${kv}
            ${conclHtml(c)}
            ${props.tradeTip(i)}
          </div>`
        },
      },
      xAxis: [
        {
          type: 'category', gridIndex: 0, data: dates, boundaryGap: false,
          axisLine: { lineStyle: { color: '#e4e7ec' } },
          axisTick: { show: false },
          axisLabel: { fontSize: 10, color: '#8b94a3', hideOverlap: true },
          splitLine: { show: false },
        },
        {
          type: 'category', gridIndex: 1, data: dates, boundaryGap: false,
          axisLine: { lineStyle: { color: '#e4e7ec' } },
          axisTick: { show: false },
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          type: 'value', gridIndex: 0, min: fit.lo, max: fit.hi, splitNumber: 5,
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: {
            fontSize: 10, color: '#8b94a3',
            formatter: (v: number) => `${((v - 1) * 100).toFixed(0)}%`,
          },
          splitLine: { lineStyle: { color: '#eef0f3' } },
        },
        {
          type: 'value', gridIndex: 0, min: 0, max: 1, position: 'right',
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: {
            fontSize: 9.5, color: '#a3adb9',
            formatter: (v: number) => `${Math.round(v * 100)}%`,
          },
          splitLine: { show: false },
        },
        { type: 'value', gridIndex: 1, min: 0, max: 1, show: false },
      ],
      dataZoom: [
        {
          type: 'inside', xAxisIndex: [0, 1], zoomOnMouseWheel: true,
          moveOnMouseMove: true, moveOnMouseWheel: false,
        },
        {
          type: 'slider', xAxisIndex: [0, 1], left: 58, right: 56, bottom: 8, height: 16,
          borderColor: '#e4e7ec', fillerColor: 'rgba(31,58,138,0.12)',
          handleStyle: { color: '#1f3a8a', borderColor: '#1f3a8a' },
          moveHandleSize: 10,
          textStyle: { color: '#8b94a3', fontSize: 10 },
          labelFormatter: (v: number) => String(dates[Math.round(v)] ?? ''),
        },
      ],
      series,
    }
  }, [dates, nav, bench, markers, invested, holdings, conclusions, hasAux, maxHoldings, n,
      props.benchName, props.markerTip, props.tradeTip])

  // refit 挂在 ref 上：它由「初始化」effect 创建，但必须等「选项就绪」effect 之后才允许被调用。
  const refitRef = useRef<() => void>(() => {})

  useEffect(() => {
    if (!box.current) return
    // 兜底：若上一次挂载因异常没能执行清理，同 dom 上会残留实例，
    // 此时再 init 会报 "There is a chart instance already initialized on the dom"。
    echarts.getInstanceByDom(box.current)?.dispose()
    const ch = echarts.init(box.current)
    chart.current = ch
    // 把实例挂到容器上：canvas 图表的状态（缩放区间、series/dataZoom）没法从 DOM 读到，
    // 校验线上产物时只能靠它——见 ark-frontend-visual-check 的「必须亲眼看见」原则。
    ;(box.current as any).__chart = ch

    /** 缩放/平移后重排：纵轴贴合可见窗口 + B/S 标记按新量程重算 + 密度切换 */
    const refit = () => {
      const o = (ch.getOption() || {}) as any
      // 选项还没落地时（init 之后、首次 setOption 之前）绝不动 yAxis/series：
      // 此时图上没有 xAxis / grid，凭空塞 yAxis + series 会让 ECharts 的坐标轴解析崩掉
      // （线上表现为 Cannot read properties of undefined (reading 'axisBuilder')）。
      if (!o.dataZoom || !o.dataZoom.length) return
      const dz = o.dataZoom[0] || {}
      const s = dz.start ?? 0
      const en = dz.end ?? 100
      const p = propsRef.current
      const cnt = p.dates.length
      const i0 = Math.max(0, Math.round((s / 100) * (cnt - 1)))
      const i1 = Math.min(cnt - 1, Math.max(i0, Math.round((en / 100) * (cnt - 1))))
      setWin((prev) => (prev && prev.a === p.dates[i0] && prev.b === p.dates[i1]
        ? prev : { a: p.dates[i0], b: p.dates[i1], n: i1 - i0 + 1 }))
      // 密度按「窗口内单方向最大标记数」判，不按交易日数：单年虽有两百多个交易日，
      // 但 B/S 各方向只有几十个点，字母徽标放得下。
      const dense = maxSideMarkers(p.markers, i0, i1) > DENSE_MARKERS
      const fit = navFit(p.nav, p.bench, i0, i1)
      // 只回写 min/max（按索引合并到 yAxis[0]），不要把 getOption() 的规范化对象整个铺回去，
      // 那个对象里含着 ECharts 内部字段，回写容易踩坑。
      ch.setOption({
        yAxis: [{ min: fit.lo, max: fit.hi }],
        series: [
          markSeries('mkB', markerPts('B', p.markers, p.dates, p.nav, fit, dense, i0, i1) as any[], dense),
          markSeries('mkS', markerPts('S', p.markers, p.dates, p.nav, fit, dense, i0, i1) as any[], dense),
        ],
      })
    }
    refitRef.current = refit
    const onResize = () => ch.resize()
    window.addEventListener('resize', onResize)
    ch.on('datazoom', refit)
    ch.on('dataZoom', refit)
    return () => {
      window.removeEventListener('resize', onResize)
      refitRef.current = () => {}
      // 关键：实例被销毁后必须把「已应用过的选项签名」清掉。
      // 否则 React 严格模式「挂载→清理→再挂载」时，第二个 effect 会因签名相同而跳过
      // setOption，新建的图表实例就永远空着（画布上没有 canvas，getOption() 为 undefined）。
      sigRef.current = ''
      ch.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    if (!option || !chart.current) return
    const sig = `${n}|${dates[0]}|${dates[n - 1]}|${markers.length}|${hasAux}`
    if (sigRef.current === sig) return
    sigRef.current = sig
    chart.current.setOption(option, true)
    refitRef.current() // 选项落地后初始化视窗文案与纵轴
  }, [option, n, dates, markers.length, hasAux])

  if (n < 2) return <div className="empty">数据不足</div>
  return (
    <div>
      <div className="chartbar">
        <span className="muted">视窗</span>
        <b className="num">{win ? win.a : dates[0]}</b>
        <span className="muted">→</span>
        <b className="num">{win ? win.b : dates[n - 1]}</b>
        <span className="muted">· {win ? win.n : n} 个交易日</span>
        <button className="zbtn" onClick={() => zoomBy(0.6)} title="放大">＋</button>
        <button className="zbtn" onClick={() => zoomBy(1 / 0.6)} title="缩小">－</button>
        <button className="linkbtn" onClick={() => zoomTo(0, 100)}>重置缩放</button>
        <span className="muted">滚轮 / 双指缩放 · 拖动底部滑条或按住图表平移</span>
      </div>
      <div ref={box} style={{ width: '100%', height: CHART_H }} />
    </div>
  )
}
