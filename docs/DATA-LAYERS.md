# ARK 数据分层设计

> 核心原则：**同一份 config + 同一份 L1 面板 ⇒ 同一份结论（逐字可重放）**。
> 每一层只依赖它的下一层；任何非 L0 层都可以被删除后从下一层完整重建。

## 分层总览

```
L0  data/raw/            一手底层数据（外部API原样快照）   不可变，只增不改
L1  cache/               派生面板                          可从 L0 重算
L2  cache/               特征 / 环境序列                   可从 L1 重算
L3  web/public/data      导出层（前端消费）                可从 L2 重算
L4  cache/               实验台一次性产物                  可随时删除
```

## L0 — 原始层 `data/raw/`（不可变）

外部数据源的**原样快照**，这是系统的地基。拉下来之后不改：

| 目录/文件 | 内容 | 来源 |
|---|---|---|
| `catalog/sw_all.json` | 申万 L1/L2/L3 层级目录（代码+名称） | 申万宏源官方（akshare） |
| `sw_index/<code>.json` | 每个申万指数的日K（801010 等，2004 至今） | 申万宏源官方（akshare） |
| `bench/<code>.json` | 基准指数日K（上证/沪深300/创业板指/中证1000/中证500） | 新浪（akshare） |
| `etf/<secid>.json` | ETF 日K（前复权，成交额单位：元） | 新浪（akshare） |
| `kline_em/` | 东财 BK/基准日K（旧路径，东财解封后仍可用） | 东方财富 push2his |
| `manifest.json` | 每个文件的 source / fetched_at / rows / 区间 / sha256 | 本地生成 |

**不可变语义**（`engine/rawstore.py`）：
- write-once + merge-append：重复抓取时新日期追加，重叠日期**保留首采值**，绝不覆盖。
- 唯一例外：带 `est` 标记的**估算行**（官方源滞后时用实时行情兜底出来的，见
  `docs/数据兜底-实时行情补尾部.md`）可以被不带 `est` 的**官方值**覆盖
  （`put(..., overwrite_est=True)`）。一手数据彼此之间仍然先到先得。
- 每次写入更新 manifest（含 sha256），`python rawstore.py` 可校验文件是否被人为改动。
- 已知口径风险：前复权价在除权后会整体重述；重叠日期保留首采值意味着除权日附近
  可能有微小断层。重放不变式由 L1 冻结面板保证，不依赖 L0 逐字节复现。

## L1 — 派生面板 `cache/`

| 文件 | 内容 | 重建命令 |
|---|---|---|
| `panel.json` | 行业宇宙（申万L1+L2/L3佐证+基准）统一交易日轴行情面板 | `python datasource_sw.py [--refresh]` |
| `etf_panel.json` | 每行业可交易 ETF + 逐日选择 + 流动性 | `python build_etf_panel.py` |
| `all_blocks.json` / `fund_names.json` | 东财板块列表 / 基金名称表（辅助） | 随上述脚本自动补 |

这一层是**冻结点**：结论可重放性以 panel.json 的内容为准。刷新数据 = 生成新面板 =
新结论基线，属于显式行为。

## L2 — 特征层 `cache/`

| 文件 | 内容 | 重建命令 |
|---|---|---|
| `features.npz`(+meta) | 标的×日期×字段 三维张量（26 个序列特征 + 相对强度） | `pipeline.py --refresh` 或 `feature_store.py` |
| `regime.json` | 环境序列（ATTACK/NEUTRAL/DEFEND/PANIC） | 随 `--refresh` 自动重建 |

## L3 — 导出层 `web/public/data/`

`history.json` / `today.json` / `day/<date>.json` / `review.json`。
由 `pipeline.py --all` 从 L2 全量重放生成；`site/data/` 是它的部署副本。
**可以整目录删除**，重跑 pipeline 即可恢复。

`etf/<sid>.json` + `etf/_index.json`（标的走势核验弹窗）**不在**这条重建链上：
它由 `build_etf_webdata.py` 从 `cache/etf_panel.json` 单独导出，因此必须显式重建。
漏跑的症状很隐蔽——账户已经盯市到最新交易日，点开 ETF 却显示涨跌停在两天前
（2026-09-24 实际发生过一次）。现在 `ark.py daily` / `ark.py init` 都会跑它，
`_index.json` 里记了日期轴末端 `last`，`ark.py status` / `verify` 把它当作一层水位
参与「各层应当同日」的校验，不一致直接失败。

## L4 — 实验台 `cache/`

`lab_*.json` / `ic_diag*.json` / `lab_trades.json` 等：一次性实验产物，
可随时删除，不参与正式管线。

## 重跑手册

```bash
# 推荐：统一入口（串起全部步骤 + 水位验收 + site 镜像）
python3 engine/ark.py init          # clone 后一键初始化
python3 engine/ark.py daily         # 收盘后更新（取数 + 增量重放 + 同步）
python3 engine/ark.py status        # 水位报告（「更新到哪天了」的唯一口径）
python3 engine/ark.py verify        # L0 sha256 + 特征层一致性 + 各层水位同日

# 手工分步（等价于 daily）
cd engine && akshare-venv-python datasource_sw.py --refresh   # L0+L1 面板
akshare-venv-python build_etf_panel.py --refresh              # L1 ETF 面板（--refresh 不能省）
akshare-venv-python pipeline.py --refresh --all --tail 12     # L2+L3 增量
akshare-venv-python build_etf_webdata.py                     # 弹窗走势数据（不在上面的依赖链里）
python3 engine/ark.py sync                                    # web/public/data → site/data

# 东财路径（限流解除后可作为备源）
venv-python datasource.py --refresh                           # 写 data/raw/kline_em/
# 备源切换说明：两种来源的指数代码命名空间不同（90.BKxxxx vs 801xxx），
# 切换 = 换结论基线，需全量重跑并披露，不可混用两代面板。

# L0 完整性校验 / 修正某一天
venv-python rawstore.py
python3 engine/ark.py repair <YYYY-MM-DD> --yes               # write-once 的唯一例外
```

## 可重放性的两个不变式（2026-09-22 实证确立）

增量重放（`--tail N`，只重算尾部、历史日复用 `day/<date>.json`）成立的前提是
「追加新交易日不改变历史结论」。为此有两条硬约定，改动对应模块时必须复查：

1. **特征以 float64 落盘**（`feature_store.save`）。存 float32 会让「读缓存重放」
   与「现算重放」精度基线不一致——实测 2605 个 day 文件全部出现第 5 位小数级差异。
2. **特征窗口起点锚定**在 `cache/features.fp.json` 的 `window_start`（当前
   2012-09-07）。用尾部滑动窗口（`last_n`）时每追加一天起点就前移一天，等权指数
   等递推量的浮点末位会漂移。

运行时由 **canary 自检**兜底：`--tail N` 的尾部重算日若与上次落盘结论逐字节不一致，
立即放弃缓存、回退全量重放并大声告警。因此上述约定一旦被破坏，系统不会静默出错，
只会变慢（退化为全量）。实证记录：修复后 day 文件与基线 2605/2605 逐字节一致，
快速增量路径整条管线 CPU ≈ 56s。

## 稳定性与扩展性约定

1. **下游只认 schema，不认来源**：feature_store / scoring / classify 只依赖
   panel 的 `{dates, meta{name,level}, series}` 结构；换数据源不改下游。
2. **新数据源接入** = 写一个新的 builder 模块产出同构 panel.json，注册进
   本文档；绝不在打分/分类代码里写 if-else 判断来源。
3. **派生层丢失不告急**：只要 L0 在，任何一层都能离线重建；只有 L0 丢失才
   需要重新触网。
4. **禁止手改 L0**：修正数据错误 = 修抓取代码 + `--refresh` 重新抓取，
   不允许直接编辑 raw 文件（manifest sha256 会立刻暴露）。
