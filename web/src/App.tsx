import React, { useEffect, useState } from 'react'
import Today from './Today'
import Review from './Review'
import Engine from './Engine'
import Calendar from './Calendar'
import Lab from './Lab'

const TABS = [
  { k: 'today', label: '今日结论' },
  { k: 'review', label: '复盘' },
  { k: 'engine', label: '引擎' },
  { k: 'lab', label: '实验' },
]

export default function App() {
  const [tab, setTab] = useState('today')
  const [today, setToday] = useState<any>(null)
  const [review, setReview] = useState<any>(null)
  const [hist, setHist] = useState<any>(null)
  const [lab, setLab] = useState<any>(null)
  const [err, setErr] = useState<string>('')

  // 研判页状态：日历选中的交易日（默认最新），历史日详情按需懒加载并缓存
  const [selected, setSelected] = useState<string>('')
  const [dayCache, setDayCache] = useState<Record<string, any>>({})

  const latestDate = today?.date || (hist?.days?.length ? hist.days[hist.days.length - 1].date : '')

  useEffect(() => {
    Promise.all([
      fetch('data/today.json').then((r) => r.json()).catch((e) => { throw e }),
      fetch('data/review.json').then((r) => r.json()).catch((e) => { throw e }),
    ])
      .then(([t, r]) => { setToday(t); setReview(r) })
      .catch((e) => setErr(String(e)))
    // 历史结论索引（日历 + 复盘页「每日结论带」用）：缺了不影响主页面，故单独失败不阻断
    fetch('data/history.json').then((r) => r.json()).then(setHist).catch(() => setHist(null))
    // 实验登记簿：独立失败不阻断主页面
    fetch('data/experiments.json').then((r) => r.json()).then(setLab).catch(() => setLab(null))
  }, [])

  // 今日/历史日数据就位后，把日历选中态对齐到最新交易日
  useEffect(() => {
    if (!selected && latestDate) setSelected(latestDate)
  }, [latestDate, selected])

  // 点击历史日时懒加载该日完整研判（day/<date>.json，与 today.json 同构）
  useEffect(() => {
    if (!selected || selected === latestDate || dayCache[selected]) return
    let alive = true
    fetch(`data/day/${selected}.json`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => { if (alive) setDayCache((c) => ({ ...c, [selected]: d })) })
      .catch(() => { if (alive) setDayCache((c) => ({ ...c, [selected]: { date: selected, error: '该日研判数据缺失' } })) })
    return () => { alive = false }
  }, [selected, latestDate, dayCache])

  const isLatest = selected === latestDate
  const currentDay = isLatest ? today : dayCache[selected]

  return (
    <>
      <header>
        <div className="header-inner">
          <div className="brand">
            <span className="name">混沌方舟 ARK</span>
            <span className="sub">行业方向趋势判断 · 非交易系统</span>
          </div>
          <div className="header-meta">
            <div>{latestDate || '—'}</div>
            <div>{today ? `引擎 ${today.engine_version}` : ''}</div>
          </div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t.k} className={tab === t.k ? 'on' : ''} onClick={() => setTab(t.k)}>
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <div className="wrap" style={{ paddingTop: 18 }}>
        {err && <div className="card"><div className="empty">数据加载失败：{err}</div></div>}
        {!err && !today && <div className="loading">加载中…</div>}

        {!err && today && tab === 'today' && (
          <>
            <Calendar days={hist?.days || []} selected={selected} onSelect={setSelected} />
            {!isLatest && (
              <div className="card" style={{ padding: '10px 16px', borderLeft: '3px solid var(--accent)', marginBottom: 14 }}>
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                  <span className="small" style={{ color: 'var(--ink-2)' }}>
                    正在查看历史交易日 <b className="num">{selected}</b> 的研判（非最新结论日）
                  </span>
                  <button className="linkbtn" onClick={() => setSelected(latestDate)}>
                    回到最新交易日
                  </button>
                </div>
              </div>
            )}
            {currentDay
              ? <Today data={currentDay} />
              : <div className="loading">装载 {selected} 研判数据</div>}
          </>
        )}

        {!err && review && tab === 'review' && <Review data={review} hist={hist} />}
        {!err && review && tab === 'engine' && <Engine data={review} today={today} />}
        {tab === 'lab' && <Lab data={lab} />}
      </div>
    </>
  )
}
