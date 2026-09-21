import React, { useEffect, useState } from 'react'
import Today from './Today'
import Review from './Review'
import Engine from './Engine'
import HistoryStrip from './HistoryStrip'

const TABS = [
  { k: 'today', label: '今日结论' },
  { k: 'history', label: '历史日历' },
  { k: 'review', label: '复盘' },
  { k: 'engine', label: '引擎' },
]

export default function App() {
  const [tab, setTab] = useState('today')
  const [today, setToday] = useState<any>(null)
  const [review, setReview] = useState<any>(null)
  const [hist, setHist] = useState<any>(null)
  const [err, setErr] = useState<string>('')

  useEffect(() => {
    Promise.all([
      fetch('data/today.json').then((r) => r.json()).catch((e) => { throw e }),
      fetch('data/review.json').then((r) => r.json()).catch((e) => { throw e }),
    ])
      .then(([t, r]) => { setToday(t); setReview(r) })
      .catch((e) => setErr(String(e)))
    // 历史结论库（复盘页的「每日结论带」用）：缺了不影响主页面，故单独失败不阻断
    fetch('data/history.json').then((r) => r.json()).then(setHist).catch(() => setHist(null))
  }, [])

  return (
    <>
      <header>
        <div className="header-inner">
          <div className="brand">
            <span className="name">混沌方舟 ARK</span>
            <span className="sub">行业方向趋势判断 · 非交易系统</span>
          </div>
          <div className="header-meta">
            <div>{today?.date || '—'}</div>
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
        {!err && today && tab === 'today' && <Today data={today} />}
        {!err && hist && tab === 'history' && <HistoryStrip days={hist.days || []} />}
        {!err && review && tab === 'review' && <Review data={review} hist={hist} />}
        {!err && review && tab === 'engine' && <Engine data={review} today={today} />}
      </div>
    </>
  )
}
