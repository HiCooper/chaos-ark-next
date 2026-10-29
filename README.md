# 混沌方舟 ARK · Next

不预测风暴，只造方舟。

A 股行业方向的**趋势研判系统**：每天回答一个问题——今天哪些行业值得「入手」或「埋伏」，预计多长周期后需要出手。只做方向判断，不做自动交易。

本仓库是 `chaos-ark` 的独立演进版（`~/Projects/QoderProjects/chaos-ark`）：
两套系统共用「行业打分 → 方向结论」的思路，但引擎与前端完全独立。
本版的差异：

- **两层架构**：申万行业/细分赛道指数只负责「方向判断」，真实可交易 ETF 只负责「收益结算」。行业指数不可交易，用它算收益无法兑现；ETF 上市前的日期视为不可交易，绝不用指数回填。
- **数据兜底**：申万官方日线常滞后 1 天（ETF/基准更新及时）。缺口 ≤2 天时用申万**实时行情**补尾部：收盘价取实时真值，**成交量/成交额为估算**（按沪深300量比），并在记录里打 `est` 标记、页面顶部提示；官方出数后重跑 `ark.py daily` 自动用真值覆盖（见 `docs/数据兜底-实时行情补尾部.md`）。另有一类相反的缺陷：申万**整日缺数据**（某交易日 120 只指数全无 bar，而 ETF/指数当日正常交易）会让系统整天跳过——`ark.py status`/`verify` 有「判断层日历缺口」守卫：已知空洞登记在 `config.KNOWN_JUDGE_GAPS`、批准补齐的日期写进 `config.GAP_FILL_DATES`（按 ETF 日收益前推，标 `est`，官方补发自动覆盖），`verify` 还会校验判断层与结算层日历必须同日（见 `docs/数据缺口-判断层整日缺失.md`）。
- **结算层是有限资金池的可执行账户**（`ark-1.8.0-executable`）：初始本金一笔钱，此后卖出所得是唯一资金来源；建仓金额 = `min(账户总值 × 入场闸门 ÷ 目标持仓笔数, 可用现金)`；持仓权重建仓后随价格漂移，**系统内不存在每日再平衡**；净值由现金账逐日记账产生，并可由事件日志独立重放（`ark.py verify` 的账本对账，偏差 < 5e-4）。详见 `docs/口径变更-2026-09-23-可执行账户.md`。
- **两层日历必须对齐（结算层是判断层的前缀）**：判断层（申万指数）与结算层（ETF 日线）出数时间不同步。结算层**尾部落后**时（新浪 ETF 常晚一晚），系统把结算日历与结论一起截到 ETF 末日、打印 ⚠，绝不在「ETF 还没有开盘价」的那天出结论——否则 T+1 建仓会被静默跳过、盯市退化成上一收盘价；结算层**中间有洞或多出日期**则 `verify` 直接失败。见 `docs/DATA-LAYERS.md` 的「层间不变式」。
- **抄底兜底通道（DIP，已启用）**：主策略在恐慌期会连续零结论、资金闲置（近期连续 11 个交易日 0 结论、均投出 51%）。通道规则＝「主策略当日零候选的空窗日」+「行业 20 日跌幅 ≥15% 且较近 5 日低点回升 >3%」→ T+1 开盘买对应 ETF、持有 20 日，与主策略共享 K=24 槽位与现金（不预留额度）。实测全窗口 13.87%→15.56%、回撤 −33.13%→−27.64%、Calmar 0.419→0.563；样本内（单边牛市）仍 −1.06pp，本质是「参与度补充器」而非全天候 alpha；**2026-10-29 已落参启用**（`ARK_DIP=0` 可临时关闭），页面以「抄底」黄色标签 + 「非结论驱动」注明。见 `docs/抄底兜底通道.md`。
- **前端为纯静态站点**：React + Vite + ECharts，数据是三份 JSON（`history/today/review`），由引擎一次性产出，无需后端服务。
- **历史日历**：GitHub 贡献图风格的交易日热力图（2605 个交易日一屏纵览），每格颜色 = 当日市场环境（进攻/中性/防守/恐慌），圆点 = 当日有「入手」结论、角标 = 有「埋伏」结论，点击任意格子查看那天的完整结论。

---

## 目录结构

```
engine/          Python 引擎（策略核心，无第三方重依赖，numpy 即可）
  config.py        全部魔法数字集中于此（阈值/权重/成本/路径）
  pipeline.py      主管线：数据快照→特征→环境→打分→分类→周期→结论→复盘
  classify.py      共振分类：BUY_NOW（入手）/ AMBUSH（埋伏）/ 回避
  horizon.py       持有周期估计（60/80/100 日择优）
  settlement.py    ETF 结算层：T+1 开盘买、持有到期收盘卖、风险平价+波动目标
  rules.py         生效规则快照（页面展示与引擎同源，改 config 即同步）
  review.py        复盘统计（胜率、回撤、牛熊分段、集中度取舍表）
  lab_*.py         实验台：反事实扫描（先精确复现基线才允许下结论）

web/             React + Vite 前端（纯静态）
  src/App.tsx      五个标签页：今日研判 / 我的持仓 / 复盘 / 引擎 / 实验
  src/Calendar.tsx  历史结论热力图（交易日历）
  src/chart.tsx    ECharts 净值曲线（缩放 + 每日结论带 + B/S 标记）
  src/MyBook.tsx   「我的持仓」：账户卡片 + 浅红强调的「下一个交易日操作」区块
                   （买入金额/约合手数、卖出金额/参考盈亏）、账户总值/现金/市值/浮盈、
                   账户卡片（总值/市值/现金/浮动盈亏/总仓位）、逐笔持仓（买入日倒序）、
                   当日已成交对账；被槽位/现金挡下时列出今日结论的建仓候选。
                   点 ETF 弹出的走势核验弹窗**按入市起始日算这本账**（真钱金额、真实份数），
                   而不是引擎的名义账本（见 `docs/我的持仓-标的弹窗口径.md`）
  src/sim.ts       影子账户（档 2）：把引擎的资金记账规则逐日重放，消费 data/sim.json
  public/data/     引擎产出的 JSON（history / today / review / experiments / sim / day/ / etf/）

site/            部署产物（web/dist 同步 + data 镜像，可直接静态托管）
docs/            设计与口径文档（年度收益归因 / 数据兜底 / 数据缺口 / 抄底兜底通道 / 周期与行业选错审计 / 实验记录规范 /
                 我的持仓-标的弹窗口径）
engine/lab_registry.py  实验登记簿 schema + 校验 + 生成器（check / new / backfill-kind）
engine/ark.py    统一入口（init / daily / status / verify / sync / repair）
report/          研究报告与截图
```

## 运行

### 一键初始化（clone 之后）

```bash
python3 engine/ark.py init
```

自动完成：建 `.venv` → 装 `engine/requirements.txt` → 全量抓取 L0/L1（约 7 分钟）→
全量管线（L2+L3）→ 同步 `site/data/`。结束后 `engine/ark.py status` 应显示各层
水位同日。

### 日常更新（收盘后）

```bash
python3 engine/ark.py daily          # 取数 + 增量重放（尾部 12 天）+ 同步 + 水位验收
python3 engine/ark.py daily --full   # 改过引擎口径/config 后用这个（全量重放）
python3 engine/ark.py status         # 只读水位报告（「更新到哪天了」的唯一口径）
python3 engine/ark.py verify         # L0 sha256 校验 + 特征层一致性
```

要点：

- **`pipeline.py` 自身不触网**，只读 `cache/panel.json`。取数由
  `datasource_sw.py --refresh`（申万指数+基准）与 `build_etf_panel.py --refresh`
  （ETF）完成——两者都**必须带 `--refresh`**，否则静默吃 L0 缓存，新交易日永远不落盘。
  `ark.py daily` 已把这些步骤与参数固化。
- **增量重放的前提**是「历史结论不随新交易日改变」。引擎全链
  （indicators/feature_store/regime/scoring/horizon）只向后看，且特征窗口起点
  锚定在 `cache/features.fp.json`、特征以 float64 落盘；运行时用 canary 自检
  （尾部重算日与上次落盘结论逐字节比对），不一致自动回退全量重放。
- 改了 `config.py` 或任何引擎模块后，`day/<date>.json` 缓存里的 `config_hash` /
  `engine_version` 不匹配会自动失效，等价于全量重放；`daily --full` 可强制。

### 前端（不跑引擎，先看现有数据）

```bash
cd web
npm install
npm run dev        # 开发：http://localhost:5173
npm run build      # 产物在 web/dist/
```

### 手工分步（等价于 ark.py daily）

```bash
cd engine
python datasource_sw.py --refresh        # L0+L1 行业面板
python build_etf_panel.py --refresh      # L1 ETF 面板
python pipeline.py --refresh --all --tail 12   # L2+L3
cd .. && python3 engine/ark.py sync      # web/public/data → site/data
```

### 产出

`web/public/data/{history,today,review,experiments}.json` + `day/<date>.json`，
前端直接消费；`site/data/` 是部署副本（`ark.py sync` 镜像）。

## 关键规则速览（详见引擎页「规则参数」面板，与 config.py 同源）

| 问题 | 答案 |
|---|---|
| 同行业持有后能继续加仓吗 | **能**。没有「已持有则禁买」约束；只有总容量 32 槽 + 每日新建 2 笔的限制。「限制同行业建仓」实测回撤更差，已列入未采纳 |
| 埋伏和入手买入金额一样吗 | **一样**。结算层不区分 action；金额差异来自建仓预算的 1/σ × 综合分倾斜 `(分/50)^0.5`，与入手/埋伏标签无关 |
| 每笔建仓买多少钱 | `min(账户总值 × 入场闸门 ÷ K, 可用现金)`，K = `PORTFOLIO.target_positions`（目标持仓笔数，当前 24）。**入场闸门（波动目标 / F5 暴露上限）已停用**——可执行口径下它们压不了存量、实测负贡献；风险旋钮改用 K |
| 卖出后能马上再买吗 | 卖出在当日收盘、买入在次日开盘，**当日所得次日才可用**；当日到期的槽位也次日才腾出 |
| 会为了保持目标权重而调仓吗 | **不会**。权重建仓后随价格漂移；系统只做建仓与到期卖出，没有再平衡 |
| 卖出规则只有持有到期一条吗 | **主口径是**。持有 H 个交易日收盘卖出（H ∈ 60/80/100 择优）。另有「连续 3 日跌破 MA20」失效预警——只作对照与提示，不进生产曲线 |
| 买入时点 | 信号日 T 收盘出结论，T+1 开盘买入 |
| 现金收益 | **不计息**（`RISK_CONTROL.cash_yield = 0`，2026-09-23 按用户口径改；原 1.0% 对齐货基水平） |
| 怎么调风险 | **只调 `target_positions`（K）**：K 越小越集中、均值越高、尾部越深。滚动一年：K=8 → 19.7%/−35.8%；**K=24（当前）→ 14.5%/−21.8%**；K=32 → 11.5%/−21.7% |
| ETF 拆分/合并怎么算 | **按份额折算复权**。面板存不复权价，折算日价格整数倍跳变而价值不变，结算时以申万行业指数为参照还原（`config.ETF_QFQ`，逻辑见 `engine/qfq.py`）；L0 原始层不动 |
| 成本 | 单边佣金万 2.5 + 滑点万 2.5，往返约 0.1%，全部收益已扣成本 |

## 与 chaos-ark（旧版）的关系

| | 本仓库（next） | chaos-ark |
|---|---|---|
| 架构 | 纯静态：引擎产 JSON，前端只读 | Python 服务（ark serve）+ SQLite |
| 判断层 | 申万 L1/L2 指数（31 行业 + 细分赛道） | 申万行业 |
| 结算层 | 真实 ETF（上市前不可交易，不回填） | 模拟仓（槽位制、冷却期、硬止损） |
| 加仓纪律 | 允许同行业重复建仓（容量制） | 已持仓不加仓 |
| 前端 | React + ECharts 静态站 | React + 自研 SVG 图 |

两者结论互不通用，勿混用对方的纪律解读本系统的成交。
