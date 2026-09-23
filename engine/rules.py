# -*- coding: utf-8 -*-
"""ARK 生效规则快照 —— 从 config.py 直接导出「当前正在跑的交易规则与参数」。

为什么单独成模块
----------------
页面上展示的参数必须与引擎实际使用的**同一份来源**。若在前端硬编码，
config 一改页面就会静默过期——数字对不上代码，比不显示更危险。
因此本模块只做三件事：**引用（不新定义任何魔法数字）、组织、说明**。
所有数值都从 config.C 取，改 config 后页面自动同步。

产物：review.json 顶层 "rules" 键，供「引擎」页渲染。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import regime as RG  # noqa: E402      # 档位阈值 / 平滑系数（同一份来源）
import horizon as HZ  # noqa: E402     # 周期估计的 K / 最小样本


# --------------------------------------------------------------------- 格式化
def _num(x, nd: int = 2) -> str:
    """数字展示：整数不带小数点，小数去掉尾随零（0.50 → 0.5，64.0 → 64）。"""
    if x is None:
        return "—"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if x.is_integer():
            return str(int(x))
        s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(x)


def _pct(x, nd: int = 2, signed: bool = False) -> str:
    if x is None:
        return "—"
    s = f"{x*100:+.{nd}f}%" if signed else f"{x*100:.{nd}f}%"
    return s


def _item(label: str, display: str, src: str, why: str = "",
          kind: str = "num") -> dict:
    """一条参数。kind 仅用于前端着色：num / pct / text / code / on。"""
    return {"label": label, "display": display, "src": src, "why": why, "kind": kind}


def _group(key: str, title: str, intro: str, items: List[dict],
           badge: str = "") -> dict:
    return {"k": key, "title": title, "intro": intro, "items": items, "badge": badge}


# --------------------------------------------------------------------- 主逻辑
def build_rules(settlement: Optional[dict] = None,
                today: Optional[dict] = None) -> dict:
    """组装生效规则快照。settlement / today 仅用于填「本次运行实况」。"""
    st = settlement or {}
    cf = st.get("conclusion_filter") or {}
    q = C.CLASSIFY
    flt = C.CONCLUSION_FILTER
    rc = C.RISK_CONTROL
    po = C.PORTFOLIO
    hz = C.HORIZON_CANDIDATES
    cost = C.TRADING_COST
    cap = q["max_daily_picks"] * 4            # 与 pipeline 传入的 max_holdings 同源

    # ---------------------------------------------------- 1 买入判定
    buy_items = [
        _item("「入手」最低综合分", f"{_num(q['buy_now_min'])} 分",
              "config.CLASSIFY.buy_now_min",
              "综合分 ≥ 该值，且主线 ≥ 58、触底 ≥ 55、风险 ≥ 35 三者同时成立，才给「入手」。"),
        _item("「埋伏」最低综合分", f"{_num(q['ambush_min'])} 分",
              "config.CLASSIFY.ambush_min",
              "综合分 ≥ 该值，且主线 ≥ 52、回调 ≥ 52、衰竭 ≥ 50 三者同时成立，才给「埋伏」。"),
        _item("每日最多输出方向", f"{q['max_daily_picks']} 个",
              "config.CLASSIFY.max_daily_picks",
              "控制单日结论数量，避免信号密集日把资金摊薄到低分标的上。"),
        _item("其中「入手」上限", f"{q['max_buy_now']} 个",
              "config.CLASSIFY.max_buy_now",
              "入手优先占位，剩余名额由埋伏按综合分补足（不足则当日结论少于上限，不补位）。"),
        _item("硬回避：主线分", f"< {_num(30)} 分",
              "engine/classify.py · 硬回避（主线通道）",
              "长期趋势处于下降通道（120/250 日动量为负）——下跌趋势中的反弹不参与。"),
        _item("硬回避：高于 MA250", f"> {_pct(0.45, 2, True)}",
              "engine/classify.py · 硬回避（长期超涨）",
              "长期超涨。复盘显示「追高」是首要失误来源。"),
        _item("硬回避：风险分", f"< {_num(22)} 分",
              "engine/classify.py · 硬回避（风险极值）",
              "波动 / 量能处于极端区间，风险收益比恶化。"),
        _item("硬回避生效条件", "该条综合分 ≥ 50",
              "engine/classify.py · 硬回避前置判断",
              "三条硬回避中任一命中、且该条综合分 ≥ 50 时才提前返回回避；低分标的本就不会进结论。"),
    ]

    # ---------------------------------------------------- 2 结论前置过滤
    n_dem = cf.get("n_demoted")
    filt_items = [
        _item("120 日相对行业等权超额", f"≥ {_pct(flt['rs120_min'], 2)}（不为负）",
              "config.CONCLUSION_FILTER.rs120_min",
              "结论自带的失效条件已写明「超额转负即作废」，事前仍给建仓等于自相矛盾。"
              "实测被剔除的这类结论胜率仅 4.8%、平均超额 −7.91%。"),
        _item("近 60 日区间位置分位", f"≤ {_num(flt['pos60_max'], 2)}",
              "config.CONCLUSION_FILTER.pos60_max",
              "> 0.95 表示买在区间顶部 5%，属追高。剔除后组合年化 +2.9pp、最大回撤 −3.2pp；"
              "阈值 0.88~0.98 为宽平台，非拟合尖峰。"),
        _item("过滤动作", "降级为「回避」并保留在回避清单",
              "engine/classify.py · 结论前置过滤",
              "不删除、不择时——被过滤的结论仍可逐条复核，只在建仓环节不采纳。"),
    ]
    if n_dem is not None:
        filt_items.append(
            _item("本次运行实际降级", f"{n_dem} 条", "review.settlement.conclusion_filter",
                  "全区间累计被前置过滤降级的结论数。", kind="live"))

    # ---------------------------------------------------- 3 建仓执行
    exec_items = [
        _item("买入时点", "信号日 T+1 开盘价",
              "engine/settlement.py simulate_etf_portfolio",
              "T 日收盘出结论，下一交易日开盘成交——不能用当日收盘价，否则引入未来函数。"),
        _item("每日最多新建仓位", f"{_num(po['max_new_per_day'])} 个",
              "config.PORTFOLIO.max_new_per_day",
              "限制建仓节奏。实测限制为 2 笔后年化 +0.9pp、回撤 −0.3pp。"),
        _item("总容量上限", f"{cap} 个仓位",
              "= CLASSIFY.max_daily_picks × 4",
              "同时在场持仓数量上限，超出后当日新信号不再建仓（滑窗滚动而非加权重算）。"),
        _item("剩余容量计算顺序", "先结算到期仓位，再算剩余名额",
              "engine/settlement.py 逐日循环",
              "顺序反了会把「今日到期」误算成「仍占用容量」，导致少建仓。"),
        _item("建仓排序", "按综合分降序取前 N",
              "engine/settlement.py",
              "每日候选先按综合分排序，再受 max_new_per_day 截断。"),
        _item("标的映射", "细分赛道无 ETF → 回退所属申万一级行业 ETF",
              "engine/etf_map.py",
              "避免结论落在一个不可交易的指数上；代理关系在结论里显式标注。"),
    ]

    # ---------------------------------------------------- 4 配权与风控
    risk_items = [
        _item("建仓预算倾斜", f"∝ (综合分 / 50) ^ {_num(po['score_tilt'])} × 1/σ",
              "config.PORTFOLIO.score_tilt",
              "只作用于**建仓时的预算分配**（低波动、高分标的买得多一点）；"
              "建仓后权重随价格漂移，不再调整——本系统没有每日再平衡。"),
        _item("风险预算", "建仓预算按 1/σ 倾斜（风险平价）" if rc["risk_parity"] else "等额预算",
              "config.RISK_CONTROL.risk_parity",
              "低波动标的在建仓时多分一点预算。注意：权重不再每日重算，"
              "因此「风险贡献相等」只在建仓那一刻成立，之后随价格漂移。"),
        _item("σ 估计窗口", f"{rc['rp_window']} 个交易日",
              "config.RISK_CONTROL.rp_window",
              "单标的历史已实现波动，用作 1/σ 权重与组合波动估计。"),
        _item("组合目标年化波动",
              _pct(rc["vol_target"], 2) if rc["vol_target"] is not None else "已停用",
              "config.RISK_CONTROL.vol_target",
              "**已停用（ark-1.8.1）**。可执行账户里它只能缩小新仓预算、压不了存量暴露，"
              "实测全窗口年化 -1.6pp 而回撤反升；滚动一年均值 -2.3pp、只换来尾部 2.2pp。"),
        _item("暴露下限", f"{_pct(rc['vol_floor'], 2)}（随波动目标停用而不生效）",
              "config.RISK_CONTROL.vol_floor",
              "仅作历史记录：波动目标停用后该下限不再参与计算。"),
        _item("暴露上限（F5）",
              "max(动量cap, 环境cap)" if rc.get("exposure_cap", {}).get("enabled") else "已停用",
              "config.RISK_CONTROL.exposure_cap",
              "**已停用（ark-1.8.1）**。原设计是「下跌年份压暴露」，但那依赖每日再平衡；"
              "在可执行账户里它压不了存量，只剩阴跌期少买，全窗口变成 11.75%/-36.45%（更差）。"
              "要压尾部请调低目标持仓笔数 K（K=24 最差一年 -21.8%）。"),
        _item("未投入部分收益", f"{_pct(rc['cash_yield'], 2)} / 年",
              "config.RISK_CONTROL.cash_yield",
              "闲置资金按货币基金口径计息，避免低估空仓期的机会成本。"),
        _item("风险旋钮（ark-1.8.1 起）", "目标持仓笔数 K",
              "config.PORTFOLIO.target_positions",
              "可执行账户里唯一有效的风险旋钮：K 越小越集中、均值越高、尾部越深；"
              "K=24（当前）滚动一年 均值 14.47% / 最差 -21.84%；K=8 → 19.69% / -35.78%；"
              "K=32 → 11.49% / -21.68%。"),
    ]

    # ---------------------------------------------------- 5 卖出判定
    sell_items = [
        _item("候选持有周期", " / ".join(str(x) for x in hz) + " 个交易日",
              "config.HORIZON_CANDIDATES",
              "实证：10 日窗口平均超额仅 +0.27%、胜率 33%（噪音主导）；60 日窗口胜率 52.5%、"
              "平均超额 +10.4%。行业方向判断不适用两周级窗口。"),
        _item("周期择优方式",
              f"取历史最相似的 {HZ.K_NEIGHBORS} 个交易日，按风险调整期望择优",
              "engine/horizon.py estimate_horizon",
              f"目标函数 = 平均超额 − 0.25 × |平均最大回撤|（原 0.6 会系统性偏向最短窗口）；"
              f"有效样本 < {HZ.MIN_SAMPLES} 时回退默认 {HZ.DEFAULT_HORIZON} 日并标记 low_confidence，"
              "不允许用臆测填补。"),
        _item("主口径卖出", "持有到期后收盘价卖出",
              "engine/settlement.py settle_trade",
              "T+1 买入、持有 H 个交易日后收盘卖出，产生组合资金曲线的唯一卖出口径。"),
        _item("对照口径 B（不进曲线）", "连续 3 日收盘跌破 MA20 → 次日开盘平仓",
              "engine/review.py early_exit",
              "用作「若执行失效条件提前退出会怎样」的反事实对照，实测平均持仓从 80.9 日降到 10.2 日，"
              "因此不进生产曲线，只作为对照披露。"),
        _item("失效条件（预警，不自动卖）", "相对超额转负 / 跌破关键均线等",
              "engine/classify.py build_invalidation",
              "在「今日出手」里以「失效条件预警」对照口径呈现，主口径仍以到期为准。"),
    ]

    # ---------------------------------------------------- 6 打分与环境
    bw = C.BASE_WEIGHTS
    dim_names = {
        "mainline": "长期主线", "pullback": "回调空间", "exhaust": "抛压衰竭",
        "rebound": "触底确认", "risk": "风险控制", "regime_fit": "环境适配",
    }
    wdisp = " · ".join(
        f"{dim_names.get(k, k)} {v:.2f}" for k, v in bw.items())
    th = RG.REGIME_THRESHOLDS
    score_items = [
        _item("六维权重（基准）", wdisp, "config.BASE_WEIGHTS",
              "方向由 Rank IC 实证确定（不是直觉）：120 日动量 IC ≈ +0.55@40 日，"
              "60 日以内动量全部为负——A 股行业层面是「长期主线强动量 + 短期反转」的双层结构。"),
        _item("环境调权", "4 档环境各自乘权重系数",
              "config.REGIME_WEIGHT_ADJ",
              "ATTACK 强化主线与触底；PANIC 大幅提高风险与环境适配权重（risk ×1.50）。"),
        _item("环境档位阈值",
              f"≥{_num(th['attack'])} 进攻 · ≥{_num(th['neutral'])} 中性 · "
              f"≥{_num(th['defend'])} 防守 · <{_num(th['defend'])} 恐慌",
              "regime.REGIME_THRESHOLDS",
              "环境热度分（5 分项合成，0–100）的滞回映射阈值。"),
        _item("环境平滑与确认",
              f"α={_num(RG.EMA_ALPHA)} 指数平滑 + 连续 {RG.REGIME_CONFIRM_DAYS} 日确认",
              "regime.EMA_ALPHA / REGIME_CONFIRM_DAYS",
              "避免日频抖动导致的档位反复切换（等效约 3 日 EMA）。"),
        _item("恐慌强制覆盖", f"基准 60 日回撤 < {_pct(C.REGIME_RULES['bench_dd_panic'], 2, True)} → 恐慌",
              "config.REGIME_RULES.bench_dd_panic",
              f"波动率历史分位 > {_num(C.REGIME_RULES['vol_pct_panic'])} 时额外从进攻/中性降级为防守。"),
        _item("环境热度构成", "宽度 0.32 · 趋势 0.24 · 动能 0.24 · 低波 · 流动性",
              "engine/regime.py build_regime_series",
              "5 个分项合成 0–100 的环境热度分。"),
    ]

    # ---------------------------------------------------- 7 成本与评估口径
    rv = C.REVIEW
    cost_items = [
        _item("单边佣金",
              f"万分之{_num(cost['commission']*10000)}（{_pct(cost['commission'], 2)}）",
              "config.TRADING_COST.commission",
              "按成交金额计。ETF 免印花税，因此 stamp = 0。"),
        _item("单边滑点",
              f"万分之{_num(cost['slippage']*10000)}（{_pct(cost['slippage'], 2)}）",
              "config.TRADING_COST.slippage",
              "按成交金额计，对成交价直接打折。"),
        _item("单笔往返成本", f"{C.ROUND_TRIP_COST*100:.2f}%",
              "= 2 × (佣金 + 滑点)",
              "买卖各扣一次，所有收益数字均已扣除该成本。"),
        _item("ETF 份额折算复权", "已启用" if C.ETF_QFQ else "未启用",
              "config.ETF_QFQ",
              "ETF 拆分/合并会让单位价格按整数倍跳变而持有人价值不变。结算取价时"
              f"检测这类断点（偏离参照收益超 {_pct(C.QFQ_BREAK_THRESH, 2, True)}）并按参照还原，"
              "价值中性事件不记为盈亏；L0 原始数据不动。"),
        _item("「对」的判定线", f"相对基准超额 ≥ {_pct(rv['outperf_win'], 2, True)}",
              "config.REVIEW.outperf_win",
              "结论评估的胜率口径：跑赢主基准（沪深300）超过该幅度记为「对」。"),
        _item("「错」的判定线", f"相对基准超额 ≤ {_pct(rv['outperf_loss'], 2, True)}",
              "config.REVIEW.outperf_loss",
              "跑输超过该幅度记为「错」，两者之间记为「中性」。"),
        _item("前瞻检验网格", " / ".join(str(x) for x in rv["lookahead_grids"]) + " 个交易日",
              "config.REVIEW.lookahead_grids",
              "复盘时对每个窗口都做前瞻统计，避免只挑对自己有利的单一周期。"),
        _item("回测区间", f"{C.BACKTEST_START} 起（预热 {C.WARMUP} 日）",
              "config.BACKTEST_START / WARMUP",
              "跨越 2018 / 2021–22 / 2024 三轮熊市；预热期用于 250 日均线等指标成型。"),
    ]

    groups = [
        _group("buy", "买入判定：什么条件下给出建仓结论", 
               "打分 → 硬回避 → 入手/埋伏 → 取 Top。每一步都是硬阈值，无人工干预。",
               buy_items),
        _group("filter", "结论前置过滤：哪些结论不给",
               "只剔除「按系统自己的规则就不该给」的结论，不引入任何择时。", filt_items),
        _group("exec", "建仓执行：什么时候买、买多少",
               "行业指数只用于判断方向；一切成交都落在真实可交易 ETF 上。", exec_items),
        _group("risk", "配权与风控：持仓怎么分配",
               "从「看对了多少」转到「单笔下多少」。这一层不改变买什么、何时买。", risk_items),
        _group("sell", "卖出判定：什么时候卖",
               "主口径按周期到期卖出；失效条件只做预警与反事实对照。", sell_items),
        _group("score", "打分与环境：结论从哪来",
               "六维分 + 环境热度分，权重由 Rank IC 实证确定。", score_items),
        _group("cost", "成本与评估口径",
               "所有收益数字均已扣除成本；评估统一相对沪深 300。", cost_items),
    ]

    # ---------------------------------------------------- 已试但未采纳
    rejected = [
        {"rule": "同 ETF 槽位上限（集中度约束）",
         "result": "≤1 / ≤2 / ≤3 / ≤4 全部不满足 Pareto",
         "why": "诊断出「328 笔买入落在 25 只 ETF、最集中的一只被买 63 次」确有问题，"
                "但约束越紧 → 成交笔数骤降 → 资金长期空转，年化损失大于回撤收益。"
                "≤2 档为「回撤 −3.5pp 换年化 −2.6pp」，已在复盘页做成取舍表如实呈现。"},
        {"rule": "跳过 PANIC 环境建仓",
         "result": "不采纳",
         "why": "是有效的胜率杠杆，但分年度胜率在 0%~67.5% 之间跳变——属样本内不稳定的伪改进。"},
        {"rule": "pos250 / 60 日回撤深度做成硬规则",
         "result": "不采纳",
         "why": "分位上胜率分离度比已采纳的因子更大，但做成硬规则后组合年化与回撤同时劣化。"
                "说明那是「概率梯度」而非「可切除的坏样本」。"},
        {"rule": "MA250 偏离 ≤ 20% 的宽松版追高约束",
         "result": "不采纳",
         "why": "最大回撤从 −29.9% 恶化到 −44%。更严的阈值反而把「强势主升段」也切掉了。"},
        {"rule": "移动止损 / 回撤熔断",
         "result": "不采纳",
         "why": "不降低总暴露，只把仓位在下跌过程中反复洗牌，回撤反而更大。"
                "降回撤唯一有效的机制是「降低总暴露」（波动目标）。"},
        {"rule": "提高信号门槛 / 限制同行业建仓",
         "result": "不采纳",
         "why": "同样属于「不降暴露只换仓」，实测回撤更差。"},
    ]

    return {
        "engine_version": None,      # 由 pipeline 填
        "config_hash": None,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "engine/config.py（页面数值与此文件同源，改配置即同步，不存在前端硬编码）",
        "groups": groups,
        "rejected": rejected,
        "floor_grid": C.RISK_CONTROL_FLOORS,
        "live": {
            "demoted": n_dem,
            "regime": (today or {}).get("regime"),
            "n_picks": len((today or {}).get("picks") or []),
            "n_open": ((today or {}).get("action_today") or {}).get("n_open"),
        },
    }


if __name__ == "__main__":
    import json
    r = build_rules()
    print(json.dumps(r, ensure_ascii=False, indent=1)[:3000])
    print("\n分组数:", len(r["groups"]),
          "| 参数条数:", sum(len(g["items"]) for g in r["groups"]),
          "| 未采纳:", len(r["rejected"]))
