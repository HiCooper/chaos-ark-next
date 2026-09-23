# 混沌方舟 ARK · Next

不预测风暴，只造方舟。

A 股行业方向的**趋势研判系统**：每天回答一个问题——今天哪些行业值得「入手」或「埋伏」，预计多长周期后需要出手。只做方向判断，不做自动交易。

本仓库是 `chaos-ark` 的独立演进版（`~/Projects/QoderProjects/chaos-ark`）：
两套系统共用「行业打分 → 方向结论」的思路，但引擎与前端完全独立。
本版的差异：

- **两层架构**：申万行业/细分赛道指数只负责「方向判断」，真实可交易 ETF 只负责「收益结算」。行业指数不可交易，用它算收益无法兑现；ETF 上市前的日期视为不可交易，绝不用指数回填。
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
  src/App.tsx      四个标签页：今日结论 / 复盘 / 引擎 / 实验
  src/HistoryStrip.tsx   历史结论热力图（交易日历）
  src/chart.tsx    ECharts 净值曲线（缩放 + 每日结论带 + B/S 标记）
  public/data/     引擎产出的 JSON（history / today / review / experiments / day/）

site/            部署产物（web/dist 同步 + data 镜像，可直接静态托管）
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
| 埋伏和入手买入份额一样吗 | **一样**。结算层不区分 action，份额差异来自风险平价 1/σ 权重 × 综合分倾斜 `(分/50)^0.5`，与入手/埋伏标签无关 |
| 卖出规则只有持有到期一条吗 | **主口径是**。持有 H 个交易日收盘卖出（H ∈ 60/80/100 择优）。另有「连续 3 日跌破 MA20」失效预警——只作对照与提示，不进生产曲线 |
| 买入时点 | 信号日 T 收盘出结论，T+1 开盘买入 |
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
