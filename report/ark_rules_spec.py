# -*- coding: utf-8 -*-
"""
混沌方舟 ARK —— 买入 / 卖出判定规则规格书（供交叉验证）

用途
----
本文件不是生产代码，也不参与运行。它是把生产引擎里的判定逻辑**逐条抽出**、
写成「同一件事只有一种写法」的规格，供另一个 Agent 独立复现并交叉验证。

对应生产源码（行号锚点，均相对 ark/engine/ 目录）：
    config.py        : 全部参数常量
    indicators.py    : 指标定义（输入层）
    feature_store.py : 特征库 / 申万一级等权指数（EW）构造
    regime.py        : 环境识别（Step 1）
    scoring.py       : 六维打分 + 综合分（Step 2-3）
    horizon.py       : 建议持有周期估计（Step 5）
    classify.py      : 买入判定（Step 4，核心）
    settlement.py    : 建仓执行 + 卖出判定（结算层，核心）
    pipeline.py      : 串联（Step 6）
    review.py        : 评估口径

阅读顺序建议：§0 架构 -> §1 指标 -> §2 打分 -> §3 环境 -> §4 周期
            -> §5 买入判定 -> §6 建仓执行 -> §7 卖出判定 -> §8 评估口径 -> §9 验收锚点

【关键提醒】凡是标了 "★口径陷阱" 的地方，都是历史上真实踩过、
会让两份实现跑出不同数字的地方。交叉验证请优先核对这几处。
"""

import numpy as np  # noqa: F401  （仅供 §6 的 sigma 参考实现使用）

# ============================================================================
# §0  架构：两层分离（一切规则都挂在这条分界上）
# ============================================================================
#
#   信号层（决定「买什么 / 什么时候买 / 什么时候作废」）
#       输入：申万一级行业指数(31 个) + 细分赛道指数(L2)
#       输出：BUY_NOW(入手) / AMBUSH(埋伏) / WATCH(观察) / AVOID(回避)
#       用途：方向判断本身的有效性（信号口径胜率）
#
#   结算层（决定「能不能赚到钱」）
#       输入：真实可交易日行业 ETF 的日线 OHLCV
#       输出：资金曲线 / 年化 / 最大回撤（结算口径胜率）
#       规则：行业指数只做方向，收益一律用 ETF 结算；ETF 上市前该行业不可交易，
#             直接跳过，绝不用指数收益回填（settle_trade 返回 None）。
#
#   两层的连接点：一条信号结论 -> (行业名, 信号日 T, 建议周期 H) -> 一笔 ETF 交易。
#   不变量：结算层不得修改信号层的任何判定；信号层不得使用 ETF 行情。


# ============================================================================
# §1  输入指标定义（indicators.py，逐字公式）
# ============================================================================
# 单一标的按日期升序的日线：close / open / high / low / volume / amount
# 以下全部为「滚动窗口，只用截至当日的数据」，不存在前视。

INDICATOR_DEFS = r"""
ret_k        = close.pct_change(k)                    # k ∈ {5,10,20,60,120,250}
ma_k         = close.rolling(k).mean()                # k ∈ {5,10,20,60,120,250}
ma20_slope   = ma20 / ma20.shift(5) - 1.0             # MA20 五日斜率
dist_ma_k    = close / ma_k - 1.0                     # k ∈ {20,60,120,250}

hi60,  lo60  = close.rolling(60).max(),  min
hi250, lo250 = close.rolling(250).max(), min
dd_from_high60 = close / hi60 - 1.0                   # ∈ (-1, 0]
pos60          = (close - lo60) / (hi60 - lo60)       # ∈ [0,1]，近60日区间位置分位
pos250         = (close - lo250) / (hi250 - lo250)

vol20 = ret_1.rolling(20).std() * sqrt(252)           # 年化波动
vol60 = ret_1.rolling(60).std() * sqrt(252)
maxdd_60 = min_{i in [t-59, t]}( close[i] / cummax(close[t-59..i]) - 1 )

vma5,  vma20  = volume.rolling(5).mean(), volume.rolling(20).mean()
ama5,  ama20  = amount.rolling(5).mean(), amount.rolling(20).mean()
vol_ratio     = vma5 / vma20                          # 5/20 日量比
amount_ratio  = ama5 / ama20                          # 5/20 日额比

up_v = (ret_1 > 0) * volume ;  dn_v = (ret_1 < 0) * volume
up_vol_share = up_v.rolling(10).sum() / (up_v+dn_v).rolling(10).sum()

consec_up = 连续上涨天数（严格 close[i] > close[i-1] 才 +1，否则归零）
sharpe20  = ret_20 / vol20
"""

# 相对强度：相对「申万一级等权指数 EW」，不是相对沪深300。★口径陷阱
# EW 构造（feature_store.py:142-166）：逐日取全部 L1 行业当日收益率的**算术平均** m，
#   再 prev *= (1+m)；dates[0] = 1.0。等价于「每日再平衡的等权组合」。
RS_DEF = r"""
EW_ret_k     = EW.close[t] / EW.close[t-k] - 1.0
rs_k         = ret_k(标的) - EW_ret_k       # k ∈ {20,60,120}
"""
# ★注意：pos60 / rs_120 / dist_ma250 三者分别用于「追高过滤」「自相矛盾过滤」「长期超涨回避」，
#   交叉验证时务必与上表逐字一致；差一个 .shift() 就会让过滤集合变化。


# ============================================================================
# §2  六维打分（scoring.py）
# ============================================================================
# 全部维度 0-100，横截面可比，NaN -> 50。
# norm(x, lo, hi) = clip((x - lo) / (hi - lo) * 100, 0, 100)，x 为 NaN 时返回 50.0
# 注意 norm 是「越大越好」的统一方向：要让某个量越小得分越高，就传它的相反数。

def norm(x: float, lo: float, hi: float) -> float:
    """indicators.norm_score"""
    if x is None or x != x:
        return 50.0
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def dims_of(c: dict) -> dict:
    """scoring.compute_dimension_scores（单标的单日）

    c: 该标的当日的 {ret5, ret20, ret60, ret120, ret250, dist20, dist120, dist250,
                     vol_ratio, amount_ratio, up_vol_share, vol20, dd_from_high60,
                     ma20_slope, rs_120, pos60 ...}
    """
    # ---- 1. mainline：长期主线强度（权重最高的因子）
    mainline = (0.40 * norm(c["ret120"], -0.10, 0.50)
                + 0.25 * norm(c["ret250"], -0.15, 0.80)
                + 0.20 * norm(c["dist120"], -0.10, 0.30)
                + 0.15 * norm(c["dist250"], -0.15, 0.40))

    # 主线强度闸门：0.45 ~ 1.00，用于压制「弱主线里的回调/触底」信号
    ml_gate = 0.45 + 0.55 * mainline / 100.0

    # ---- 2. pullback：短期回调深度（条件化：只在主线内成立）
    pullback = (0.40 * norm(-c["ret20"], -0.20, 0.15)
                + 0.30 * norm(-c["dd_from_high60"], 0.0, 0.30)
                + 0.30 * norm(-c["dist20"], -0.15, 0.10)) * ml_gate

    # ---- 3. exhaust：量能枯竭 / 抛压衰竭
    exhaust = (0.50 * norm(-c["vol_ratio"], -1.80, -0.60)
               + 0.30 * norm(-c["amount_ratio"], -1.80, -0.60)
               + 0.20 * norm(0.55 - c["up_vol_share"], -0.25, 0.25))

    # ---- 4. rebound：触底确认（跌势减缓 + 短期转正 + MA20 拐头）
    rebound = (0.50 * norm(c["ret5"] - c["ret20"] * 0.25, -0.05, 0.08)
               + 0.30 * norm(c["ret5"], -0.08, 0.10)
               + 0.20 * norm(c["ma20_slope"], -0.04, 0.06)) \
              * (0.40 + 0.60 * mainline / 100.0)      # 同样按主线强度压制

    # ---- 5. risk：波动与量能极值控制（预测力弱，只用于排除极端）
    risk = (0.70 * norm(0.45 - c["vol20"], -0.25, 0.35)
            + 0.30 * norm(1.60 - c["vol_ratio"], -0.40, 1.00))

    return {"mainline": mainline, "pullback": pullback, "exhaust": exhaust,
            "rebound": rebound, "risk": risk}


def regime_fit(regime: str, d: dict) -> float:
    """scoring.compute_regime_fit —— 第六维，随环境切换配方"""
    ml, pb, ex, rb, rk = (d["mainline"], d["pullback"], d["exhaust"],
                          d["rebound"], d["risk"])
    if regime == "ATTACK":
        return 0.60 * ml + 0.40 * rb
    if regime == "DEFEND":
        return 0.50 * ml + 0.30 * rk + 0.20 * pb
    if regime == "PANIC":
        return 0.60 * rk + 0.40 * ml
    return 0.40 * ml + 0.30 * pb + 0.30 * rb           # NEUTRAL 兜底


BASE_WEIGHTS = {"mainline": 0.34, "pullback": 0.18, "exhaust": 0.12,
                "rebound": 0.14, "risk": 0.10, "regime_fit": 0.12}

REGIME_WEIGHT_ADJ = {
    "ATTACK":  {"mainline": 1.15, "pullback": 0.85, "exhaust": 0.90, "rebound": 1.20, "risk": 0.90, "regime_fit": 1.00},
    "NEUTRAL": {"mainline": 1.00, "pullback": 1.00, "exhaust": 1.00, "rebound": 1.00, "risk": 1.00, "regime_fit": 1.00},
    "DEFEND":  {"mainline": 1.00, "pullback": 1.15, "exhaust": 1.20, "rebound": 0.90, "risk": 1.30, "regime_fit": 1.20},
    "PANIC":   {"mainline": 0.85, "pullback": 1.20, "exhaust": 1.25, "rebound": 0.80, "risk": 1.50, "regime_fit": 1.40},
}


def composite_of(regime: str, dims: dict) -> float:
    """scoring.compute_regime_fit —— 综合分（0-100）

    综合分 = Σ_k( base_k * adj_k * dim_k ) / Σ_k( base_k * adj_k )
    ★注意分母是「加权后的权重之和」，不是 1。NaN 维度按 50 参与计算。
    """
    fit = regime_fit(regime, dims)
    dd = dict(dims, regime_fit=fit)
    adj = REGIME_WEIGHT_ADJ.get(regime, REGIME_WEIGHT_ADJ["NEUTRAL"])
    raw = {k: BASE_WEIGHTS[k] * adj.get(k, 1.0) for k in BASE_WEIGHTS}
    tot = sum(raw.values())
    val = 0.0
    for k, w in raw.items():
        v = dd.get(k)
        v = 50.0 if (v is None or v != v) else v
        val += w * v
    return val / tot


# 候选池（feature_store.investable）
def investable(fs, date: str) -> list:
    """当日可打分标的：level ∈ {L1, L2} 且 ret_60 非 NaN（= 有足够历史）。"""
    return [c for c in fs.codes
            if fs.meta[c].get("level") in ("L1", "L2")
            and fs.data[c]["ret_60"][fs.idx(date)] == fs.data[c]["ret_60"][fs.idx(date)]]


# ============================================================================
# §3  环境识别（regime.py，Step 1 —— 影响维度权重与 regime_fit）
# ============================================================================
REGIME_RULES = {"bench_dd_panic": -0.12, "vol_pct_panic": 0.92}
REGIME_THRESHOLDS = {"ATTACK": 62.0, "NEUTRAL": 46.0, "DEFEND": 32.0}   # 低于 32 = PANIC
EMA_ALPHA = 0.4          # 3 日 EMA 平滑
HYSTERESIS_DAYS = 2      # 需连续 2 日越过阈值才切换档位

REGIME_SPEC = r"""
逐日计算（基准 = PRIMARY_BENCH = 沪深300 "1.000300"；L1 = 31 个申万一级行业）：

  breadth20 = (#{L1 行业当日 ret_20 > 0}) / (#{L1 行业 ret_20 有效})
  median20  = median(L1 行业的 ret_20)
  vol_pct   = 基准 vol20 在过去 250 期（含当日）中的分位

  p_breadth  = breadth20 * 100
  p_trend    = 0.6*norm(bench_dist_ma20, -0.05, 0.05) + 0.4*norm(bench_ma20_slope, -0.03, 0.03)
  p_momentum = norm(median20, -0.08, 0.12)
  p_vol      = (1 - vol_pct) * 100
  liq_ratio  = (全 L1 成交额 5 日均) / (全 L1 成交额 20 日均)
  p_liq      = norm(liq_ratio, 0.70, 1.30)

  raw_score  = 0.32*p_breadth + 0.24*p_trend + 0.24*p_momentum + 0.10*p_vol + 0.10*p_liq
  smooth     = EMA(raw_score, alpha=0.4)

  档位（对 smooth 取值）：>=62 ATTACK ; >=46 NEUTRAL ; >=32 DEFEND ; else PANIC
  滞回：与当前档位不同时，需**连续 2 日**都指向新档位才切换
  强制覆盖（优先级高于滞回）：
      bench_dd60 < -0.12                    -> PANIC（并写回 cur）
      vol_pct > 0.92 且 cur ∈ {ATTACK,NEUTRAL} -> DEFEND
  confidence = clip(1 - std(5 个分项)/45, 0.35, 0.95)
"""


# ============================================================================
# §4  建议持有周期估计（horizon.py，Step 5）
# ============================================================================
HORIZON_CANDIDATES = [60, 80, 100]   # 只在这三个里选
DEFAULT_HORIZON = 20                 # 样本不足时的回退值
K_NEIGHBORS = 40
MIN_SAMPLES = 12
DD_PENALTY = 0.25                    # risk_adj = mean_excess - 0.25*|mean_maxdd|

HORIZON_SPEC = r"""
状态向量 z = [mainline, pullback, exhaust, rebound, risk, env_score] / 100   （6 维）

候选历史样本集：该标的**自身历史**中「未来窗口完整」的日子
                即 hist = state[: i - max(HORIZON_CANDIDATES) - 1] = state[: i-101]
取与当前 z 的欧氏距离最近的 K=40 个样本。

对每个 H ∈ {60, 80, 100}：
    mean_excess = mean( fwd_ex_ew[sample, H] )      # 相对行业等权(EW)的超额
    mean_maxdd  = mean( fwd_dd[sample, H] )         # 未来 1..H 日最低 / 当日 - 1
    risk_adj    = mean_excess - 0.25 * abs(mean_maxdd)
    样本 < 12 个有效值的 H 直接跳过

选 risk_adj 最大的 H；若全部不足 12 个样本 -> 回退 H=20，picked_by="default"
low_confidence = (选中 H 的有效样本数 < 25)

★陷阱：候选只有 60/80/100，但回退值是 20。所以真实结果里会出现 H=20 的结论，
  这不是 bug，是「样本不足」的显式标记。不要为了「看起来一致」把 20 从结果里删掉。
"""


# ============================================================================
# §5  买入判定（classify.py —— 本文件最关键的一节）
# ============================================================================
CLASSIFY = {
    "buy_now_min": 64.0,     # 入手：综合分门槛
    "ambush_min": 56.0,      # 埋伏：综合分门槛
    "avoid_crowd_pct": 0.90, # (定义保留，当前未在判定中使用)
    "avoid_dd": -0.18,       # 失效条件里的回撤阈值
    "max_daily_picks": 8,    # 每日最多输出结论数
    "max_buy_now": 4,        # 每日最多「入手」数
}
CONCLUSION_FILTER = {"rs120_min": 0.0, "pos60_max": 0.95}

# 硬回避门槛（三条，任一命中即为「回避」倾向）
AVOID_RULES = {
    "mainline_lt": 30.0,      # mainline < 30   : 长期趋势处于下降通道
    "dist_ma250_gt": 0.45,    # dist_ma250 > 0.45 : 价格高于 MA250 45% 以上，长期超涨
    "risk_lt": 22.0,          # risk < 22        : 波动/量能处于极端区间
}

# 买入动作门槛
BUY_NOW_RULE = {"composite": 64.0, "mainline": 58.0, "rebound": 55.0, "risk": 35.0}
AMBUSH_RULE = {"composite": 56.0, "mainline": 52.0, "pullback": 52.0, "exhaust": 50.0}


def buy_verdict(c: dict, dims: dict, comp: float) -> dict:
    """classify.classify_day —— 单标的的买入判定（顺序不可调换）

    返回 {"action": "AVOID"|"BUY_NOW"|"AMBUSH"|"WATCH", "avoid_why": [...]}
    """
    # ---------- 第 1 关：硬回避（classify.py:173-191）----------
    is_avoid, avoid_why = False, []
    if dims["mainline"] < AVOID_RULES["mainline_lt"]:
        is_avoid = True
        avoid_why.append("长期趋势处于下降通道（120/250 日动量为负），反弹不参与")
    if c["dist_ma250"] is not None and c["dist_ma250"] > AVOID_RULES["dist_ma250_gt"]:
        is_avoid = True
        avoid_why.append("价格高于 MA250 45%，长期超涨")
    if dims["risk"] < AVOID_RULES["risk_lt"]:
        is_avoid = True
        avoid_why.append("波动/量能处于极端区间，风险收益比恶化")

    # ★陷阱：必须是 is_avoid **且** composite >= 50 才提前返回 AVOID；
    #   只满足 is_avoid 而 composite < 50 的，代码会继续往下走判定（结果必然落到 WATCH，
    #   因为分数达不到 64/56 门槛，但控制流不同，交叉验证请照此实现）。
    if is_avoid and comp >= 50:
        return {"action": "AVOID", "avoid_why": avoid_why}

    # ---------- 第 2 关：入手 / 埋伏（classify.py:193-210）----------
    if (comp >= BUY_NOW_RULE["composite"]
            and dims["mainline"] >= BUY_NOW_RULE["mainline"]
            and dims["rebound"] >= BUY_NOW_RULE["rebound"]
            and dims["risk"] >= BUY_NOW_RULE["risk"]):
        return {"action": "BUY_NOW", "avoid_why": avoid_why,
                "trigger": "长期主线成立 + 短期触底确认：主线趋势未破，回调后动能重新转强"}

    if (comp >= AMBUSH_RULE["composite"]
            and dims["mainline"] >= AMBUSH_RULE["mainline"]
            and dims["pullback"] >= AMBUSH_RULE["pullback"]
            and dims["exhaust"] >= AMBUSH_RULE["exhaust"]):
        return {"action": "AMBUSH", "avoid_why": avoid_why,
                "trigger": "主线行业回调中 + 抛压衰竭：长期趋势仍在，等待缩量企稳后的反弹"}

    return {"action": "WATCH", "avoid_why": avoid_why}


def select_and_filter(rows: list, filt: dict = CONCLUSION_FILTER) -> dict:
    """classify.classify_day —— Top 选取 + 结论前置过滤

    rows: 候选池全部标的的 [{"code","composite","action","rs_120","pos60","dims"...}]，
          须已按 composite 降序排列。
    返回 {"picks": [...], "n_filtered": int}
    """
    # ---------- Top 选取：入手优先，埋伏补足（classify.py:212-215）----------
    # ★顺序：先按 composite 降序切 BUY_NOW 前 4，再切 AMBUSH 前 (8 - 入手数)。
    #   所以当入手满 4 个时，埋伏一个都不留；入手 0 个时，埋伏最多 8 个。
    buys = [r for r in rows if r["action"] == "BUY_NOW"][:CLASSIFY["max_buy_now"]]
    amb = [r for r in rows if r["action"] == "AMBUSH"][:CLASSIFY["max_daily_picks"] - len(buys)]
    final = buys + amb

    # ★注意：final 不是全局按分数排序 —— 所有 BUY_NOW 一定排在所有 AMBUSH 前面。

    # ---------- 结论前置过滤（classify.py:217-245）----------
    # ★陷阱：过滤发生在 Top 选取**之后**，被降级的结论**不会补位**——
    #   当日结论数可能从 8 变成 6，这是设计意图，不是漏实现。
    kept, demoted = [], []
    for p in final:
        why = []
        if filt.get("rs120_min") is not None and p["rs120"] is not None and p["rs120"] < filt["rs120_min"]:
            why.append("120日相对行业等权超额为负 —— 与结论自带的失效条件冲突，事前即作废")
        if filt.get("pos60_max") is not None and p["pos60"] is not None and p["pos60"] > filt["pos60_max"]:
            why.append("价格位于近60日区间顶部（位置分位 > 95%）—— 属追高")
        if why:
            p = dict(p, action="AVOID", filtered=True, why=why)
            demoted.append(p)
        else:
            kept.append(p)
    return {"picks": kept, "n_filtered": len(demoted), "demoted": demoted}


def exit_conditions_text(c: dict, action: str) -> list:
    """classify.build_invalidation —— 结论自带的失效条件（事前写死，非事后拟合）

    这四条是卖出规则的**文本依据**：结算层的主口径不执行它们（只作为「失效预警」展示），
    对照口径 B 执行其中的第 (1) 条（跌破 MA20，见 §7）。
    """
    d120 = c["dist_ma120"]
    inv = []
    # (1) 跌破 MA120 —— 对应结算层的 MA20 早退（口径 B 的触发条件）
    inv.append(f"跌破 MA120（当前偏离 {d120*100:+.1f}%）——主线破坏，结论作废"
               if d120 is not None else "跌破 MA120——主线破坏，结论作废")
    # (2) 相对强度转负 —— 已升级为事前过滤（CONCLUSION_FILTER.rs120_min = 0.0）
    inv.append(f"120日相对行业等权超额转负（当前 {c['rs_120']*100:+.2f}%）")
    # (3) 回撤扩大 + 放量下跌
    inv.append(f"自60日高点回撤扩大到 -18% 以上（当前 {c['dd_from_high60']*100:+.2f}%）且量比 > 1.5（放量下跌）")
    # (4) 动作相关
    if action == "AMBUSH":
        inv.append("埋伏后 15 个交易日内触底分仍低于 45（判定为下跌中继而非回调）")
    else:
        inv.append("触底分回落至 40 以下且 5 日收益转负（反弹证伪）")
    return inv


# ============================================================================
# §6  建仓执行（settlement.simulate_etf_portfolio）
# ============================================================================
PORTFOLIO = {"score_tilt": 0.5, "max_new_per_day": 2}
RISK_CONTROL = {"risk_parity": True, "rp_window": 60,
                "vol_target": 0.16, "vol_window": 60, "vol_floor": 0.80,
                "cash_yield": 0.015}
MAX_HOLDINGS = CLASSIFY["max_daily_picks"] * 4      # = 32
TRADING_COST = {"commission": 0.0002, "stamp": 0.0, "slippage": 0.0005}
ROUND_TRIP_COST = 2 * (TRADING_COST["commission"] + TRADING_COST["slippage"])   # = 0.0014
MAX_PER_SID = None          # 未启用（实验结论：非 Pareto 改进）

ENTRY_SPEC = r"""
【输入】trades = 全部「可结算」的信号结论，每条含：
        date(信号日 T) / sw_name(行业名) / horizon(建议周期 H) / composite / action
        / sell_date(单笔结算给出的实际卖出日，可为空) / etf_sid(买入日选中的 ETF)

【建仓计划】(settlement.py:421-449)
  b_i = idx(date) + 1                        # T+1 开盘买入
  s_i_ = idx(sell_date) if sell_date else idx(date) + 1 + H
  跳过条件（任一命中即不进计划，且**不计数**）：
      idx(date) 不存在，或 idx(date) < start_i
      b_i >= len(dates)，或 s_i_ >= len(dates)，或 s_i_ <= b_i
  sid0 = etf_sid 或 etf_of(sw_name, dates[b_i])；为 None -> skipped_untradable + 1，跳过
  market_gate=True 且 gate[b_i] 为假 -> blocked_by_gate + 1，跳过（生产未启用）
  plan[b_i].append({sw, end=s_i_, secid=sid0, score=composite, horizon, action, reason})

【逐日循环】k = max(start_i, 1) .. len(dates)-1
  (1) 结算与到期判定
      对每个持仓 h：c = close(h.secid, k)
        权重： w = 1/max(σ(h.secid, k, 60), 0.05)   if risk_parity else 1.0
               if score_tilt:  w *= (max(score,1.0) / 50.0) ** score_tilt   ★见下方陷阱
        当日收益：若 c 与昨日收盘 p 都有值 -> day_rets.append((c/p - 1, w))
        续持： h["end"] >  k   -> 保留（深拷贝）
        到期： h["end"] <= k   -> 今日收盘卖出，记 S 事件，从持仓移除
  (2) 新开仓（仅当 k 在 plan 中）
        adds = sorted(plan[k], key=-score)              # 高分优先
        if max_new_per_day: adds = adds[:2]             # ★此截断不计数
        room = max(0, 32 - len(holdings))               # ★room 用「到期剔除后」的持仓数
        blocked_by_capacity += max(0, len(adds) - room) # ★只是计数，非精确差额
        for item in adds[:room]:
            sid = item.secid 或 etf_of(item.sw, dates[k])；为空 -> 跳过
            if max_per_sid 且 count(holdings.sid == sid) >= max_per_sid -> blocked_by_sid_cap+1, 跳过
            o = open_(sid, k)；为空 -> 跳过
            σ = σ(sid, k, 60) if risk_parity else 0.25
            w = (1/max(σ,0.05) if risk_parity else 1.0)
            if score_tilt: w *= (max(item.score,1.0)/50.0) ** score_tilt
            holdings.append(...)；n_executed += 1；记 B 事件
            当日收益： (close(sid,k)/o - 1, w)
  (3) 组合当日收益
        r_raw = Σ(r_i * w_i) / Σ(w_i)                  # ★等权归一到 1
        expo  = clip(vol_target / σ_port, vol_floor, 1.0)  if vol_target 且 len(ret_hist) >= 20  else 1.0
                σ_port = std(最近 vol_window=60 日的组合日收益) * sqrt(252)
        r = expo * r_raw + (1 - expo) * (0.015/252)     # 未投入部分计现金收益
        nav *= (1 + r)  ； nav_dates.append(d)
  (4) 事件锚定：当日全部 B/S 事件的 i = len(nav) - 1（净值点下标）

【初始条件】
  nav = [1.0]，nav_dates = [dates[max(start_i,1) - 1]]  ★首点是「起始基准日」，比净值多一个
  holdings = []，prev_close_cache = {}
  一个仓位都没有的交易日，组合日收益 = 现金收益的部分
"""

def sigma(book, sid: str, k: int, window: int = 60) -> float:
    """settlement._sigma —— 标的截至 k-1 日的年化波动（无前视）

    取值区间是 [k-window, k)，**不含当日 k**。
    有效点 < 11 或 σ <= 1e-4 时返回 0.25（先验波动）。
    """
    vals = [book.close(sid, j) for j in range(max(0, k - window), k)]
    vals = [v for v in vals if v]
    if len(vals) < 11:
        return 0.25
    rr = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))]
    sig = float(np.std(rr) * (252 ** 0.5))
    return sig if sig > 1e-4 else 0.25


WEIGHT_TRAP = r"""
★口径陷阱（本规格里最容易出错的一条）

正确： w *= (max(score, 1.0) / 50.0) ** score_tilt
错误： w *= max(score, 1.0) ** score_tilt / 50.0

差别：正确式以「50 分」为归一点，score=50 的权重系数恰为 1.0；
      错误式是「常数分母」，等于给所有高分结论加了隐性杠杆，会虚报年化（实测虚高约 1pp）。

另有两处必须同时满足，否则只在 score_tilt > 0 的用例上产生 0.04pp 级别的偏差：
  (a) 建仓当日就要乘上 tilt 系数（不能留到次日循环再乘）；
  (b) 续持权重的分母同样用 50.0 ** score_tilt。
"""


# ============================================================================
# §7  卖出判定（settlement.settle_trade / open_positions）
# ============================================================================
EXIT_MA = 20          # 早退参考均线
EXIT_CONFIRM = 3      # 需连续 3 个交易日收盘跌破才退出

EXIT_SPEC = r"""
【主口径 —— 生产资金曲线用的就是这一条】
  买入： T+1 开盘价（T = 信号日）
  卖出： T+1+H 收盘价（H = 建议周期）
  卖出日无行情：从 s_i 向前找最近有成交的日期平仓，s_i 改写为该日
  成本： 双边 ROUND_TRIP_COST = 0.0014
  单笔净收益 net = (sell/buy - 1) - ROUND_TRIP_COST
  退出原因：持有到期 / 到期日无行情顺延

【对照口径 B —— early_exit=True，仅在复盘页作对照展示，不进生产曲线】
  在区间 k ∈ [b_i+1, s_i] 上逐日检查（s_i 为上面顺延后的卖出日）：
      连续跌破计数器 below： close(sid,k) < MA20(sid,k) 则 below += 1，否则 below = 0
      一旦 below >= 3：
          在 k+1 日**开盘价**平仓；若 k+1 无开盘价，退而用当日 k 收盘价
          标记 exited_early = True，退出原因 =「失效条件触发：连续 3 日收盘跌破 MA20，次日开盘平仓」
          break
  MA20 取标的自身均线：窗口 [k-19, k] 共 20 个点，有效点 < 10 视为无均线（不计数）

  ★口径陷阱：单日跌破不做任何处理。曾试过「跌破即退」，实测该规则会 100% 触发、
    平均只持有 7 天，把有效持仓全部洗掉 —— 所以必须是「连续 3 日确认」。

【结论层失效条件（文本，§5 第 4 项）与上面两条的关系】
  主口径   : 不执行任何失效条件，持有到周期结束
  口径 B   : 只执行第 (1) 条（跌破 MA20 的日线近似），且加了 3 日确认
  失效预警 : 「今日出手」模块按口径 B 的判定给出「连续 N 日收盘跌破 MA20」提示，
             属于风险提示，主口径不会因此卖出

【「今日出手」重建（open_positions）—— 与主口径的差异点】
  目的：重建「截至 as_of 仍在场」的持仓（允许持有期未走完的结论进入）。
  b_i = idx(date)+1（只保留 b_i <= as_of 的）
  e_i = b_i + H   ★**不做数据末端截断**：截断会把「周期未走完」误判成「今日到期」
  到期判定：h["end"] <= k 则移除；若同时 k == as_of，计入 sell_today（今日该出手）
  容量：按 score 降序，len(holdings) >= 32 时停止建仓
  due_in = max(0, e_i - as_i)   ★纯计数，不依赖未来价格，即使 e_i 超出数据末端也照算
  end_date = None if e_i >= len(dates) else dates[e_i]   ★只有日历日才置空
  失效判定：从 as_of 向前回溯最多 EXIT_CONFIRM*3 = 9 天，累计「收盘 < MA20」的**连续**天数，
            达到 3 天即 invalid=True（注意窗口只有 9 天，与口径 B 的全周期窗口不同）
"""


# ============================================================================
# §8  评估口径（review.py）—— 交叉验证的「正确答案」怎么数出来
# ============================================================================
REVIEW = {"outperf_win": 0.02, "outperf_loss": -0.03}

EVAL_SPEC = r"""
两个口径严格分离，绝不混用：

【信号口径】衡量「方向判断本身」
  区间收益： 指数 close[i+H] / close[i] - 1        （i = 信号日，用行业指数）
  超额：     ex_ew = 区间收益 - 等权指数 EW 同期收益
  判定：     ex_ew >= +2% -> RIGHT ; ex_ew <= -3% -> WRONG ; 否则 NEUTRAL
  方向胜率（overview.win_rate）= RIGHT 数 / 已评估总数（含 NEUTRAL 在分母）

【结算口径】衡量「能不能赚到钱」
  区间收益： ETF 开盘买 / 收盘卖 - 1 - ROUND_TRIP_COST
  超额：     ex_bench = ETF 净收益 - 基准 ETF(510300) 同期净收益
  判定：     同样用 +2% / -3% 双阈值
  胜率：     win_rate_excess = #{ex_bench >= 2%} / 可结算总数
             win_rate_abs    = #{net_ret > 0}   / 可结算总数

【可结算性】ETF 上市日之后、且买卖两端都有行情，才算 settled；
            否则该结论只进信号口径（n_signal_only），不进任何资金曲线。
"""


# ============================================================================
# §9  验收锚点（当前生产配置下，全量区间 2016-01-04 ~ 2026-09-21）
# ============================================================================
# 交叉验证时，如果对方复现出的数字与下表一致，说明规则实现到位。
# 数据源：site/data/review.json（engine_version = ark-1.1.0-etf）
EXPECTED = {
    "区间": {"start": "2016-01-04", "end": "2026-09-21",
             "nav_dates_first": "2015-12-31", "nav_len": 2606, "years": 10.34},

    "信号层": {"evaluated": 1449, "right": 607, "wrong": 588, "neutral": 254,
               "win_rate": 0.4189, "avg_excess": 0.0219},

    "结算层": {"n_settled": 1032, "n_signal_only": 417, "n_total": 1449,
               "win_rate_excess": 0.3934, "win_rate_abs": 0.4961,
               "avg_ret": 0.0363, "avg_excess": 0.0215},

    "组合曲线": {"cagr": 0.1257, "maxdd": -0.2991, "maxdd_date": "2022-04-26",
                 "total_return": 2.4022, "vol": 0.1949, "sharpe": 0.705,
                 "calmar": 0.42, "bench_cagr": 0.0416, "bench_maxdd": -0.4475,
                 "excess_cagr": 0.0841},

    "组合执行": {"planned_trades": 1032, "executed_trades": 328,
                 "blocked_by_capacity": 347, "blocked_by_sid_cap": 0,
                 "max_holdings": 32, "avg_holdings": 10.19,
                 "avg_exposure": 0.902, "avg_invested": 0.262,
                 "skipped_untradable": 0, "distinct_etf": 25,
                 "n_B_events": 328, "n_S_events": 328},

    "平均持仓": {"组合口径_avg_days": 80.9, "组合口径_median_days": 80.0,
                 "信号口径_avg_days": 78.8,
                 "口径B_avg_days": 10.2, "口径B_exit_share": 0.99},

    "前置过滤": {"n_demoted": 328},
}

# ★执行计数的口径说明（最容易对不上的一处）：
#   planned(1032) - executed(328) = 704，但 blocked_by_capacity 只有 347。
#   差额来源：
#     - max_new_per_day=2 的截断**发生在容量计数之前**，被它削掉的部分不计数；
#     - adds[:room] 里仍可能有 sid 为空 / 开盘价缺失而 continue 的（同样不计数）。
#   所以 blocked_by_capacity 是「因容量被挡」的**下界近似**，不要试图用它做等式配平。
#   如需精确配平，请自行按 plan/execute 两条序列做集合差。

# 待验证/已知边界
KNOWN_LIMITS = [
    "min_days=20 的持仓来自「样本不足回退到 DEFAULT_HORIZON=20」，共 3 笔；",
    "plan 里 s_i_ >= len(dates) 的结论会被静默丢弃（不计数），这是「周期未走完」的正常剔除；",
    "n_settled(1032) 与 planned_trades(1032) 数值相同属巧合，两者定义不同；",
    "口径 B 的 cagr 6.70% / maxdd -40.17% 明显劣于主口径，这是它不进生产的原因；",
    "max_per_sid 已实现但未启用（实验结论：不构成 Pareto 改进）。",
]


# ============================================================================
# 附：交叉验证建议的检查顺序
# ============================================================================
CHECKLIST = [
    "1. 指标层：任取一个标的 + 一天，复算 pos60 / rs_120 / dist_ma250，与本文 §1 逐字一致？",
    "2. 打分层：复算该标的当日 mainline / pullback / rebound 三维，再复算 composite（注意分母是加权和）。",
    "3. 环境层：复算当日 env_score -> smooth -> 档位，含滞回与强制覆盖两个特例。",
    "4. 买入层：验证「is_avoid 且 composite>=50 才提前返回」这一控制流；",
    "   验证 Top 选取顺序（4 入手 + 最多 4 埋伏）与「过滤不补位」。",
    "5. 执行层：验证 w 的两处 score_tilt 写法（正确式 / (50.0**tilt)），以及建仓当日即生效；",
    "   验证 room 用的是「到期剔除后」的持仓数。",
    "6. 卖出层：验证主口径 T+1+H 收盘卖；对照口径 B 是「连续 3 日跌破 MA20 -> 次日开盘」。",
    "7. 计数层：executed=328 / n_B=328 / n_S=328 / distinct_etf=25 是否复现（或差异可解释）。",
    "8. 曲线层：cagr / maxdd / vol 是否落在 12.57% / -29.91% / 19.49% 附近。",
]
