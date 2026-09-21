import React, { useState } from 'react'
import { Card, Chip, pct } from './ui'
import { Bars } from './ui'

const STEPS = [
  { t: '数据快照', d: '抓取 31 个申万一级行业 + 60 个细分赛道（共 95 个标的）与 5 个宽基指数的日线，回溯至 2013 年（3332 个交易日）。原始响应落盘缓存，重跑不重新联网即可复现。' },
  { t: '特征库', d: '把 OHLCV 展成 95×3332 的特征张量：多周期收益、均线偏离、波动、回撤、量能、相对行业等权的超额。列式 npz 存储。' },
  { t: '环境识别', d: '5 个分项（宽度 / 趋势 / 动能 / 低波 / 流动性）合成环境热度分，3 日平滑 + 连续 2 日确认才切换档位，避免日频抖动；极端回撤或波动自动降级。' },
  { t: '六维打分', d: '长期主线、回调空间、抛压衰竭、触底确认、风险控制、环境适配。方向由 Rank IC 实证确定，不是直觉。' },
  { t: '共振分类', d: '入手（主线成立 + 触底确认）／埋伏（主线回调 + 抛压衰竭）／观察／回避。回避项明确列出原因。' },
  { t: '周期估计', d: '用该行业历史上「状态最相似的 K 个交易日」统计各窗口的真实胜率与超额，按风险调整期望择优；样本不足时标记 low_confidence，不允许臆测。' },
  { t: '结论生成', d: '每条结论带触发理由链、失效条件、预计到期日，以及能真正买到的 ETF 标的及代码（细分赛道无对应 ETF 时，回退到所属一级行业的 ETF 并标注）。' },
  { t: '交易结算', d: '行业指数只用于判断方向（不可交易），收益一律用真实 ETF 结算：信号日 T+1 开盘买入、持有建议周期后收盘卖出，扣双边佣金万二 + 滑点万五。ETF 上市前的日期标记为不可交易，绝不用指数回填收益。' },
  { t: '复盘引擎', d: '对每一条历史结论做结果判定、归因、反事实统计，输出「做错了什么 / 为什么 / 如何改」，并回写为改进建议。改进必须像 A/B/C 三种执行口径那样跑出来对比，跑输就如实记录失败。' },
]

function RulesPanel({ rules }: { rules: any }) {
  const groups: any[] = rules.groups || []
  const [gk, setGk] = useState<string>(groups[0]?.k || '')
  const cur = groups.find((g) => g.k === gk) || groups[0]
  const live = rules.live || {}
  if (!cur) return null

  return (
    <>
      <Card
        title="当前生效的交易规则与参数"
        extra={<Chip tone="accent">{(rules.engine_version || '') + ' · 配置指纹 ' + String(rules.config_hash || '').slice(0, 8)}</Chip>}
      >
        <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
          以下数值全部由 <code>engine/config.py</code> 直接导出，与引擎实跑用的是同一份来源——
          改配置后重跑管线，本页自动同步，不存在前端硬编码导致的过期。
        </div>

        <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
          {live.regime && <Chip>当前环境 {live.regime}</Chip>}
          {live.n_picks != null && <Chip>今日结论 {live.n_picks} 条</Chip>}
          {live.n_open != null && <Chip>在场持仓 {live.n_open} 笔</Chip>}
          {live.demoted != null && <Chip tone="warn">累计前置过滤降级 {live.demoted} 条</Chip>}
        </div>

        <div className="ruleNav">
          {groups.map((g) => (
            <button
              key={g.k}
              className={`ruleTab ${g.k === cur.k ? 'on' : ''}`}
              onClick={() => setGk(g.k)}
            >
              {g.title.split('：')[0]}
            </button>
          ))}
        </div>

        <div className="ruleIntro">{cur.intro}</div>

        <div className="ruleTable">
          {cur.items.map((it: any, i: number) => (
            <div className="ruleRow" key={i}>
              <div className="ruleHead">
                <div className="rl">{it.label}</div>
                <div className={`rv ${it.kind === 'num' || it.kind === 'pct' ? 'num' : ''} ${it.kind === 'live' ? 'is-live' : ''}`}>
                  {it.display}
                </div>
              </div>
              {it.why && <div className="ruleWhy">{it.why}</div>}
              {it.src && <div className="ruleSrc"><code>{it.src}</code></div>}
            </div>
          ))}
        </div>
      </Card>

      <Card
        title="已试但未采纳的规则（边界记录）"
        extra={<Chip tone="accent">{rules.rejected?.length || 0} 条</Chip>}
      >
        <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
          这些都是回测里确实成立、但<b>不满足 Pareto 改进</b>（会让年化 / 回撤 / 胜率三者之一变差）的方向。
          记在这里是为了避免重复试错，也便于交叉验证时知道哪些路已经走过。
        </div>
        <div className="rejList">
          {(rules.rejected || []).map((r: any, i: number) => (
            <div className="rejRow" key={i}>
              <div className="rejHead">
                <span className="rejRule">{r.rule}</span>
                <span className="rejRes">{r.result}</span>
              </div>
              <div className="rejWhy">{r.why}</div>
            </div>
          ))}
        </div>
      </Card>
    </>
  )
}

export default function Engine({ data, today }: { data: any; today: any }) {
  const o = data.overview || {}
  const rules = data.rules
  return (
    <>
      <Card title="工作流：每日结论背后的 9 步">
        {STEPS.map((s, i) => (
          <div className="step" key={i}>
            <div className="n">{i + 1}</div>
            <div className="b">
              <div className="t">{s.t}</div>
              <div className="d">{s.d}</div>
            </div>
          </div>
        ))}
        <div className="note" style={{ marginTop: 10 }}>
          关键不变式：跑「今天」和回测「过去 2600 天」走的是同一套代码路径与同一份打分矩阵，
          因此任何一天的结论都可以逐日重放核对。
        </div>
      </Card>

      <div className="grid g2">
        <Card title="方向是怎么定的：Rank IC 实证">
          <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
            引擎的每一个维度方向，都来自「该指标与未来超额收益的横截面 Rank IC」检验，而不是经验直觉。
            第一轮诊断的结果推翻了最初的设计：
          </div>
          <Bars
            rows={[
              { label: '120 日动量（主线）', value: 0.55, text: 'IC +0.55', color: '#1b8a4b' },
              { label: '250 日动量', value: 0.37, text: 'IC +0.37', color: '#1b8a4b' },
              { label: '60 日动量（短期）', value: 0.07, text: 'IC -0.07', color: '#c62828' },
              { label: '20 日动量（短期）', value: 0.07, text: 'IC -0.07', color: '#c62828' },
              { label: '距 MA60 偏离', value: 0.09, text: 'IC -0.09', color: '#c62828' },
            ]}
            max={0.6}
          />
          <div className="small muted" style={{ marginTop: 10 }}>
            结论：A 股行业层面是「长期主线强动量 + 短期反转」的双层结构。
            120 日动量分年度 IC 稳定在 0.52–0.58，五分组未来 40 日超额从 -5.0% 单调到 +6.0%；
            而 60 日以内的动量全部为负（t 约 -7 到 -10）。
            因此系统只做长期主线方向的判断，短期反转仅用于在主线内部找买点，
            且按主线强度做条件加权——下跌趋势中的反弹不参与。
          </div>
        </Card>

        <Card title="周期是怎么定的">
          <div className="small" style={{ color: 'var(--ink-2)' }}>
            不用固定周期，也不靠主观判断。对当日被选中的行业，取其状态向量
            （六维分 + 环境热度），在该行业自身历史上找出状态最接近的 K 个交易日，
            统计这批样本在之后各窗口的真实表现，按「平均超额 − 0.25×平均回撤」择优。
          </div>
          <div className="note" style={{ marginTop: 10 }}>
            回撤惩罚系数从 0.6 下调到 0.25，是因为 0.6 会系统性偏向最短窗口——
            而实证显示 10–40 日窗口平均超额接近于零，60–100 日才是有效区间。
            这个修正来自复盘引擎的归因统计，不是预设。
          </div>
          <div className="small muted" style={{ marginTop: 10 }}>
            当前有效窗口与表现：
          </div>
          <table style={{ marginTop: 4 }}>
            <thead><tr><th>窗口</th><th style={{ textAlign: 'right' }}>n</th><th style={{ textAlign: 'right' }}>胜率</th><th style={{ textAlign: 'right' }}>平均超额</th></tr></thead>
            <tbody>
              {Object.entries(o.by_horizon || {}).map(([k, v]: any) => (
                <tr key={k}>
                  <td>{k} 个交易日</td>
                  <td className="num" style={{ textAlign: 'right' }}>{v.n}</td>
                  <td className="num" style={{ textAlign: 'right' }}>{pct(v.win_rate, 0, false)}</td>
                  <td className="num" style={{ textAlign: 'right', color: v.avg_excess >= 0 ? 'var(--up)' : 'var(--down)' }}>{pct(v.avg_excess)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="口径与边界">
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
          <Chip>{today?.universe_size ?? '—'} 个候选行业/赛道</Chip>
          <Chip>方向：行业指数 · 收益：真实 ETF</Chip>
          <Chip>成本：双边 0.14%（佣金万二 + 滑点万五）</Chip>
          <Chip>引擎版本 {today?.engine_version}</Chip>
          <Chip>配置指纹 {today?.config_hash}</Chip>
        </div>
        <ul className="reasons" style={{ color: 'var(--ink-2)' }}>
          <li>本系统只做方向判断，不给出仓位、不加减仓规则、不连接任何交易通道。</li>
          <li><b>申万行业指数不可交易</b>，因此它只用于判断方向；一切收益数字都来自真实行业 ETF 的成交价，并扣除双边成本。</li>
          <li>ETF 上市之前该行业不可交易，这些结论不进入收益统计，也不用指数回填——这是早期样本偏少的真实原因。</li>
          <li>细分赛道若没有对应 ETF，用所属一级行业 ETF 结算，会稀释收益；页面上已标注代理关系。</li>
          <li>资金曲线是「有信号即建仓、持仓按 1/σ 风险预算与综合分加权、组合波动超目标时按比例降暴露」，
              不含主动择时加减仓；这不是交易系统的业绩承诺。</li>
          <li>ETF 跟踪误差、折溢价、成分调整、涨跌停与流动性骤变未被单独建模。</li>
          <li>统计口径为样本内回测；分年度与跨牛熊分段结果已在复盘页披露，请优先看跨市场阶段是否一致再决定是否采信。</li>
        </ul>
        <div className="note" style={{ marginTop: 10 }}>
          复现方式：<code>python engine/pipeline.py --full</code> 重放全部 2600+ 交易日并刷新页面数据；
          先跑 <code>python engine/build_etf_panel.py</code> 重建 ETF 映射；
          <code>python engine/diag.py</code> 与 <code>diag3.py</code> 可重新验证因子方向。
        </div>
      </Card>

      {rules && <RulesPanel rules={rules} />}
    </>
  )
}
