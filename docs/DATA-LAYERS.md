# ARK 数据分层设计

> 核心原则：**同一份 config + 同一份 L1 面板 ⇒ 同一份结论（逐字可重放）**。
> 每一层只依赖它的下一层；任何非 L0 层都可以被删除后从下一层完整重建。

## 分层总览

```
L0  data/raw/             一手底层数据（外部API原样快照）  不可变，只增不改
L1  cache/                派生面板                          可从 L0 重算
L2  cache/                特征 / 环境序列                   可从 L1 重算
L3  web/public/data/      导出层（数据，前端消费）          可从 L2 重算
L4  cache/                实验台一次性产物                  可随时删除
── 以下不在上面的依赖链上 ────────────────────────────────────────────
VIEW web/public/view/     展示层（只给页面看的东西）        由 L1 派生，必须显式重建
```

数据流向下（一手快照 → 页面），**重建方向相反**：每一层只依赖它的下一层，
任何非 L0 层都能整层删除后重建。展示层是例外——它不由 L3 生成，而与结算层
共用同一次 `load_etf_book()` 调用，因此单独成根、用虚线标出（见文末「展示层」一节）。

图形版见 `docs/数据分层.puml`（PlantUML 源），线上渲染版在站点「引擎」页的
「数据分层」卡片（内容由 `engine/rules.py build_layers()` 导出，数字实时取自磁盘）。

## L0 — 原始层 `data/raw/`（不可变）

外部数据源的**原样快照**，这是系统的地基。拉下来之后不改：

| 目录/文件 | 内容 | 来源 |
|---|---|---|
| `catalog/sw_all.json` | 申万 L1/L2/L3 层级目录（代码+名称） | 申万宏源官方（akshare） |
| `sw_index/<code>.json` | 每个申万指数的日K（801010 等，2004 至今） | 申万宏源官方（akshare） |
| `bench/<code>.json` | 基准指数日K（上证/沪深300/创业板指/中证1000/中证500） | 新浪（akshare） |
| `etf/<secid>.json` | ETF 日K（前复权，成交额单位：元） | 新浪（akshare） |
| `kline_em/` | 东财 BK/基准日K（旧路径，东财解封后仍可用） | 东方财富 push2his |
| `sw_valuation/<code>.json` | 申万官方指数分析日度数据：close/pe/pb/dp/换手/市值（162 指数，2012 至今，71M）。**非管线 kind**：手动 `python3 engine/fetch_sw_valuation.py`，接口 page_size 上限 ≈200（akshare 硬编码 50） | 申万官方 index_analysis（akshare） |
| `analyst_rank/<date>.json` | 巨潮个股投资评级流水，一天一文件（2022 至今，192M）。**非管线 kind**：手动 `python3 engine/fetch_analyst_rank.py` | 巨潮资讯（akshare） |
| `sw_component/<code>.json` | 申万一级行业成分股当前映射（含权重）。**非管线 kind**：手动 `python3 engine/fetch_sw_component.py`；⚠️ 取当前值 ⇒ 幸存者偏差，结论须带此披露 | 申万宏源官方（akshare） |
| `manifest.json` | 每个文件的 source / fetched_at / rows / 区间 / sha256 | 本地生成 |

> **非管线 kind 的生命周期**（2026-10-02 架构评审 P1-1）：`sw_valuation / analyst_rank /
> sw_component` 不在 daily 抓取链上，更新节奏由人决定。其水位在 `ark.py status` 里以
> `L0x_` 前缀展示（`L0x_analyst_rank / L0x_sw_component / L0x_sw_valuation`），
> **不参与「各层同日」断言**。对应实验结论：估值/基本面（已否决）、分析师预期（已放弃）、
> 拥挤度（条件化 IC 有效但组合层否决）——详见 2026-09-29 两份实验文档与登记簿。

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

`history.json` / `today.json` / `day/<date>.json` / `review.json` / `sim.json`。
由 `pipeline.py --all` 从 L2 全量重放生成；`site/data/` 是它的部署副本。
**可以整目录删除**，重跑 pipeline 即可恢复。

`review.json` 顶层的 `rules` 块（含 `layers` 分层总览）由 `engine/rules.py` 直接
从 `config.py` 与磁盘现状导出——页面上的参数、规则、分层数字都不允许前端硬编码。

## 展示层 `web/public/view/`（不在依赖链上）

**标的走势核验弹窗**的数据：`view/etf/<sid>.json`（`dates` / `raw` / `adj` / `breaks`）、
`view/etf/_breaks.json`（全池折算断点清单）、`view/etf/_index.json`（代码 → sid 索引
+ 日期轴末端 `last`）。`site/view/` 是部署副本，前端以 `view/etf/…` 相对路径取数。

**它为什么不是 L4、也不该叫某一层数据**：

1. 它**不持有任何自己的算法**。历史上它自己调 `qfq.apply_qfq()` 复权，于是仓库里
   存在两份复权实现：结算层走 `pipeline.load_etf_book()`（尊重 `config.ETF_QFQ`
   与 `ARK_QFQ=0`），展示层直接调 qfq（无视开关）。参照或开关一变，同一个持仓就在
   页面上和弹窗里显示成两个市值（实测差 4.2%，见 `docs/我的持仓-标的弹窗口径.md`）。
   现在改为**只消费** `load_etf_book(with_report=True)` 的结果——`adj` 与 `breaks`
   与结算层同一次调用，红线（弹窗 ≡ 结算取价）从"需要校验"变成"结构上不可能违反"。
2. 它**不在重建链上**：数据层推不动它，改一层不会自动改它，因此必须显式重建。
   漏跑的症状很隐蔽——账户已经盯市到最新交易日，点开 ETF 却显示涨跌停在两天前
   （2026-09-24 实际发生过一次）。现在 `ark.py daily` / `init` 都会跑它，
   且排在**管线之前**（它只依赖 `cache/etf_panel.json`），这样管线写 `review.json`
   时展示层已是最新，引擎页上的规模数字不会比真数据旧一天。
3. 水位上它登记为 **`VIEW_etf_modal`**，语义是「展示层陈旧守卫」而不是数据层水位：
   `ark.py status` / `verify` 靠 `_index.json` 的 `last` 让「各层应当同日」把它一起
   罩住，不一致直接失败。`site/view` 也参与 `site_synced` 检查。
4. `ark.py verify` 里保留 `_etf_webdata_vs_settlement()`：对 `sim.json` 的每只结算标的
   抽查最近 3 个有价日，`view/etf/<sid>.json` 的 `adj` 必须与 `px.c` 逐位相同。
   现在它是**回归断言**——拦的是"以后有人给展示层加回一套自己的复权实现"。

迁移验收（2026-09-24）：新旧产物 **125/125 个文件逐字节相同**，与 `sim.json` 的
`px.c` 逐位一致（0 处不一致）——即"改了结构、没改数值"。

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

# 手工分步（等价于 daily；注意展示层排在管线之前）
cd engine && akshare-venv-python datasource_sw.py --refresh   # L0+L1 面板
akshare-venv-python build_etf_panel.py --refresh              # L1 ETF 面板（--refresh 不能省）
akshare-venv-python build_etf_webdata.py                      # 展示层：标的走势切片（只依赖上面的 ETF 面板）
akshare-venv-python pipeline.py --refresh --all --tail 12     # L2+L3 增量
python3 engine/ark.py sync                                    # web/public/{data,view} → site/{data,view}

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
