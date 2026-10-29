import React, { useEffect, useState } from 'react'
import Today from './Today'
import Review from './Review'
import Engine from './Engine'
import Calendar from './Calendar'
import Lab from './Lab'
import MyBook from './MyBook'
import { EtfModalHost } from './etf-modal-host'

const TABS = [
  { k: 'today', label: '今日研判' },
  { k: 'book', label: '我的持仓' },
  { k: 'review', label: '复盘' },
  { k: 'engine', label: '引擎' },
  { k: 'lab', label: '实验' },
]

// 从 URL hash 解析 tab（如 #/review）：刷新 / 分享链接后仍定位到同一 tab
function tabFromHash(): string {
  const h = window.location.hash.replace(/^#\/?/, '')
  return TABS.some((t) => t.k === h) ? h : 'today'
}

export default function App() {
  const [tab, setTab] = useState(tabFromHash)
  const [today, setToday] = useState<any>(null)
  const [review, setReview] = useState<any>(null)
  const [hist, setHist] = useState<any>(null)
  const [lab, setLab] = useState<any>(null)
  const [err, setErr] = useState<string>('')

  // 研判页状态：日历选中的交易日（默认最新），历史日详情按需懒加载并缓存
  const [selected, setSelected] = useState<string>('')
  const [dayCache, setDayCache] = useState<Record<string, any>>({})

  // tab ↔ URL 双向同步：点击切换写入 hash（可前进/后退），手动改 hash 或点浏览器回退也能跟随
  const switchTab = (k: string) => {
    setTab(k)
    if (tabFromHash() !== k) window.location.hash = `/${k}`
  }
  useEffect(() => {
    const onHash = () => setTab(tabFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const latestDate = today?.date || (hist?.days?.length ? hist.days[hist.days.length - 1].date : '')

  useEffect(() => {
    // 数据每天会被引擎重写，但文件名不变（data/today.json）——浏览器会拿缓存的旧副本，
    // 表现为「数据已更新到 10-28，页面日历里还停在 10-27」。统一 no-store，每次打开都取最新。
    const NO_STORE: RequestInit = { cache: 'no-store' }
    Promise.all([
      fetch('data/today.json', NO_STORE).then((r) => r.json()).catch((e) => { throw e }),
      fetch('data/review.json', NO_STORE).then((r) => r.json()).catch((e) => { throw e }),
    ])
      .then(([t, r]) => { setToday(t); setReview(r) })
      .catch((e) => setErr(String(e)))
    // 历史结论索引（日历 + 复盘页「每日结论带」用）：缺了不影响主页面，故单独失败不阻断
    fetch('data/history.json', NO_STORE).then((r) => r.json()).then(setHist).catch(() => setHist(null))
    // 实验登记簿：独立失败不阻断主页面
    fetch('data/experiments.json', NO_STORE).then((r) => r.json()).then(setLab).catch(() => setLab(null))
  }, [])

  // 今日/历史日数据就位后，把日历选中态对齐到最新交易日
  useEffect(() => {
    if (!selected && latestDate) setSelected(latestDate)
  }, [latestDate, selected])

  // 点击历史日时懒加载该日完整研判（day/<date>.json，与 today.json 同构）
  useEffect(() => {
    if (!selected || selected === latestDate || dayCache[selected]) return
    let alive = true
    fetch(`data/day/${selected}.json`, { cache: 'no-store' })
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
    <EtfModalHost settlement={review?.settlement}>
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
            <button key={t.k} className={tab === t.k ? 'on' : ''} onClick={() => switchTab(t.k)}>
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

        {!err && today && tab === 'book' && (
          currentDay
            ? <MyBook data={currentDay} days={hist?.days || []} selected={selected} onSelect={setSelected}
              generatedAt={hist?.generated_at} />
            : <div className="loading">装载 {selected} 研判数据</div>
        )}

        {!err && review && tab === 'review' && <Review data={review} hist={hist} />}
        {!err && review && tab === 'engine' && <Engine data={review} today={today} />}
        {tab === 'lab' && <Lab data={lab} />}
      </div>
    </EtfModalHost>
  )
}
