import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

/**
 * 交易日历热力图：按「月」分列的横向布局。
 * 每个月是一个竖列：顶部月份名，下面按周分行（周一..周五）的小方格，
 * 月份之间用横向 flex 排开——对齐完全交给 flex，不需要任何偏移计算。
 *
 * 每个格子 = 一个交易日，颜色 = 当日市场环境档位（regime），
 * 格内标记 = 当日有「入手」（圆点）/「埋伏」（左下角小方块）结论。
 *
 * 按年渲染：只挂载当前查看年份的 12 个月（约 250 格），年份下拉切换；
 * 切年时自动选中该年最后一个交易日。选中日期由父级持有（selected + onSelect）。
 */

const REGIME_TXT: Record<string, string> = {
  ATTACK: '进攻', NEUTRAL: '中性', DEFEND: '防守', PANIC: '恐慌',
}

function stateClass(r: string): string {
  switch (r) {
    case 'ATTACK': return 'is-attack'
    case 'PANIC': return 'is-panic'
    case 'DEFEND': return 'is-defend'
    default: return 'is-neutral'
  }
}

/** 周一为一周起点；返回该日期所在周的周一（ISO 日期串，作周键） */
function mondayKey(d: Date): string {
  const offset = (d.getDay() + 6) % 7 // Mon=0..Sun=6
  const m = new Date(d)
  m.setDate(d.getDate() - offset)
  return m.toISOString().slice(0, 10)
}

/** 行 0..4 = 周一..周五（A 股只在交易日出结论；周六并入周五行，周日并入周一行的空位） */
function weekRow(d: Date): number {
  return Math.min((d.getDay() + 6) % 7, 4)
}

function weekday(d: string): string {
  const dt = new Date(d + 'T00:00:00')
  return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][dt.getDay()]
}

/** 一个月 = { key: 'YYYY-MM', name: 'N月', weeks: (day|null)[][] }，weeks[r][c] = 第 r 周的周(1+c) */
type MonthCol = { key: string; name: string; weeks: (any | null)[][] }

/** 把某一年的交易日整理成 12 个月列（只含有数据的月份） */
function buildMonths(yearDays: any[]): MonthCol[] {
  const byMonth = new Map<string, any[]>()
  for (const d of yearDays) {
    const k = d.date.slice(0, 7)
    if (!byMonth.has(k)) byMonth.set(k, [])
    byMonth.get(k)!.push(d)
  }
  const months: MonthCol[] = []
  for (const [k, list] of byMonth) {
    // 月内按周分行；跨月的周只保留属于本月的日（另一部分在相邻月的列里）
    const weeks = new Map<string, (any | null)[]>()
    for (const d of list) {
      const dt = new Date(d.date + 'T00:00:00')
      const wk = mondayKey(dt)
      if (!weeks.has(wk)) weeks.set(wk, [null, null, null, null, null])
      weeks.get(wk)![weekRow(dt)] = d
    }
    const keys = [...weeks.keys()].sort()
    months.push({
      key: k,
      name: `${Number(k.slice(5, 7))}月`,
      weeks: keys.map((w) => weeks.get(w)!),
    })
  }
  return months
}

export default function Calendar({ days, selected, onSelect }: {
  days: any[]
  selected: string
  onSelect: (d: string) => void
}) {
  const selRef = useRef<HTMLButtonElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // 可选年份（数据里出现过的年份，升序）
  const years = useMemo(() => {
    const ys: string[] = []
    for (const d of days) {
      const y = d.date.slice(0, 4)
      if (!ys.length || ys[ys.length - 1] !== y) ys.push(y)
    }
    return ys
  }, [days])

  const latestYear = years[years.length - 1] || ''
  const [viewYear, setViewYear] = useState('')
  // 选中日所在年份变化时（点格子/上下日切换）跟随渲染对应年份
  const effYear = selected ? selected.slice(0, 4) : ''
  useEffect(() => {
    if (effYear && years.includes(effYear)) setViewYear(effYear)
  }, [effYear, years])

  const months = useMemo(
    () => buildMonths(viewYear ? days.filter((d) => d.date.startsWith(viewYear)) : days),
    [days, viewYear],
  )

  // 渲染年份切换 / 首次挂载：滚到最右（该年最新交易日）。
  // 用 useLayoutEffect：必须在浏览器绘制前定位，否则用户会先看到 1 月再跳到 12 月，
  // 表现为刷新时日历「滚动一下」。
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    // 必须显式 'instant'：'auto' 会遵循容器 CSS 的 scroll-behavior，
    // 那里若残留 smooth，首屏定位就会变成一段可见的平滑滚动
    el.scrollTo({ left: el.scrollWidth, behavior: 'instant' })
  }, [viewYear, months])

  // 选中格变化时把格子在水平方向滚入可视区（点格子/箭头切日）。
  // 两条纪律：
  // 1) selected 从空到有值（刷新后数据到达时的初始对齐）不滚——那一下平滑滚动
  //    就是「刷新时日历滚动」的元凶；首屏定位完全交给上面的 viewYear 布局效果。
  // 2) 只调容器的 scrollLeft，不用 scrollIntoView——后者会连带滚动整页竖向，
  //    选中格在首屏外时会把页面拽下去。且格子已完整可见时不滚，避免多余动画。
  const prevSel = useRef<string>('')
  useEffect(() => {
    const prev = prevSel.current
    prevSel.current = selected
    const el = selRef.current
    const sc = scrollRef.current
    if (!prev || !el || !sc) return
    const cr = el.getBoundingClientRect()
    const sr = sc.getBoundingClientRect()
    if (cr.left >= sr.left && cr.right <= sr.right) return
    const target = sc.scrollLeft + (cr.left + cr.right) / 2 - (sr.left + sr.right) / 2
    sc.scrollTo({ left: target, behavior: 'smooth' })
  }, [selected])

  // 下拉切年：渲染该年格子，并选中该年最后一个交易日
  const changeYear = (y: string) => {
    setViewYear(y)
    const inYear = days.filter((d) => d.date.startsWith(y))
    if (inYear.length && !selected.startsWith(y)) onSelect(inYear[inYear.length - 1].date)
  }

  const counts = useMemo(() => {
    let buy = 0, amb = 0, daysN = 0
    for (const d of days) {
      const picks = d.picks || []
      if (picks.some((p: any) => p.action === 'BUY_NOW')) buy++
      if (picks.some((p: any) => p.action === 'AMBUSH')) amb++
      if (picks.length > 0) daysN++
    }
    return { buy, amb, daysN }
  }, [days])

  // 选中日信息（供 strip-head 显示）
  const selIdx = days.findIndex((d) => d.date === selected)
  const selDay = selIdx >= 0 ? days[selIdx] : null

  const stepDate = (delta: number) => {
    if (selIdx < 0) return
    const t = selIdx + delta
    if (t >= 0 && t < days.length) onSelect(days[t].date)
  }

  return (
    <>
      {/* 头部提到卡片外面：日期大字 + 第 N 个结论日 + 上一天/下一天 + 年份下拉 */}
      <div className="hist-head">
        <div className="date-line">
          <span className="date-big">{selected || '—'}</span>
          <span className="date-weekday">
            {selDay ? `${weekday(selected)} · 第 ${selIdx + 1}/${days.length} 个结论日` : ''}
          </span>
        </div>
        <div className="hist-head-right">
          <select
            className="year-select"
            value={viewYear || latestYear}
            onChange={(e) => changeYear(e.target.value)}
            aria-label="选择年份"
          >
            {[...years].reverse().map((y) => (
              <option key={y} value={y}>{y} 年</option>
            ))}
          </select>
          <div className="nav-arrows">
            <button className="arrow-btn" aria-label="上一日" disabled={selIdx <= 0} onClick={() => stepDate(-1)}>◀</button>
            <button className="arrow-btn" aria-label="下一日" disabled={selIdx >= days.length - 1} onClick={() => stepDate(1)}>▶</button>
          </div>
        </div>
      </div>

      <div className="hist-strip">
        <div className="hist-scroll" ref={scrollRef}>
        <div className="hist-flow">
          {months.map((m) => (
            <div className="hmonth" key={m.key}>
              <div className="hmonth-name">{m.name}</div>
              <div className="hmonth-grid">
                {m.weeks.map((row, r) => (
                  <div className="hweek" key={r}>
                    {row.map((d, c) => {
                      if (!d) return <i className="hcell empty" key={c} />
                      const picks = d.picks || []
                      const buy = picks.filter((p: any) => p.action === 'BUY_NOW').length
                      const amb = picks.filter((p: any) => p.action === 'AMBUSH').length
                      const sells = d.n_sell || 0
                      const cls = [
                        'hcell',
                        stateClass(d.regime),
                        buy > 0 ? 'has-buy' : '',
                        amb > 0 ? 'has-ambush' : '',
                        sells > 0 ? 'has-sell' : '',
                        d.date === selected ? 'selected' : '',
                      ].join(' ')
                      const label = `${d.date} · ${REGIME_TXT[d.regime] || d.regime}` +
                        (buy + amb > 0 ? ` · 入手${buy} 埋伏${amb}` : ' · 无方向结论') +
                        (sells > 0 ? ` · 出手${sells} 笔` : '')
                      return (
                        <button
                          key={c}
                          ref={d.date === selected ? selRef : undefined}
                          className={cls}
                          title={label}
                          aria-label={label}
                          aria-pressed={d.date === selected}
                          onClick={() => onSelect(d.date)}
                        >
                          {amb > 0 && <i className="hmark" />}
                        </button>
                      )
                    })}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="hist-legend">
        <span className="legend-item"><i className="swatch sw-attack" />进攻</span>
        <span className="legend-item"><i className="swatch sw-neutral" />中性</span>
        <span className="legend-item"><i className="swatch sw-defend" />防守</span>
        <span className="legend-item"><i className="swatch sw-panic" />恐慌</span>
        <span className="legend-item"><i className="swatch sw-buy" />当日有「入手」</span>
        <span className="legend-item"><i className="swatch sw-ambush" />当日有「埋伏」</span>
        <span className="legend-item"><i className="swatch sw-sell" />当日有「出手」</span>
        <span className="legend-item" style={{ marginLeft: 'auto' }}>
          共 {days.length} 个交易日 · 有结论 {counts.daysN} 日 · 入手 {counts.buy} / 埋伏 {counts.amb} · 点击方格切换日期
        </span>
      </div>
      </div>
    </>
  )
}
