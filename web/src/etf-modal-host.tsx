import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { EtfModal, SidEvent } from './etf-modal'

/**
 * 全局唯一的「标的走势核验弹窗」宿主。
 *
 * 复盘页（逐笔明细 / 折算样本）与今日结论页（可直接交易标的 / 在途持仓 / 行业全景表）
 * 都可能出现 ETF，点击即弹出同一套走势核验弹窗。任何页面：
 *   const { open, sidOf } = useEtfModal()
 *   open(sidOf(etfCode) ?? '', focusDate?)   // sidOf：6 位代码 → sid（如 159516 → 0.159516）
 *
 * sid 解析优先用 data/etf/_index.json（build_etf_webdata.py 生成，覆盖全池 ETF，
 * 含从未成交的）；接口缺失时退回结算事件里出现过的 sid。
 */

type EtfModalCtx = {
  open: (sid: string, focusDate?: string) => void
  /** 6 位 ETF 代码 → sid（"0.159516"）；解析不到返回 null */
  sidOf: (code?: string | null) => string | null
}

const Ctx = createContext<EtfModalCtx>({ open: () => {}, sidOf: () => null })

export function useEtfModal() {
  return useContext(Ctx)
}

export function EtfModalHost({ settlement, children }: { settlement: any; children: React.ReactNode }) {
  const [target, setTarget] = useState<{ sid: string; focusDate?: string } | null>(null)
  // data/etf/_index.json：code → {sid, name, sw}，覆盖面板全部 ETF
  const [index, setIndex] = useState<Record<string, { sid: string; name: string }> | null>(null)

  useEffect(() => {
    fetch('data/etf/_index.json')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => setIndex(j.etfs || {}))
      .catch(() => setIndex(null))
  }, [])

  const allDates: string[] = settlement?.curve?.dates || []
  const allNav: number[] = settlement?.curve?.nav || []
  const events: any[] = settlement?.events || []

  // 结算事件里出现过的代码 → sid（_index.json 加载失败时的兜底）
  const fallbackByCode = useMemo(() => {
    const m: Record<string, string> = {}
    for (const e of events) {
      if (e.sid) m[String(e.sid).split('.')[1]] = e.sid
    }
    return m
  }, [events])

  const ctx = useMemo<EtfModalCtx>(() => ({
    open: (sid: string, focusDate?: string) => {
      if (sid) setTarget({ sid, focusDate })
    },
    sidOf: (code?: string | null) => {
      const c = code ? String(code).split('.').pop()! : ''
      if (!c) return null
      return index?.[c]?.sid ?? fallbackByCode[c] ?? null
    },
  }), [index, fallbackByCode])

  // 选定标的的全部 B/S 事件（B 事件本身不带日期，按曲线索引补上）
  const sidEvents = useMemo(() => {
    if (!target) return [] as SidEvent[]
    const out: SidEvent[] = []
    for (const e of events) {
      if (e.sid !== target.sid) continue
      out.push({ ...e, date: allDates[e.i] })
    }
    return out
  }, [target, events, allDates])

  return (
    <Ctx.Provider value={ctx}>
      {children}
      {target && (
        <EtfModal
          sid={target.sid}
          focusDate={target.focusDate}
          curveDates={allDates}
          curveNav={allNav}
          events={sidEvents}
          onClose={() => setTarget(null)}
        />
      )}
    </Ctx.Provider>
  )
}
