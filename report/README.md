# report/ — 历史快照存档（防误用标识）

> ⚠️ **本目录不是现行规格书。**
> 数据锚点：`ark/site/data/review.json`，engine_version = **`ark-1.1.0-etf`**（2026 年早期版本）。
> 其中的规则参数（如持有周期候选 `[60,80,100]`、回退 `H=20`、`min_days=20 共 3 笔持仓`、
> `avoid_crowd_pct` 等）均为 1.1.0 时代的交叉验证记录，与现行引擎**不一致**。

- 现行参数以 **`engine/config.py`** 为准（每个参数带依据 + 日期 + 复现命令）；
- 现行数据分层与口径以 **`docs/DATA-LAYERS.md`** 为准；
- 历史决策链（1.1.0 → 1.8.4）见 `docs/` 各实验记录与 `web/public/data/experiments.json` 登记簿。

本目录仅用于历史核对（例如追溯某参数最初的验证方式），**引用其结论前必须先核对
`engine/config.py` 是否已变更**。存档内容不做改写。
