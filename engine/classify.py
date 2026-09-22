# -*- coding: utf-8 -*-
"""工作流 Step 4 —— 信号共振与结论分类。

把六维分 + 原始指标 + 环境状态，压缩成一句可执行的判断：
  BUY_NOW（趋势已确立，可入手）/ AMBUSH（结构转好但动能未确认，可埋伏）
  / WATCH（观察，不进结论）/ AVOID（过热或破位，回避）

每个结论必须携带：触发理由链（来自真实指标）、失效条件、建议持有周期。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from feature_store import FeatureStore  # noqa: E402
from horizon import estimate_horizon  # noqa: E402

ACTION_LABEL = {
    "BUY_NOW": "入手",
    "AMBUSH": "埋伏",
    "WATCH": "观察",
    "AVOID": "回避",
}

# MATURE 阶段综合分降权系数。**生产值在 config.CLASSIFY["mature_weight"]**
# （2026-09-22 落参 0.7，依据实验 lab_mature_weight，boss 确认采用）。
# 环境变量 ARK_MATURE_W 只作实验台覆盖入口（lab_mature_weight.py 用它复现各档变体），
# 不是生产配置通道 —— 要改生产口径请改 config，别用 env。
MATURE_WEIGHT = float(os.environ.get(
    "ARK_MATURE_W", C.CLASSIFY.get("mature_weight", 1.0)))

# ---- 结论闸门旋钮（**默认值 = 生产口径，env 只作实验台覆盖入口**）----
# 2026-09-22 诊断：2024 全年 233/242 个交易日零结论，硬约束是 AMBUSH 的
# `pullback >= 52`。该维度在打分层被 ml_gate（0.45~1.0）乘法挤压——同一份
# 截面若不乘 ml_gate 有 55.8 个/天达 52 分，乘完只剩 0.40 个（140 倍压制），
# 于是「埋伏」只在「行业深崩 + 长期主线仍强」的极端组合下才成立。
# 这些旋钮用于 lab_gate2024.py 逐档反事实，未确认前不进 config。
_AB_ML_MIN = float(os.environ.get("ARK_AMBUSH_ML_MIN", 52))
_AB_PB_MIN = float(os.environ.get("ARK_AMBUSH_PB_MIN", 52))
_AB_EX_MIN = float(os.environ.get("ARK_AMBUSH_EX_MIN", 50))
_BN_ML_MIN = float(os.environ.get("ARK_BUYNOW_ML_MIN", 58))
_BN_RB_MIN = float(os.environ.get("ARK_BUYNOW_RB_MIN", 55))
_BN_RK_MIN = float(os.environ.get("ARK_BUYNOW_RK_MIN", 35))
# 自适应门槛：AMBUSH 的综合分门槛改为「当日候选截面分位」，并保留绝对下限
_COMP_PCT = os.environ.get("ARK_COMP_PCT")
_COMP_FLOOR = float(os.environ.get("ARK_COMP_FLOOR", 50))
# 趋势延续型入场（默认关闭 = 生产口径）。现有两种入场语义**都要求回调**：
#   AMBUSH 要 pullback>=52（回调充分），BUY_NOW 要 rebound>=55（跌后动能转强）。
# 2024 的机会恰是「没有回调的持续趋势」（银行/非银/通信/芯片全年 +30~42%，
# 全年不出现深调）——对这种行情系统在语义上无法出手。此旋钮增加第三条入场
# 路径：长期主线够强 + 中短期动能同向为正（不看回调）。
# 取值 = mainline 门槛（如 65）；None = 关闭。
_MOMO_ML = os.environ.get("ARK_MOMO_ML")
_MOMO_ML = float(_MOMO_ML) if _MOMO_ML else None


def _f(x) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _pct(x, nd=2) -> str:
    v = _f(x)
    return "—" if v is None else f"{v*100:+.{nd}f}%"


def build_reason_chain(fs: FeatureStore, code: str, date: str,
                       dims_row: Dict[str, float], regime: str) -> List[str]:
    """理由链：每条都由当日的真实指标推导，可被复核。"""
    r = fs.row(code, date)
    reasons: List[str] = []
    ml = dims_row.get("mainline", 50)
    pb = dims_row.get("pullback", 50)
    ex = dims_row.get("exhaust", 50)
    rb = dims_row.get("rebound", 50)
    rk = dims_row.get("risk", 50)

    if ml >= 62:
        reasons.append(
            f"长期主线成立：120日 {_pct(r['ret_120'])} / 250日 {_pct(r['ret_250'])}，"
            f"相对行业等权 {_pct(r['rs_120'])}，价格位于 MA120 {_pct(r['dist_ma120'])}"
            f"（主线分 {ml:.0f}）")
    elif ml <= 38:
        reasons.append(
            f"缺乏长期主线：120日 {_pct(r['ret_120'])}，位于 MA250 {_pct(r['dist_ma250'])}"
            f"（主线分 {ml:.0f}，下跌趋势中的反弹不参与）")

    if pb >= 58:
        reasons.append(
            f"回调给出空间：20日 {_pct(r['ret_20'])}，距60日高点 {_pct(r['dd_from_high60'])}，"
            f"位于 MA20 {_pct(r['dist_ma20'])}（回调分 {pb:.0f}，已按主线强度折算）")
    elif pb <= 40:
        reasons.append(f"回调不充分：20日 {_pct(r['ret_20'])}，距60日高点仅 {_pct(r['dd_from_high60'])}")

    if ex >= 58:
        reasons.append(
            f"抛压衰竭：5/20日量比 {r['vol_ratio']:.2f}、成交额比 {r['amount_ratio']:.2f}，"
            f"上涨日成交占比 {r['up_vol_share']*100:.0f}%（量能分 {ex:.0f}）")
    elif ex <= 40:
        reasons.append(f"量能未收敛：5/20日量比 {r['vol_ratio']:.2f}，仍有放量迹象，等待缩量确认")

    if rb >= 58:
        reasons.append(
            f"触底信号：5日 {_pct(r['ret_5'])} 强于20日线性外推，MA20 五日斜率 {_pct(r['ma20_slope'])}"
            f"（触底分 {rb:.0f}）")
    elif rb <= 40:
        reasons.append(f"尚未触底：5日 {_pct(r['ret_5'])}，跌势未见减缓（触底分 {rb:.0f}）")

    if rk <= 38:
        reasons.append(
            f"风险提示：年化波动 {r['vol20']:.2f}，5/20日量比 {r['vol_ratio']:.2f}，波动分 {rk:.0f}")

    fit = dims_row.get("regime_fit", 50)
    env_txt = {"ATTACK": "进攻", "NEUTRAL": "中性", "DEFEND": "防守", "PANIC": "恐慌"}.get(regime, "中性")
    reasons.append(f"环境适配：当前「{env_txt}」环境下适配分 {fit:.0f}")

    return reasons


def build_invalidation(fs: FeatureStore, code: str, date: str, action: str) -> List[str]:
    r = fs.row(code, date)
    d120 = _f(r["dist_ma120"])
    inv = []
    if d120 is not None:
        inv.append(f"跌破 MA120（当前偏离 {d120*100:+.1f}%）——主线破坏，结论作废")
    else:
        inv.append("跌破 MA120——主线破坏，结论作废")
    inv.append(f"120日相对行业等权超额转负（当前 {_pct(r['rs_120'])}）")
    inv.append(f"自60日高点回撤扩大到 {C.CLASSIFY['avoid_dd']*100:.0f}% 以上"
               f"（当前 {_pct(r['dd_from_high60'])}）且量比 >1.5（放量下跌）")
    if action == "AMBUSH":
        inv.append("埋伏后 15 个交易日内触底分仍低于 45（判定为下跌中继而非回调）")
    else:
        inv.append("触底分回落至 40 以下且 5 日收益转负（反弹证伪）")
    return inv


def classify_day(fs: FeatureStore, dims: Dict[str, np.ndarray], comp: np.ndarray,
                 regime_series: Dict[str, dict], state: np.ndarray, fwd: dict,
                 date: str, verbose: bool = False,
                 stages: Optional[Dict[str, np.ndarray]] = None) -> dict:
    """生成单日完整结论。stages 为行业周期阶段标注（仅展示，不影响结论）。"""
    i = fs.idx(date)
    info = regime_series.get(date)
    if not info:
        return {"date": date, "error": "no_regime"}
    regime = info["regime"]

    candidates = fs.investable(date)
    if not candidates:
        return {"date": date, "error": "no_candidates", "regime": regime}

    codes = fs.codes
    row_of = {}
    for c in candidates:
        ci = codes.index(c)
        row_of[c] = {
            "mainline": float(dims["mainline"][ci, i]),
            "pullback": float(dims["pullback"][ci, i]),
            "exhaust": float(dims["exhaust"][ci, i]),
            "rebound": float(dims["rebound"][ci, i]),
            "risk": float(dims["risk"][ci, i]),
            "regime_fit": float(dims["regime_fit"][ci, i]),
            "composite": float(comp[ci, i]),
        }

    # 行业周期阶段（展示层标注，来自 scoring.compute_stage，缺失为 ""）
    def _stage(c: str) -> str:
        if stages is None or c not in stages:
            return ""
        arr = stages[c]
        return str(arr[i]) if 0 <= i < len(arr) else ""

    stage_of = {c: _stage(c) for c in candidates}

    # MATURE 阶段降权（生产口径，系数见 config.CLASSIFY["mature_weight"]）：
    # 成熟期主线的综合分乘以系数，让它在排名 / 入选阈值 / Top-K 与结算槽位竞争中
    # 真实让位。只动 composite，不触碰 mainline 等阈值维度（那些决定「是否回避」，
    # 语义不同）。stage 标注本身来自 scoring.compute_stage，是纯展示层数据。
    _mw = MATURE_WEIGHT
    if _mw != 1.0:
        for c in candidates:
            if stage_of[c] == "MATURE":
                row_of[c]["composite"] = float(row_of[c]["composite"]) * _mw

    # 主线 / 风险 的横截面分位（用于阈值与归因）
    ml_vals = {c: row_of[c]["mainline"] for c in candidates}
    rk_vals = {c: row_of[c]["risk"] for c in candidates}

    # AMBUSH 的综合分门槛：默认取生产的绝对阈值；实验台可用 ARK_COMP_PCT
    # 换成「当日候选截面分位 + 绝对下限」的自适应门槛。
    ambush_min = float(C.CLASSIFY["ambush_min"])
    if _COMP_PCT:
        _arr = np.array([row_of[c]["composite"] for c in candidates], dtype=float)
        ambush_min = max(_COMP_FLOOR, float(np.percentile(_arr, float(_COMP_PCT))))

    picks, watch, avoid = [], [], []
    for c in sorted(candidates, key=lambda x: -row_of[x]["composite"]):
        d = row_of[c]
        r = fs.row(c, date)
        base = {
            "code": c, "name": fs.meta[c]["name"], "level": fs.meta[c]["level"],
            "composite": round(d["composite"], 2),
            "stage": stage_of[c],
            "dims": {k: round(v, 1) for k, v in d.items() if k != "composite"},
            "metrics": {
                "ret_5": _f(r["ret_5"]), "ret_20": _f(r["ret_20"]), "ret_60": _f(r["ret_60"]),
                "ret_120": _f(r["ret_120"]), "ret_250": _f(r["ret_250"]),
                "rs_20": _f(r["rs_20"]), "rs_60": _f(r["rs_60"]), "rs_120": _f(r["rs_120"]),
                "dist_ma20": _f(r["dist_ma20"]), "dist_ma120": _f(r["dist_ma120"]),
                "dist_ma250": _f(r["dist_ma250"]), "ma20_slope": _f(r["ma20_slope"]),
                "vol_ratio": _f(r["vol_ratio"]),
                "amount_ratio": _f(r["amount_ratio"]), "up_vol_share": _f(r["up_vol_share"]),
                "sharpe20": _f(r["sharpe20"]), "maxdd_60": _f(r["maxdd_60"]),
                "pos60": _f(r["pos60"]), "pos250": _f(r["pos250"]),
                "vol20": _f(r["vol20"]), "consec_up": _f(r["consec_up"]),
                "dd_from_high60": _f(r["dd_from_high60"]),
                "close": _f(r["close"]),
            },
        }
        # ---------- 排除 / 回避 ----------
        is_avoid = False
        avoid_why = []
        if d["mainline"] < 30:
            is_avoid = True
            avoid_why.append("长期趋势处于下降通道（120/250 日动量为负），反弹不参与")
        d250 = _f(r["dist_ma250"])
        if d250 is not None and d250 > 0.45:
            is_avoid = True
            avoid_why.append(f"价格高于 MA250 {d250*100:.0f}%，长期超涨（复盘显示追高是首要失误来源）")
        if d["risk"] < 22:
            is_avoid = True
            avoid_why.append("波动/量能处于极端区间，风险收益比恶化")

        if is_avoid and d["composite"] >= 50:
            base["action"] = "AVOID"
            base["why"] = avoid_why
            avoid.append(base)
            continue

        # ---------- 入手 / 埋伏 ----------
        # 阈值取自模块级旋钮（默认=config，实验台可覆盖）；AMBUSH 的 exhau-st 语义
        # 见 config.CLASSIFY 注释。用变量是为了让实验台能在同一份代码路径上做反事实。
        action = "WATCH"
        trigger = ""
        if (d["composite"] >= C.CLASSIFY["buy_now_min"] and d["mainline"] >= _BN_ML_MIN
                and d["rebound"] >= _BN_RB_MIN and d["risk"] >= _BN_RK_MIN):
            action = "BUY_NOW"
            trigger = "长期主线成立 + 短期触底确认：主线趋势未破，回调后动能重新转强"
        elif (d["composite"] >= ambush_min and d["mainline"] >= _AB_ML_MIN
              and d["pullback"] >= _AB_PB_MIN and d["exhaust"] >= _AB_EX_MIN):
            action = "AMBUSH"
            trigger = "主线行业回调中 + 抛压衰竭：长期趋势仍在，等待缩量企稳后的反弹"
        elif (_MOMO_ML is not None and d["composite"] >= ambush_min
              and d["mainline"] >= _MOMO_ML
              and (_f(r["ret_20"]) or 0) > -0.02 and (_f(r["ret_60"]) or 0) > 0
              and d["risk"] >= _BN_RK_MIN):
            # 趋势延续型（实验旋钮，默认关闭）：不要求回调，要求中长期动能同向为正
            action = "AMBUSH"
            trigger = "长期主线强 + 中短期动能延续：趋势未破且未过热，顺势参与（不要求回调）"

        base["action"] = action
        base["trigger"] = trigger
        if action in ("BUY_NOW", "AMBUSH"):
            picks.append(base)
        else:
            watch.append(base)

    # ---------- 取 Top：入手优先，埋伏补足 ----------
    buys = [p for p in picks if p["action"] == "BUY_NOW"][:C.CLASSIFY["max_buy_now"]]
    amb = [p for p in picks if p["action"] == "AMBUSH"][:C.CLASSIFY["max_daily_picks"] - len(buys)]
    final = buys + amb

    # ---------- 结论前置过滤：把「事前就自相矛盾 / 明显追高」的降级为回避 ----------
    # 不引入任何择时：只剔除那些按本系统自己的规则就不该给出的结论。
    #   · rs120 < 0：结论自带的失效条件里写着「120日相对行业等权超额转负 -> 结论作废」，
    #     事前给出建仓结论等于自相矛盾（实测这类结论胜率仅 4.8%、平均超额 -7.91%）。
    #   · pos60 > 0.95：买在近 60 日区间顶部 5%，属追高（实测年化 +2.9pp、回撤 -3.2pp）。
    # 实证见 engine/lab_opt*.py：实验台先精确复现线上基线后才做反事实。
    FLT = getattr(C, "CONCLUSION_FILTER", {})
    demoted: List[dict] = []
    kept: List[dict] = []
    for p in final:
        f = fs.row(p["code"], date)
        rs120 = _f(f["rs_120"])
        pos60 = _f(f["pos60"])
        why: List[str] = []
        if FLT.get("rs120_min") is not None and rs120 is not None and rs120 < FLT["rs120_min"]:
            why.append(f"120日相对行业等权超额为负（当前 {_pct(rs120)}）"
                       "—— 与结论自带的失效条件冲突，事前即作废")
        if FLT.get("pos60_max") is not None and pos60 is not None and pos60 > FLT["pos60_max"]:
            why.append(f"价格位于近60日区间顶部（位置分位 {pos60*100:.0f}%，"
                       f"距区间高点仅 {abs(_f(f['dd_from_high60']) or 0)*100:.1f}%）"
                       "—— 属追高，复盘显示这类位置是主要失误来源")
        if why:
            p["action"] = "AVOID"
            p["why"] = why
            p["filtered"] = True
            demoted.append(p)
        else:
            kept.append(p)
    final = kept

    # ---------- 为最终结论估计周期 + 生成理由 ----------
    for p in final:
        ci = codes.index(p["code"])
        hz = estimate_horizon(fs, dims, regime_series, fwd, p["code"], date, state[ci])
        # 到期的日历日：能查到就用真实交易日，超出样本范围则按 1.45 自然日/交易日外推
        fi = i + hz["horizon"]
        if fi < len(fs.dates):
            hz["exit_est"] = fs.dates[fi]
        else:
            base = dt.datetime.strptime(date, "%Y-%m-%d")
            hz["exit_est"] = (base + dt.timedelta(days=int(hz["horizon"] * 1.45))).strftime("%Y-%m-%d")
            hz["exit_est_is_projection"] = True
        p["horizon"] = hz
        p["reasons"] = build_reason_chain(fs, p["code"], date, p["dims"], regime)
        p["invalidation"] = build_invalidation(fs, p["code"], date, p["action"])
        p["crowding_pct"] = float(np.nanmean(
            [1.0 if rk_vals[o] <= p["dims"]["risk"] else 0.0 for o in candidates]))
        p["trend_pct"] = float(np.nanmean(
            [1.0 if ml_vals[o] <= p["dims"]["mainline"] else 0.0 for o in candidates]))
    for a in (demoted + avoid)[:6]:
        a["horizon"] = None

    # ---------- 行业全景表（ranking） ----------
    # 全部候选行业按综合分排序的紧凑视图，每行带 60/80/100 三个持有周期下
    # 「历史相似样本」的前瞻统计（超额收益/胜率/平均回撤），供页面全景表与调仓对比。
    # 不改变任何结论逻辑：picks/avoid/watch 的生成路径原样保留，这里只做导出。
    # 回避类不再重复估周期（无意义——结论已否决），action 原样标出。
    ranking: List[dict] = []
    for c in sorted(candidates, key=lambda x: -row_of[x]["composite"]):
        act = next(
            (p["action"] for p in final if p["code"] == c),
            next((a["action"] for a in (demoted + avoid) if a["code"] == c), "WATCH"),
        )
        row = {
            "code": c, "name": fs.meta[c]["name"], "level": fs.meta[c]["level"],
            "composite": round(row_of[c]["composite"], 2), "action": act,
            "stage": stage_of[c],
        }
        if act in ("BUY_NOW", "AMBUSH"):
            # 最终结论已估过周期，直接复用，保证与结论卡同源
            p = next(p for p in final if p["code"] == c)
            hz = p.get("horizon") or {}
            if hz.get("stats"):
                row["hz"] = _compact_hz(hz["stats"])
                row["hz_picked"] = hz.get("horizon")
        elif act != "AVOID":
            ci2 = codes.index(c)
            hz2 = estimate_horizon(fs, dims, regime_series, fwd, c, date, state[ci2])
            if hz2.get("stats"):
                row["hz"] = _compact_hz(hz2["stats"])
                row["hz_picked"] = hz2.get("horizon")
        ranking.append(row)

    return {
        "date": date,
        "regime": regime,
        "regime_info": {
            "env_score": info["env_score"], "confidence": info["confidence"],
            "breadth20": info.get("breadth20"), "median_ret20": info.get("median_ret20"),
            "bench_ret20": info.get("bench_ret20"), "bench_dd60": info.get("bench_dd60"),
            "dispersion20": info.get("dispersion20"), "vol_pct": info.get("vol_pct"),
            "components": info.get("components", {}), "forced": info.get("forced"),
        },
        "universe_size": len(candidates),
        "picks": final,
        "ranking": ranking,
        "n_filtered_conclusions": len(demoted),
        "avoid": (demoted + avoid)[:6],
        "watch_top": [
            {"code": w["code"], "name": w["name"], "level": w["level"],
             "composite": w["composite"]} for w in watch[:6]
        ],
    }


def _compact_hz(stats: dict) -> dict:
    """把 horizon.stats 压成 60/80/100 -> {excess, win, dd} 的紧凑结构（省盘面）。"""
    out = {}
    for H, s in (stats or {}).items():
        if s.get("insufficient"):
            continue
        out[str(H)] = {
            "excess": s.get("mean_excess"), "win": s.get("win_rate"),
            "dd": s.get("mean_maxdd"),
        }
    return out


def summarize(day: dict) -> str:
    """一句话总览（给页面顶部用）。"""
    if day.get("error"):
        return f"{day['date']} 无有效结论（{day['error']}）"
    reg = {"ATTACK": "进攻", "NEUTRAL": "中性", "DEFEND": "防守", "PANIC": "恐慌"}[day["regime"]]
    n_buy = sum(1 for p in day["picks"] if p["action"] == "BUY_NOW")
    n_amb = sum(1 for p in day["picks"] if p["action"] == "AMBUSH")
    names = "、".join(p["name"] for p in day["picks"][:3])
    if not day["picks"]:
        return f"环境「{reg}」，今日无行业通过共振门槛——空仓等待本身就是结论。"
    return (f"环境「{reg}」（热度 {day['regime_info']['env_score']:.0f}），"
            f"{n_buy} 个入手 / {n_amb} 个埋伏；首选 {names}。")
