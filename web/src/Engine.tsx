import React, { useState } from 'react'
import { Card, Chip, pct } from './ui'
import { Bars } from './ui'

const stepsFor = (u: any) => {
  const codes = u?.codes ?? '—'
  const days = u?.days ?? '—'
  const first = u?.first ?? '—'
  return [
    {
      t: '数据快照',
      d: `抓取申万一级 ${u?.l1 ?? '—'} 个行业 + 细分赛道佐证，加基准指数，共 ${codes} 个标的的日线，`
        + `回溯至 ${first}（${days} 个交易日）。原始响应落盘缓存，重跑不重新联网即可复现。`,
    },
    {
      t: '特征库',
      d: `把 OHLCV 展成 ${codes} × ${days} 的特征张量：多周期收益、均线偏离、波动、回撤、量能、`
        + '相对行业等权的超额。列式 npz 存储。',
    },
    { t: '环境识别', d: '5 个分项（宽度 / 趋势 / 动能 / 低波 / 流动性）合成环境热度分，3 日平滑 + 连续 2 日确认才切换档位，避免日频抖动；极端回撤或波动自动降级。' },
    { t: '六维打分', d: '长期主线、回调空间、抛压衰竭、触底确认、风险控制、环境适配。方向由 Rank IC 实证确定，不是直觉。' },
    { t: '共振分类', d: '入手（主线成立 + 触底确认）／埋伏（主线回调 + 抛压衰竭）／观察／回避。回避项明确列出原因。' },
    { t: '周期估计', d: '用该行业历史上「状态最相似的 K 个交易日」统计各窗口的真实胜率与超额，按风险调整期望择优；样本不足时标记 low_confidence，不允许臆测。' },
    { t: '结论生成', d: '每条结论带触发理由链、失效条件、预计到期日，以及能真正买到的 ETF 标的及代码（细分赛道无对应 ETF 时，回退到所属一级行业的 ETF 并标注）。' },
    { t: '交易结算', d: '行业指数只用于判断方向（不可交易），收益一律用真实 ETF 结算：信号日 T+1 开盘买入、持有建议周期后收盘卖出，扣双边佣金万二 + 滑点万五。ETF 上市前的日期标记为不可交易，绝不用指数回填收益。' },
    { t: '复盘引擎', d: '对每一条历史结论做结果判定、归因、反事实统计，输出「做错了什么 / 为什么 / 如何改」，并回写为改进建议。改进必须像 A/B/C 三种执行口径那样跑出来对比，跑输就如实记录失败。' },
  ]
}

/** 参数表：每个旋钮是什么、当前值、作用在哪一层。与「规则」分开，互不混淆。 */
function ParamsCard({ params }: { params: any[] }) {
  const mods: any[] = params || []
  const n = mods.reduce((a, m) => a + (m.items?.length || 0), 0)
  return (
    <Card
      title="当前生效参数（什么参数、什么含义）"
      extra={<Chip tone="accent">{n} 个参数 · {mods.length} 个模块</Chip>}
    >
      <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
        每个旋钮的<b>当前值 / 它是什么 / 作用在哪一层</b>。数值全部实时取自
        <code> engine/config.py</code>（个别常量在 classify / regime / horizon 内，已在来源里标注），
        改配置后重跑管线即同步——不存在前端硬编码。「已停用」表示该旋钮仍在配置里但不参与计算。
      </div>
      {mods.map((m, i) => (
        <div key={i} style={{ marginBottom: 16 }}>
          <div className="ruleIntro" style={{ marginBottom: 6 }}>
            <b>{m.module}</b>　<span className="muted">{m.intro}</span>
          </div>
          <table>
            <thead>
              <tr>
                <th style={{ width: '26%' }}>参数</th>
                <th style={{ width: '17%', textAlign: 'right' }}>当前值</th>
                <th>含义 / 影响</th>
                <th style={{ width: '9%' }}>作用层</th>
              </tr>
            </thead>
            <tbody>
              {m.items.map((it: any, j: number) => (
                <tr key={j}>
                  <td><code>{it.key}</code></td>
                  <td className="num" style={{ textAlign: 'right', fontWeight: 600 }}>{it.display}</td>
                  <td className="small" style={{ color: 'var(--ink-2)' }}>{it.meaning}</td>
                  <td><Chip>{it.scope}</Chip></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </Card>
  )
}

function RulesPanel({ rules }: { rules: any }) {
  const groups: any[] = rules.groups || []
  const [gk, setGk] = useState<string>(groups[0]?.k || '')
  const cur = groups.find((g) => g.k === gk) || groups[0]
  if (!cur) return null

  return (
    <>
      <Card
        title="当前生效的交易规则"
        extra={<Chip tone="accent">{(rules.engine_version || '') + ' · 配置指纹 ' + String(rules.config_hash || '').slice(0, 8)}</Chip>}
      >
        <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
          规则回答「系统怎么决策」，与参数分开两处：参数表见下方「当前生效参数」。
          两者都直接导出自 <code>engine/config.py</code>，与引擎实跑同一份来源——
          改配置后重跑管线本页自动同步，不存在前端硬编码导致的过期。
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

      <ParamsCard params={rules.params || []} />

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
      <Card title="设计前提：为什么是两层——信号用行业指数，成交与收益用 ETF">
        <div className="small" style={{ color: 'var(--ink-2)', marginBottom: 10 }}>
          一句话：<b>行业指数回答「该不该做、做什么方向」，真实 ETF 回答「能不能做、赚了多少」</b>。
          两层各用各的最优数据，混用任何一层都会把系统推向不可执行或不可信。
        </div>
        <div className="grid g2" style={{ marginBottom: 10 }}>
          <div className="layerbox">
            <div className="layertag">判断层 · 申万行业指数（125 条 × 6468 个交易日，1999 年起）</div>
            <ul className="reasons" style={{ margin: 0 }}>
              <li>历史长、口径同质、无折溢价与流动性噪声——横截面比较（Rank IC）才有意义。</li>
              <li>带 L1/L2/L3 层级，才能做「主线共振」判断：一级方向 + 细分赛道互相印证。</li>
              <li>它<b>不可交易</b>：所以只用于方向，不参与任何收益计算。</li>
            </ul>
          </div>
          <div className="layerbox">
            <div className="layertag">结算层 · 真实可交易 ETF（123 只，前复权价 + 双边成本 0.14%）</div>
            <ul className="reasons" style={{ margin: 0 }}>
              <li>收益只能来自真的买得到、卖得掉的东西：T+1 开盘买入、持有到期收盘卖出。</li>
              <li>ETF 上市之前该行业视为不可交易，<b>绝不用行业指数回填</b>（这也是早期样本偏少的真实原因）。</li>
              <li>份额折算（拆分/合并）按前复权还原，不记为盈亏。</li>
            </ul>
          </div>
        </div>
        <ul className="reasons" style={{ marginTop: 0 }}>
          <li>
            <b>为什么不干脆用 ETF 做信号</b>：ETF 中位上市日 2020-12-29，2018-01-02 之前只有 36/123 只，
            回测会被砍到 ~2022 之后并引入幸存者偏差；而且 ETF 没有行业层级，重建不了共振逻辑，
            还带来折溢价、跟踪误差、场内流动性等噪声。
          </li>
          <li>
            <b>为什么不能用指数算收益</b>：指数不可交易，用指数算出来的收益无法兑现——这是"看着能赚、实盘赚不到"的经典来源。
          </li>
          <li>
            <b>由此得到的数据依赖方向（重要）</b>：<b>ETF 价格再新也生不出「该买什么」</b>——
            新的 ETF 行情只能改写已有交易清单的收益，不能生成新的清单；反过来，<b>行业指数不出数 → 当天没有结论
            → 复盘、买卖清单、影子账户全都推进不了</b>（缺的不是价格，是交易清单）。
            所以数据源滞后必须在<b>数据层</b>解决，而不是在结算层"跳过一天"。
          </li>
        </ul>
        <div className="note" style={{ marginTop: 10 }}>
          落地方式：申万官方日线滞后时，用申万<b>实时行情</b>补最近 1~2 个交易日——收盘价取实时真值、
          成交量/成交额按沪深300量比<b>估算</b>并逐行打 <code>est</code> 标记，官方出数后重跑
          <code> ark.py daily</code> 自动用真值覆盖。详见 <code>docs/数据兜底-实时行情补尾部.md</code>。
        </div>
      </Card>

      <Card title="工作流：每日结论背后的 9 步">
        {stepsFor(data.rules?.universe).map((s, i) => (
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
                  <td className="num" style={{ textAlign: 'right' }}>{pct(v.win_rate, 2, false)}</td>
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
          <li>资金曲线是<b>有限资金池的可执行账户</b>：初始本金一笔钱、卖出所得是唯一后续资金来源，
              建仓金额 = <code>min(账户总值 × 入场闸门 ÷ 目标持仓笔数, 可用现金)</code>，
              预算再按 1/σ × 综合分倾斜；<b>持仓权重建仓后随价格漂移，没有每日再平衡</b>，
              入场闸门（波动目标 / 暴露上限）只压新仓预算、压不了存量。这不是交易系统的业绩承诺。</li>
          <li>ETF 跟踪误差、折溢价、成分调整、涨跌停与流动性骤变未被单独建模。</li>
          <li>统计口径为样本内回测；分年度与跨牛熊分段结果已在复盘页披露，请优先看跨市场阶段是否一致再决定是否采信。</li>
        </ul>
        <div className="note" style={{ marginTop: 10 }}>
          复现方式：<code>python3 engine/ark.py daily --full</code>（取数 + 全量重放 + 同步 + 水位验收）；
          只重跑重放层用 <code>python3 engine/pipeline.py --all</code>；
          因子方向可复跑 <code>python3 engine/diag.py</code>；
          账本可执行性对账用 <code>python3 engine/ark.py verify</code>。
        </div>
      </Card>

      {rules && <RulesPanel rules={rules} />}
    </>
  )
}
