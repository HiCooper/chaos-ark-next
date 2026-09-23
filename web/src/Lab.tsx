import React, { useState } from 'react'

// 状态 → 徽章配色（沿用全站扁平纸墨风）
const STATUS_CLS: Record<string, string> = {
  '已采用': 'lab-st-adopted',
  '已否决': 'lab-st-rejected',
  '待拍板': 'lab-st-pending',
}

const pct = (v: number | undefined, signed = true) => {
  if (v == null || Number.isNaN(v)) return '—'
  const s = (v * 100).toFixed(2) + '%'
  return signed && v > 0 ? '+' + s : s
}

function Metrics({ m }: { m: any }) {
  if (!m) return <span className="muted">—</span>
  return (
    <span className="num">
      {pct(m.cagr)} / {pct(m.maxdd)}
      {m.sharpe != null ? ` / Sharpe ${m.sharpe.toFixed(2)}` : ''}
    </span>
  )
}

function ExperimentCard({ e }: { e: any }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="card lab-card">
      <div className="lab-head" onClick={() => setOpen((o) => !o)}>
        <div className="lab-title">
          <span className={`lab-st ${STATUS_CLS[e.status] || ''}`}>{e.status}</span>
          <b>{e.title}</b>
          <span className="lab-meta">{e.layer} · {e.date}</span>
        </div>
        <div className="lab-vs small muted">{e.vs}</div>
      </div>

      {open && (
        <div className="lab-body">
          <div className="lab-sec">
            <div className="lab-label">假设</div>
            <p>{e.hypothesis}</p>
          </div>

          <div className="lab-sec">
            <div className="lab-label">实验参数</div>
            <table className="lab-tbl">
              <tbody>
                {e.params.map((p: any, i: number) => (
                  <tr key={i}>
                    <td className="lab-k">{p.k}</td>
                    <td className="num">{p.v}</td>
                    <td className="muted small">{p.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="lab-sec">
            <div className="lab-label">改动前后效果（年化 / 回撤 / Sharpe）</div>
            <table className="lab-tbl">
              <thead>
                <tr><th>变体</th><th>年化</th><th>回撤</th><th>Sharpe</th><th>说明</th></tr>
              </thead>
              <tbody>
                {e.results.map((r: any, i: number) => (
                  <tr key={i}>
                    <td>{r.label}</td>
                    <td className="num">{pct(r.cagr)}</td>
                    <td className="num">{pct(r.maxdd)}</td>
                    <td className="num">{r.sharpe != null ? r.sharpe.toFixed(3) : '—'}</td>
                    <td className="muted small">{r.note || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="lab-sec lab-goals">
            <div>
              <div className="lab-label">核心目标 ①：年化收益</div>
              <p>{e.goals.annualized}</p>
            </div>
            <div>
              <div className="lab-label">核心目标 ②：最大回撤</div>
              <p>{e.goals.drawdown}</p>
            </div>
          </div>

          <div className="lab-sec">
            <div className="lab-label">统计效度检验（幸存者偏差 vs 真实效益）</div>
            <p>{e.robustness}</p>
          </div>

          <div className="lab-sec">
            <div className="lab-label">实验总结</div>
            <p className="lab-conclusion">{e.conclusion}</p>
          </div>

          {e.artifacts?.length > 0 && (
            <div className="lab-sec">
              <div className="lab-label">产物</div>
              <div className="lab-artifacts">
                {e.artifacts.map((a: string) => <code key={a}>{a}</code>)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Lab({ data }: { data: any }) {
  if (!data) return <div className="card"><div className="empty">实验数据缺失</div></div>
  const { baseline_version, baseline_metrics, principles, experiments } = data
  return (
    <>
      <div className="card lab-principles">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <h3 style={{ margin: 0 }}>实验准则</h3>
          <span className="small muted">
            当前基准 <b className="num">{baseline_version}</b> ·{' '}
            <Metrics m={baseline_metrics} />
          </span>
        </div>
        <div className="lab-goal-row">
          {principles.goals.map((g: string) => (
            <div className="lab-goal" key={g}>◎ {g}</div>
          ))}
        </div>
        <ul className="lab-rules">
          {principles.rules.map((r: string) => <li key={r}>{r}</li>)}
        </ul>
      </div>

      {experiments.map((e: any) => <ExperimentCard key={e.id} e={e} />)}
    </>
  )
}
