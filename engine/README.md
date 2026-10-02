# engine/ — 代码索引

> 目的：`ls engine/` 的认知成本从 64 个文件降到 20 个生产模块。
> 分类依据（2026-10-02，随架构评审建立）：**生产链** = `ark.py daily/init/verify/status`
> 会运行或被 import 的模块；其余为一次性实验台 / 诊断 / 抓取器 / 工具。
> 本文件只做导航，**行为与参数的唯一权威是 `config.py`，数据分层的权威是 `docs/DATA-LAYERS.md`**。

## 一、生产链（20 个）——daily/verify 的执行面

| 文件 | 职责（docstring 摘要） | 分层 |
|---|---|---|
| `ark.py` | 统一入口：取数→面板→特征→重放→复盘→部署副本 | CLI |
| `pipeline.py` | 主管线：8 步工作流串成一次可重复执行 | L2/L3 |
| `datasource.py` | 东财日线抓取（被 pipeline 引用为数据接口） | L0 |
| `datasource_sw.py` | 申万官方口径抓取（akshare 申万指数 + 新浪 ETF/基准） | L0 |
| `rawstore.py` | L0 原始层：write-once 不可变快照 + manifest sha256 | L0 |
| `build_etf_panel.py` | 每个申万一级行业选「最早上市 + 流动性最好」的 ETF | L1 |
| `build_etf_webdata.py` | 展示层：标的走势弹窗数据 | View |
| `feature_store.py` | 特征库：面板 →「标的 x 日期 x 字段」三维张量 | L2 |
| `indicators.py` | 指标层：OHLCV → 每日特征向量（纯函数） | L2 |
| `regime.py` | Step 1 环境识别 | L3 |
| `scoring.py` | Step 2-3 行业六维打分 | L3 |
| `classify.py` | Step 4 信号共振与结论分类 | L3 |
| `horizon.py` | Step 5 持有周期估计（历史相似状态匹配） | L3 |
| `rules.py` | 生效规则快照：从 config 导出前端展示的规则参数 | L3 |
| `settlement.py` | ETF 结算层：方向结论 → 真实可执行资金曲线 | L4 |
| `review.py` | Step 7 复盘引擎 | L4 |
| `config.py` | **静态配置（参数唯一权威，每项带依据+日期+复现命令）** | 全局 |
| `etf_map.py` | 申万行业/细分赛道 → 可交易 ETF 映射 | 支撑 |
| `qfq.py` | ETF 份额折算检测与复权修正（34 断点/29 ETF） | 支撑 |
| `lab_registry.py` | 实验登记簿 schema、校验器（verify 会跑） | 治理 |
| `verify_ledger.py` | 账本可执行性对账（verify 会跑） | 治理 |

## 二、L0 抓取器（3 个）——独立于 daily，需手动更新

| 文件 | 职责 | 现状 |
|---|---|---|
| `fetch_sw_valuation.py` | 申万官方指数分析（估值/换手/市值，162 指数） | 2026-09-29 一次抓取，71M |
| `fetch_analyst_rank.py` | 巨潮投资评级流水（价格外第 2 信息源） | 2026-09-29 一次抓取，192M |
| `fetch_sw_component.py` | 申万一级成分股映射（评级聚合所需） | 2026-09-29 一次抓取，744K |

> 水位见 `ark.py status` 的「非管线 kind」段（2026-10-02 起）。对应实验结论见
> `docs/估值与基本面信息源实验-2026-09-29.md`、`docs/拥挤度与分析师预期实验-2026-09-29.md`。

## 三、工具（3 个）——离线维护，不在 daily 链上

| 文件 | 职责 |
|---|---|
| `rebuild_review.py` | 只跑「特征缓存→结论重放→复盘」重建 review.json |
| `check_regress.py` | 轻量回归：classify/scoring 旋钮在默认值下不改变结论 |
| `offline_day.py` | 离线重建历史 day/<date>.json |

## 四、实验台 lab_*（30 个）——一次性产物，保留供复现

> 全部**离线、只读**：不写生产产物、不改 config。每个实验的结论登记在
> `web/public/data/experiments.json`（27 条），详细报告在 `docs/` 对应文档。

| 文件 | 一句话 | docs 出处 |
|---|---|---|
| `lab_flex.py` | 持仓灵活性/退出规则阴影重放（verify 模式自证与生产逐位相等） | 持仓灵活性…2026-10-02 |
| `lab_crowding.py` | 拥挤度方向 1：低换手分位是否值得落参 | 拥挤度与分析师预期…2026-09-29 |
| `lab_crowding_cond.py` | 条件化拥挤度（只在弱市启用） | 条件化拥挤度…2026-09-29 |
| `lab_crowding_replay.py` | 条件化拥挤度组合层影子重放 | 条件化拥挤度…2026-09-29 |
| `lab_valuation.py` | 估值/换手/市值增量信息诊断 | 估值与基本面…2026-09-29 |
| `lab_forecast.py` | 分析师预期修正因子 | 拥挤度与分析师预期…2026-09-29 |
| `lab_exit.py` | 退出规则族实测（到期/延展/破位早退） | 周期错配归因审计…2026-09-24 |
| `lab_horizon.py` | 周期估错归因 + 周期选择机会上限 | 周期错配归因审计…2026-09-24 |
| `lab_stage_horizon.py` | 阶段自适应持有周期 | 同上 |
| `lab_sector.py` | 「行业选错」归因审计 | 行业选错归因审计…2026-09-24 |
| `lab_sector_fix.py` | 「行业选错」三个候选方向 | 行业选错-三方向…2026-09-24 |
| `lab_fresh_median.py` | 随机入场一年为何差：归因 + 突破方向 | 随机入场一年-归因…2026-09-24 |
| `lab_entry_dates.py` | 随机入市日一年期收益验证 | 随机入场一年系列 |
| `lab_gate2024.py` | 结论闸门专项 | 2024 系列实验 |
| `lab_gate_settle.py` | 从实验台 day 文件重建交易清单并结算 | — |
| `lab_mature_weight.py` | MATURE 降权信号层实验 | 口径变更…2026-09-23 |
| `lab_mature_oos.py` | MATURE 降权样本外审计 | 口径变更…2026-09-23 |
| `lab_exec_risk.py` | 可执行口径下风控重扫 | 口径变更…2026-09-23 |
| `lab_cooldown.py` | 可执行口径尾部控制扫描 | 口径变更…2026-09-23 |
| `lab_combo.py` | 信号层×组合层叠加（暴露上限网格） | — |
| `lab_stability.py` | 组合层稳定性（1.6.0 基线） | — |
| `lab_signal_break.py` | 信号层突破 G3/G4（1.6.0 基线） | — |
| `lab_trend_quality.py` | 趋势因子新增信息诊断 | 趋势因子review…2026-09-28 |
| `lab_downside.py` | 下行市场表现优化 | 下跌年份系列 |
| `lab_downyear.py`~`lab_downyear5.py` | 下跌年份不亏损五轮迭代 | 下跌年份系列 |
| `lab_registry.py` | （见生产链——它不是实验，是登记簿本体） | — |

## 五、诊断 diag*（8 个）——只读分析

| 文件 | 一句话 |
|---|---|
| `diag.py` | 各维度/原始指标与未来超额的 Rank IC |
| `diag2.py` | 反转/超跌反弹/量能枯竭 + 动量的环境条件 |
| `diag3.py` | 「长期主线动量 + 短期回调反转」双层结构确认 |
| `diag_2024.py` | 2024 专项 |
| `diag_gate.py` / `diag_gate2.py` | 某年「不出结论」的闸门分解 |
| `diag_rank.py` | 闸门说「不」的那些天排名里有没有钱 |
| `diag_state.py` | 引擎「看不上」的状态事后赚不赚钱 |

## 六、已知死引用（docs → 已删除脚本，共 15 个）

以下脚本在 2026-10-02 评审时**已不存在**，但 `docs/` 早期文档中仍留有引用（复现命令失效，
属历史记录，不回填）：`lab_dd / lab_diag / lab_grid ~ lab_grid6 / lab_opt ~ lab_opt6 /
oos_b / verify_parity`。这些脚本的结论均以文档正文与登记簿为准。

## 七、新脚本约定（2026-10-02 起）

- **新的实验脚本放 `engine/lab/`**（见该目录 README），不再往 `engine/` 平铺；
- 存量脚本**不迁移**——保证 2026-10-02 之前的文档复现命令逐字可跑；
- 一次性诊断脚本：结论写进 docs 后即删，不长期保存；
- 命名用**日期后缀**（如 `lab_crowding_20260929.py`）而非数字版本后缀。
