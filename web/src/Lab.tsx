import React, { useState } from 'react'

/**
 * 「实验」页：登记所有做过的实验与口径变更。
 *
 * 两类记录，版式统一、内容自适应：
 *   · 对照实验（有 hypothesis/params/results/goals/robustness）——展示完整实验卡；
 *   · 口径变更记录（只有 result/why/conclusion）——展示结论/背景/结果，不硬凑空表。
 *
 * 稳定性约定：任何一条记录缺字段都**只影响这一张卡**（CardBoundary 兜底），
 * 绝不允许因为某条脏数据把整页渲染成白屏。
 */

const STATUS_CLS: Record<string, string> = {
  '已采用': 'lab-st-adopted',
  '已落参': 'lab-st-adopted',
  '验证通过': 'lab-st-adopted',
  '已否决': 'lab-st-rejected',
  '部分否决': 'lab-st-partial',
  '待拍板': 'lab-st-pending',
  '已记录': 'lab-st-record',
}

const pct = (v: number | undefined, signed = true) => {
  if (v == null || Number.isNaN(v)) return '—'
  const s = (v * 100).toFixed(2) + '%'
  return signed && v > 0 ? '+' + s : s
}

const num = (v: any, nd = 2) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(nd))

function Metrics({ m }: { m: any }) {
  if (!m) return <span className="muted">—</span>
  return (
    <span className="num">
      {pct(m.cagr)} / {pct(m.maxdd)}
      {m.sharpe != null ? ` / Sharpe ${num(m.sharpe)}` : ''}
    </span>
  )
}

/** 单卡错误边界：一条脏数据不能让整页消失（这就是之前"点击展开白屏"的根因）。 */
class CardBoundary extends React.Component<{ children: React.ReactNode; title?: string }, { err: Error | null }> {
  state = { err: null as Error | null }
  static getDerivedStateFromError(err: Error) { return { err } }
  render() {
    if (this.state.err) {
      return (
        <div className="card lab-card">
          <div className="lab-head" style={{ cursor: 'default' }}>
            <div className="lab-title">
              <span className="lab-st lab-st-rejected">数据异常</span>
              <b>{this.props.title || '该实验记录'}</b>
            </div>
          </div>
          <div className="lab-body">
            <div className="note">这条记录渲染失败（字段缺失或格式不符）：<code>{String(this.state.err.message)}</code></div>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="lab-sec">
      <div className="lab-label">{label}</div>
      {children}
    </div>
  )
}

function LabTable({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  if (!rows.length) return null
  return (
    <table className="lab-tbl">
      {head.length > 0 && (
        <thead><tr>{head.map((h, i) => <th key={i} className={i > 0 ? 'lab-num' : ''}>{h}</th>)}</tr></thead>
      )}
      <tbody>
        {rows.map((r, i) => <tr key={i}>{r}</tr>)}
      </tbody>
    </table>
  )
}

function ExperimentCard({ e }: { e: any }) {
  const [open, setOpen] = useState(false)
  // 优先用显式 kind（engine/lab_registry.py 的 schema）；老数据缺 kind 时按字段形状兜底
  const isRecord = e.kind ? e.kind === 'change' : (!e.params && !e.results && !e.goals)
  return (
    <CardBoundary title={e.title}>
      <div className={`card lab-card ${open ? 'on' : ''}`}>
        <div className="lab-head" onClick={() => setOpen((o) => !o)} role="button" aria-expanded={open}>
          <span className={`lab-st ${STATUS_CLS[e.status] || 'lab-st-record'}`}>{e.status || '—'}</span>
          <div className="lab-title">
            <b>{e.title}</b>
            <span className="lab-meta">
              {e.layer}{e.date ? ` · ${e.date}` : ''}{e.date_note ? ` · ${e.date_note}` : ''}
            </span>
          </div>
          <span className="chev">{open ? '▾' : '▸'}</span>
        </div>

        <div className="lab-sum">
          {e.vs && <div className="lab-vs"><span className="lab-vs-k">对照</span>{e.vs}</div>}
          {e.caveat && <div className="lab-caveat">⚠ {e.caveat}</div>}
        </div>

        {open && (
          <div className="lab-body">
            {e.hypothesis && (
              <Section label="假设"><p>{e.hypothesis}</p></Section>
            )}
            {e.why && (
              <Section label="背景 / 为什么"><p>{e.why}</p></Section>
            )}
            {Array.isArray(e.params) && e.params.length > 0 && (
              <Section label="实验参数">
                <LabTable head={[]} rows={e.params.map((p: any) => [
                  <td className="lab-k">{p.k}</td>,
                  <td className="num lab-num">{p.v}</td>,
                  <td className="muted small">{p.note}</td>,
                ])} />
              </Section>
            )}
            {Array.isArray(e.results) && e.results.length > 0 && (
              <Section label="改动前后效果（年化 / 回撤 / Sharpe）">
                <LabTable
                  head={['变体', '年化', '回撤', 'Sharpe', '说明']}
                  rows={e.results.map((r: any) => [
                    <td>{r.label}</td>,
                    <td className="num lab-num">{pct(r.cagr)}</td>,
                    <td className="num lab-num">{pct(r.maxdd)}</td>,
                    <td className="num lab-num">{num(r.sharpe, 3)}</td>,
                    <td className="muted small">{r.note || ''}</td>,
                  ])}
                />
              </Section>
            )}
            {e.result && (
              <Section label="结果"><p>{e.result}</p></Section>
            )}
            {e.goals && (e.goals.annualized || e.goals.drawdown) && (
              <div className="lab-sec lab-goals">
                <div>
                  <div className="lab-label">核心目标 ①：年化收益</div>
                  <p>{e.goals.annualized || '—'}</p>
                </div>
                <div>
                  <div className="lab-label">核心目标 ②：最大回撤</div>
                  <p>{e.goals.drawdown || '—'}</p>
                </div>
              </div>
            )}
            {e.robustness && (
              <Section label="统计效度检验（幸存者偏差 vs 真实效益）"><p>{e.robustness}</p></Section>
            )}
            {e.conclusion && (
              <Section label="实验总结">
                <p className="lab-conclusion">{e.conclusion}</p>
              </Section>
            )}
            {e.next && (
              <Section label="后续"><p>{e.next}</p></Section>
            )}
            {Array.isArray(e.artifacts) && e.artifacts.length > 0 && (
              <Section label="产物">
                <div className="lab-artifacts">
                  {e.artifacts.map((a: string) => <code key={a}>{a}</code>)}
                </div>
              </Section>
            )}
            {isRecord && !e.conclusion && !e.result && (
              <div className="muted small">该条为口径变更登记，无对照实验表。</div>
            )}
          </div>
        )}
      </div>
    </CardBoundary>
  )
}

export default function Lab({ data }: { data: any }) {
  if (!data) return <div className="card"><div className="empty">实验数据缺失</div></div>
  const { baseline_version, baseline_metrics, principles, experiments } = data
  const list: any[] = experiments || []
  return (
    <>
      <div className="card lab-principles">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <h3 style={{ margin: 0 }}>实验准则</h3>
          <span className="small muted">
            当前基准 <b className="num">{baseline_version}</b> · <Metrics m={baseline_metrics} />
          </span>
        </div>
        <div className="lab-goal-row">
          {(principles?.goals || []).map((g: string) => (
            <div className="lab-goal" key={g}>◎ {g}</div>
          ))}
        </div>
        <ul className="lab-rules">
          {(principles?.rules || []).map((r: string) => <li key={r}>{r}</li>)}
        </ul>
        <div className="lab-count muted small">
          共 {list.length} 条记录 · 点击任意一条展开详情（徽章：绿=已采用/已落参，红=已否决，
          黄=待拍板/部分否决，灰=仅登记）。<br />
          <b>产出规则</b>：新增实验用
          <code>python3 engine/lab_registry.py new &lt;id&gt; --kind experiment|change --title '…' --layer '…'</code>
          生成字段齐全的骨架（未填完时校验必然失败）；<code>ark.py verify</code> 与
          <code>ark.py daily</code> 都会校验 schema，坏记录进不了站点。规范见
          <code>docs/实验记录规范.md</code>。
        </div>
      </div>

      {list.map((e: any) => <ExperimentCard key={e.id} e={e} />)}
    </>
  )
}
