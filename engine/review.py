# -*- coding: utf-8 -*-
"""工作流 Step 7 —— 复盘引擎。

注意：这里复盘的不是「市场走势」，而是「ARK 自己过往的每日结论」。
每一条历史结论都要回答四件事：
    1. 结果如何（持有到期后的真实收益 / 超额 / 最大回撤）
    2. 判定：对 / 错 / 中性
    3. 归因：为什么（环境误判？选错行业？追高？埋伏过早？周期估错？系统性？波动失控？）
    4. 改进：用反事实统计回答「如果当时收紧某个条件，结果会好多少」——必须有数字，不许拍脑袋。
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from feature_store import EW_CODE, FeatureStore  # noqa: E402
from settlement import (  # noqa: E402
    EtfBook, simulate_etf_portfolio, market_phases, stats_by_phase, perf_stats,
    holding_stats,
)

ATTR_LABEL = {
    "REGIME_MISREAD": "环境误判：出手时的环境档位与后续实际走势相悖",
    "SECTOR_PICK": "行业选错：同期存在明显更好的行业，横向比较跑输",
    "MAINLINE_BROKEN": "主线证伪：出手时所属行业并未处于长期上升趋势，反转逻辑落空",
    "TIMING_LATE": "追高：出手时价格已显著高于长期均线，属于超涨位置",
    "TIMING_EARLY": "埋伏过早：左侧进场后先经历明显回撤",
    "HORIZON_ERROR": "周期估错：方向对但持有窗口错配（过短或过长）",
    "BETA_DRAG": "系统性拖累：全市场下行，行业难以独善其身",
    "VOL_BLOWUP": "波动失控：持仓期波动显著放大，风险收益比恶化",
}
SUCCESS_LABEL = {
    "MAINLINE_LEAD": "主线领先：出手时该行业的长期动量已居全市场前列",
    "PULLBACK_ENTRY": "回调买点：在长期上升趋势内等到了回调空间",
    "EXHAUST_CONFIRM": "抛压衰竭：缩量企稳被后续走势验证",
    "REBOUND_CONFIRM": "触底确认：进场时的跌势减缓信号被验证",
    "REGIME_RIGHT": "环境判断正确：所处环境档位与后续走势一致",
}


# ------------------------------------------------------------------ 工具
def _get(a, i):
    v = a[i]
    if v != v:
        return None
    return float(v)


def fwd_stats(fs: FeatureStore, code: str, i: int, H: int) -> Optional[dict]:
    """从日期下标 i 起、持有 H 个交易日的真实结果。超出样本范围返回 None。"""
    n = len(fs.dates)
    if i + H >= n or i < 0:
        return None
    c = fs.series(code, "close")
    ew = fs.series(EW_CODE, "close")
    bench_code = C.PRIMARY_BENCH if C.PRIMARY_BENCH in fs.codes else None
    p0, p1 = c[i], c[i + H]
    if not p0 or p0 != p0 or p1 != p1:
        return None
    ret = p1 / p0 - 1.0
    seg = c[i + 1:i + H + 1]
    dd = float((seg.min() / p0) - 1.0) if seg.size else None
    ew0, ew1 = ew[i], ew[i + H]
    ex_ew = ret - (ew1 / ew0 - 1.0) if ew0 and ew0 == ew0 else None
    ex_bench = None
    if bench_code:
        b = fs.series(bench_code, "close")
        b0, b1 = b[i], b[i + H]
        if b0 and b0 == b0:
            ex_bench = ret - (b1 / b0 - 1.0)
    return {"ret": float(ret), "dd": dd, "ex_ew": ex_ew, "ex_bench": ex_bench}


def cross_median(fs: FeatureStore, i: int, H: int) -> Optional[float]:
    """同期 L1 行业收益的中位数（用于判断「有没有更好的选择」）。"""
    n = len(fs.dates)
    if i + H >= n:
        return None
    vals = []
    for c in fs.l1_codes():
        s = fs.series(c, "close")
        p0, p1 = s[i], s[i + H]
        if p0 and p0 == p0 and p1 == p1:
            vals.append(p1 / p0 - 1.0)
    return float(np.median(vals)) if len(vals) >= 5 else None


# ------------------------------------------------------------------ 单条评估
def evaluate_pick(fs: FeatureStore, day: dict, pick: dict, i: int,
                  regime_series: Dict[str, dict],
                  book: Optional[EtfBook] = None) -> Optional[dict]:
    """评估一条历史结论。

    双口径并存，绝不混用：
        - 信号口径：行业指数 vs 行业等权 —— 衡量「方向判断本身」是否有效
        - 结算口径：真实 ETF 成交（含成本）—— 衡量「这个判断能不能赚到钱」
    """
    H = pick["horizon"]["horizon"]
    st = fwd_stats(fs, pick["code"], i, H)
    if st is None:
        return None
    ex = st["ex_ew"] if st["ex_ew"] is not None else 0.0

    # ---- 结算口径（ETF 真实成交）
    tf = None
    tf_early = None
    if book is not None:
        tf = book.settle_trade(pick["name"], day["date"], H)
        # 口径 B：执行结论里写明的失效条件（跌破 MA20 即退出）
        tf_early = book.settle_trade(pick["name"], day["date"], H, early_exit=True)
    settled = tf is not None
    etf_ex = (tf["ex_bench"] if tf and tf["ex_bench"] is not None else None)

    def _verdict(x: Optional[float]) -> str:
        if x is None:
            return "NEUTRAL"
        if x >= C.REVIEW["outperf_win"]:
            return "RIGHT"
        if x <= C.REVIEW["outperf_loss"]:
            return "WRONG"
        return "NEUTRAL"

    # 两条口径严格分离，绝不混用：
    #   verdict     -> 信号口径（行业指数 vs 行业等权），描述「方向判断本身」
    #   etf_verdict -> 结算口径（ETF 净收益 vs 基准 ETF），描述「能不能赚到钱」
    verdict = _verdict(ex)
    etf_verdict = _verdict(etf_ex) if settled else None

    med = cross_median(fs, i, H)
    dims = pick["dims"]
    regime = day["regime"]

    # 后续市场实际走势（用于环境归因）
    ew_fwd = None
    ew = fs.series(EW_CODE, "close")
    if i + H < len(fs.dates):
        ew_fwd = float(ew[i + H] / ew[i] - 1.0)

    attrs: List[str] = []
    m = pick.get("metrics", {})
    if verdict == "WRONG":
        # 按「可改进性」排优先级，最多保留 2 个主因，避免一条结论被打满标签
        cand: List[str] = []
        if (pick.get("trend_pct") or 0.5) < 0.50:
            cand.append("MAINLINE_BROKEN")
        if (m.get("dist_ma250") or 0) > 0.40:
            cand.append("TIMING_LATE")
        if regime in ("ATTACK", "NEUTRAL") and ew_fwd is not None and ew_fwd < -0.05:
            cand.append("REGIME_MISREAD")
        if pick["action"] == "AMBUSH" and st["dd"] is not None and st["dd"] < -0.08 and st["ret"] > -0.02:
            cand.append("TIMING_EARLY")
        longer = fwd_stats(fs, pick["code"], i, min(H * 2, 120))
        shorter = fwd_stats(fs, pick["code"], i, max(H // 2, 5))
        if ((longer and longer["ex_ew"] is not None and longer["ex_ew"] > ex + 0.05)
                or (shorter and shorter["ex_ew"] is not None and shorter["ex_ew"] > ex + 0.05)):
            cand.append("HORIZON_ERROR")
        v0 = fs.series(pick["code"], "vol20")[i]
        v1 = fs.series(pick["code"], "vol20")[i + H] if i + H < len(fs.dates) else None
        if v0 == v0 and v1 == v1 and v0 and v1 / v0 > 1.6:
            cand.append("VOL_BLOWUP")
        if ew_fwd is not None and ew_fwd < -0.03 and st["ret"] < 0:
            cand.append("BETA_DRAG")
        if med is not None and st["ret"] < med - 0.06:      # 跑输同行中位数 6% 以上才算选错
            cand.append("SECTOR_PICK")
        attrs = cand[:2] or ["SECTOR_PICK"]
    elif verdict == "RIGHT":
        if dims.get("mainline", 50) >= 65:
            attrs.append("MAINLINE_LEAD")
        if dims.get("pullback", 50) >= 60:
            attrs.append("PULLBACK_ENTRY")
        if dims.get("exhaust", 50) >= 60:
            attrs.append("EXHAUST_CONFIRM")
        if dims.get("rebound", 50) >= 60:
            attrs.append("REBOUND_CONFIRM")
        if regime in ("ATTACK", "NEUTRAL") and (ew_fwd or 0) > 0.02:
            attrs.append("REGIME_RIGHT")

    out = {
        "date": day["date"], "code": pick["code"], "name": pick["name"],
        "level": pick["level"], "action": pick["action"], "regime": regime,
        "horizon": H, "composite": pick["composite"], "dims": dims,
        "crowding_pct": pick.get("crowding_pct"), "trend_pct": pick.get("trend_pct"),
        "ret": round(st["ret"], 4), "ex_ew": round(ex, 4),
        "ex_bench": round(st["ex_bench"], 4) if st["ex_bench"] is not None else None,
        "maxdd": round(st["dd"], 4) if st["dd"] is not None else None,
        "cross_median": round(med, 4) if med is not None else None,
        "verdict": verdict, "attribution": attrs,
        # ---- 结算口径
        "settled": settled,
        "etf_verdict": etf_verdict,
        "etf": tf["etf_name"] if tf else None,
        "etf_code": tf["etf"] if tf else None,
        "etf_ret": tf["net_ret"] if tf else None,
        "etf_gross": tf["gross_ret"] if tf else None,
        "etf_ex_bench": round(etf_ex, 4) if etf_ex is not None else None,
        "etf_maxdd": tf["maxdd"] if tf else None,
        "etf_buy": tf["buy_date"] if tf else None,
        "etf_sell": tf["sell_date"] if tf else None,
        "etf_reason": tf["reason"] if tf else None,
        "etf_held": tf["held_days"] if tf else None,
        # 口径 B（执行失效条件）
        "etf_early_ret": tf_early["net_ret"] if tf_early else None,
        "etf_early_ex_bench": (round(tf_early["ex_bench"], 4)
                               if tf_early and tf_early["ex_bench"] is not None else None),
        "etf_early_maxdd": tf_early["maxdd"] if tf_early else None,
        "etf_early_exited": tf_early["exited_early"] if tf_early else None,
        "etf_early_days": tf_early["held_days"] if tf_early else None,
        "etf_early_sell": tf_early["sell_date"] if tf_early else None,
        "etf_early_sid": tf_early["etf"] if tf_early else None,
        "etf_early_reason": tf_early["reason"] if tf_early else None,
    }
    # 结算口径下的绝对收益胜负（与「跑赢基准」并列展示）
    if settled:
        out["etf_abs_win"] = bool(tf["net_ret"] > 0)
    return out


# ------------------------------------------------------------------ 组合级验证
def simulate_portfolio(fs: FeatureStore, evals: List[dict]) -> dict:
    """把历史结论按「等权 + 各自建议持有期」模拟成净值曲线。

    这是结论质量的验证口径：不含成本、不含仓位管理、不含择时加减仓，
    只回答「如果照着每日结论等权持有，方向判断整体上有没有正超额」。
    """
    dates = fs.dates
    di = {d: i for i, d in enumerate(dates)}
    holdings = defaultdict(list)      # date_index -> [code]（当日活跃持仓）
    for e in evals:
        i0 = di.get(e["date"])
        if i0 is None:
            continue
        i1 = i0 + e["horizon"]
        if i1 >= len(dates):
            continue
        for k in range(i0, i1):
            holdings[k].append(e["code"])
    if not holdings:
        return {"dates": [], "curve": [], "bench": [], "stats": {}}

    curve, bench_curve, used_dates = [], [], []
    bench_code = C.PRIMARY_BENCH if C.PRIMARY_BENCH in fs.codes else EW_CODE
    bseries = fs.series(bench_code, "close")
    nav, bnav = 1.0, 1.0
    for k in range(len(dates) - 1):
        if k not in holdings:
            continue
        # 每日在全部活跃持仓上等权配置（不因持有期长短而放大/缩小权重）
        rets = []
        for code in holdings[k]:
            s = fs.series(code, "close")
            p0, p1 = s[k], s[k + 1]
            if p0 and p0 == p0 and p1 == p1:
                rets.append(p1 / p0 - 1.0)
        if not rets:
            continue
        tot = float(np.mean(rets))
        b0, b1 = bseries[k], bseries[k + 1]
        br = (b1 / b0 - 1.0) if (b0 and b0 == b0) else 0.0
        nav *= (1.0 + tot)
        bnav *= (1.0 + br)
        curve.append(round(nav, 5))
        bench_curve.append(round(bnav, 5))
        used_dates.append(dates[k + 1])
    if not curve:
        return {"dates": [], "curve": [], "bench": [], "stats": {}}
    arr = np.array(curve)
    peak = np.maximum.accumulate(arr)
    mdd = float((arr / peak - 1.0).min())
    total = arr[-1] - 1.0
    btotal = bench_curve[-1] - 1.0
    yrs = len(arr) / 252.0
    cagr = (arr[-1] ** (1 / yrs) - 1.0) if yrs > 0 and arr[-1] > 0 else 0.0
    vol = float(np.std(np.diff(arr) / arr[:-1]) * np.sqrt(252)) if len(arr) > 2 else 0.0
    return {
        "dates": used_dates, "curve": curve, "bench": bench_curve,
        "stats": {
            "total_return": round(float(total), 4),
            "bench_return": round(float(btotal), 4),
            "excess": round(float(total - btotal), 4),
            "cagr": round(float(cagr), 4), "vol": round(vol, 4),
            "maxdd": round(mdd, 4),
            "sharpe": round(float(cagr / vol), 3) if vol > 1e-6 else None,
            "days": len(arr),
        },
    }


# ------------------------------------------------------------------ 反事实改进
def build_improvements(evals: List[dict]) -> List[dict]:
    """基于历史结论做条件切分统计：给出「如果当时…会怎样」的量化改进建议。"""
    out: List[dict] = []
    done = [e for e in evals if e["verdict"] in ("RIGHT", "WRONG", "NEUTRAL")]
    if len(done) < 30:
        return out

    def stat(subset: List[dict]) -> dict:
        if not subset:
            return {"n": 0}
        ex = np.array([e["ex_ew"] for e in subset])
        return {
            "n": len(subset),
            "win_rate": round(float((ex >= C.REVIEW["outperf_win"]).mean()), 3),
            "avg_excess": round(float(ex.mean()), 4),
        }

    base = stat(done)

    # 1) 风险分位（波动 / 量能极端程度）
    rows = []
    for lo, hi in ((0.0, 0.3), (0.3, 0.6), (0.6, 0.85), (0.85, 1.01)):
        sub = [e for e in done if e.get("crowding_pct") is not None and lo <= e["crowding_pct"] < hi]
        rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", **stat(sub)})
    worst = [r for r in rows if r["n"] >= 15]
    if worst:
        lo_row = min(worst, key=lambda r: r["bucket"])
        good = [e for e in done if e.get("crowding_pct") is not None and e["crowding_pct"] >= 0.30]
        s_good = stat(good)
        if lo_row["avg_excess"] < base["avg_excess"] and s_good["n"] >= 15:
            out.append({
                "id": "RISK_FILTER",
                "title": "剔除极端波动：风险分位处于最低档（波动/量能极端）的出手应降级为观察",
                "evidence": {"buckets": rows, "baseline": base, "filtered": s_good},
                "suggestion": "把 risk 维度分位 <0.30 的候选直接排除出结论，不作为 BUY_NOW / AMBUSH。",
                "expected_impact": f"历史样本下平均超额 {base['avg_excess']*100:+.2f}% -> {s_good['avg_excess']*100:+.2f}%，"
                                   f"胜率 {base['win_rate']:.1%} -> {s_good['win_rate']:.1%}（样本 {s_good['n']}）",
            })

    # 2) 主线分位分档
    rows = []
    for lo, hi in ((0.0, 0.4), (0.4, 0.7), (0.7, 0.9), (0.9, 1.01)):
        sub = [e for e in done if e.get("trend_pct") is not None and lo <= e["trend_pct"] < hi]
        rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", **stat(sub)})
    valid = [r for r in rows if r["n"] >= 15]
    if valid:
        best = max(valid, key=lambda r: r["avg_excess"])
        worst_r = min(valid, key=lambda r: r["avg_excess"])
        out.append({
            "id": "MAINLINE_BAND",
            "title": f"主线分位的最优区间是 {best['bucket']}，回避 {worst_r['bucket']}",
            "evidence": {"buckets": rows, "baseline": base},
            "suggestion": "把长期动量分位落在最弱档的候选排除出结论——下跌趋势中的反弹不参与。",
            "expected_impact": f"最优档平均超额 {best['avg_excess']*100:+.2f}% / 胜率 {best['win_rate']:.1%}，"
                               f"最弱档 {worst_r['avg_excess']*100:+.2f}% / 胜率 {worst_r['win_rate']:.1%}",
        })

    # 3) 环境过滤
    rows = []
    for rg in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC"):
        sub = [e for e in done if e["regime"] == rg]
        if sub:
            rows.append({"bucket": rg, **stat(sub)})
    if len(rows) >= 2:
        bad = [r for r in rows if r["n"] >= 10 and r["avg_excess"] < 0]
        if bad:
            b = min(bad, key=lambda r: r["avg_excess"])
            out.append({
                "id": "REGIME_GATE",
                "title": f"在「{b['bucket']}」环境下应降低出手频率或只保留埋伏型结论",
                "evidence": {"buckets": rows, "baseline": base},
                "suggestion": f"regime={b['bucket']} 时把 max_daily_picks 减半，并禁止 BUY_NOW。",
                "expected_impact": f"该环境历史平均超额 {b['avg_excess']*100:+.2f}%、胜率 {b['win_rate']:.1%}（样本 {b['n']}），"
                                   f"明显低于全样本 {base['avg_excess']*100:+.2f}%",
            })

    # 4) 周期准确度
    horizon_rows = []
    for H in sorted(set(e["horizon"] for e in done)):
        sub = [e for e in done if e["horizon"] == H]
        horizon_rows.append({"bucket": f"{H}日", **stat(sub)})
    err = [e for e in done if "HORIZON_ERROR" in e["attribution"]]
    if err:
        share = len(err) / len(done)
        out.append({
            "id": "HORIZON_CALIBRATE",
            "title": f"周期估计偏差占比 {share:.1%}，需要重新校准 analog 的样本窗",
            "evidence": {"horizon_buckets": horizon_rows,
                         "error_cases": len(err), "total": len(done)},
            "suggestion": "把 analog 的 K 近邻从 40 提到 60，并在风险调整项中把回撤惩罚系数从 0.6 提到 0.8（更偏好短窗口）。",
            "expected_impact": f"影响 {len(err)}/{len(done)} 条结论的窗口选择",
        })

    # 5) 入手 vs 埋伏
    rows = []
    for act in ("BUY_NOW", "AMBUSH"):
        sub = [e for e in done if e["action"] == act]
        if sub:
            rows.append({"bucket": act, **stat(sub)})
    if len(rows) == 2:
        a, b = rows[0], rows[1]
        better = a if a["avg_excess"] >= b["avg_excess"] else b
        worse = b if better is a else a
        out.append({
            "id": "ACTION_MIX",
            "title": f"「{better['bucket']}」的历史表现优于「{worse['bucket']}」",
            "evidence": {"buckets": rows},
            "suggestion": f"把每日配额向 {better['bucket']} 倾斜（如 入手:埋伏 = 3:1）。",
            "expected_impact": f"{better['bucket']} 平均超额 {better['avg_excess']*100:+.2f}%/胜率 {better['win_rate']:.1%}，"
                               f"{worse['bucket']} {worse['avg_excess']*100:+.2f}%/胜率 {worse['win_rate']:.1%}",
        })

    return out


# ------------------------------------------------------------------ 主入口
def build_review(fs: FeatureStore, conclusions: Dict[str, dict], dates: List[str],
                 fwd: dict, regime_series: Dict[str, dict],
                 book: Optional[EtfBook] = None) -> dict:
    evals: List[dict] = []
    pending = 0
    n_demoted = 0
    for d in dates:
        day = conclusions[d]
        if day.get("error"):
            continue
        # 被结论前置过滤降级为「回避」的结论，从未进入 picks，单独计数披露
        n_demoted += int(day.get("n_filtered_conclusions") or 0)
        i = fs.idx(d)
        for p in day["picks"]:
            ev = evaluate_pick(fs, day, p, i, regime_series, book=book)
            if ev is None:
                pending += 1
                continue
            evals.append(ev)

    done = [e for e in evals]
    cnt = Counter(e["verdict"] for e in done)
    total_done = len(done)
    win_rate = cnt["RIGHT"] / total_done if total_done else 0.0
    ex_arr = np.array([e["ex_ew"] for e in done]) if done else np.array([0.0])
    dd_list = [e["maxdd"] for e in done if e["maxdd"] is not None]
    dd_arr = np.array(dd_list) if dd_list else np.array([0.0])

    def _group_by_year(items: List[dict]) -> dict:
        """分年度统计——用于暴露「是不是只有某一年有效」这类稳健性问题。"""
        g = defaultdict(list)
        for e in items:
            g[e["date"][:4]].append(e)
        out = {}
        for k, v in sorted(g.items()):
            a = np.array([x["ex_ew"] for x in v])
            out[k] = {
                "n": len(v),
                "win_rate": round(float((a >= C.REVIEW["outperf_win"]).mean()), 3),
                "avg_excess": round(float(a.mean()), 4),
            }
        return out

    def group(key: str) -> dict:
        g = defaultdict(list)
        for e in done:
            g[e[key]].append(e)
        out = {}
        for k, v in g.items():
            a = np.array([x["ex_ew"] for x in v])
            out[str(k)] = {
                "n": len(v),
                "win_rate": round(float((a >= C.REVIEW["outperf_win"]).mean()), 3),
                "avg_excess": round(float(a.mean()), 4),
                "avg_maxdd": round(float(np.mean([x["maxdd"] for x in v if x["maxdd"] is not None]) or 0), 4),
            }
        return out

    # 归因分布
    attr_counter: Counter = Counter()
    succ_counter: Counter = Counter()
    for e in done:
        for a in e["attribution"]:
            if e["verdict"] == "WRONG":
                attr_counter[a] += 1
            elif e["verdict"] == "RIGHT":
                succ_counter[a] += 1
    wrong_n = cnt["WRONG"] or 1

    # 月度节奏
    monthly = defaultdict(list)
    for e in done:
        monthly[e["date"][:7]].append(e)
    monthly_rows = []
    for m in sorted(monthly):
        v = monthly[m]
        a = np.array([x["ex_ew"] for x in v])
        monthly_rows.append({
            "month": m, "n": len(v),
            "win_rate": round(float((a >= C.REVIEW["outperf_win"]).mean()), 3),
            "avg_excess": round(float(a.mean()), 4),
        })

    # 典型案例：优先展示「能真实结算」的结论，并用 ETF 口径的收益，
    # 否则案例里写着的收益是买不到的指数收益，会误导执行。
    def _pick_metric(e: dict):
        if e.get("settled") and e.get("etf_ex_bench") is not None:
            return e["etf_ex_bench"]
        return e["ex_ew"]

    wrong_sorted = sorted([e for e in done if e["verdict"] == "WRONG"],
                          key=_pick_metric)
    right_sorted = sorted([e for e in done if e["verdict"] == "RIGHT"],
                          key=lambda x: -_pick_metric(x))
    # 可结算的排前面
    wrong_sorted.sort(key=lambda e: 0 if e.get("settled") else 1)
    right_sorted.sort(key=lambda e: 0 if e.get("settled") else 1)

    def case(e: dict) -> dict:
        use_etf = bool(e.get("settled") and e.get("etf_ret") is not None)
        return {
            "date": e["date"], "name": e["name"], "action": e["action"],
            "regime": e["regime"], "horizon": e["horizon"],
            "composite": e["composite"], "dims": e["dims"],
            "ret": e["etf_ret"] if use_etf else e["ret"],
            "ex_ew": e["etf_ex_bench"] if use_etf else e["ex_ew"],
            "maxdd": e["etf_maxdd"] if use_etf else e["maxdd"],
            "basis": "ETF" if use_etf else "指数",
            "etf": e.get("etf") if use_etf else None,
            "attribution": [ATTR_LABEL.get(a, a) for a in e["attribution"]]
            if e["verdict"] == "WRONG"
            else [SUCCESS_LABEL.get(a, a) for a in e["attribution"]],
            "attr_codes": e["attribution"],
        }

    pf = simulate_portfolio(fs, evals)

    # ================================================================ ETF 结算口径
    settlement: dict = {}
    if book is not None:
        settled = [e for e in evals if e.get("settled")]
        trades = [{
            "date": e["date"], "sw_name": e["name"], "horizon": e["horizon"],
            "composite": e["composite"], "net_ret": e.get("etf_ret"),
            "ex_bench": e.get("etf_ex_bench"), "maxdd": e.get("etf_maxdd"),
            "sell_date": e.get("etf_sell"), "etf_sid": e.get("etf_code"),
            "action": e.get("action"), "reason": e.get("etf_reason"),
        } for e in settled]

        # ---- 数据末端「已买入、未结算」的结论（open edge）----
        # settle_trade 对卖出日（T+1+H）超出数据末端的结论返回 None，
        # 这些结论因此整体缺席 evals / settled。但 T+1 的买入在现实中
        # 已经发生，不能因为「周期还没走完」就当没买过——否则曲线右端
        # 只有 S 没有 B、持仓被人为清零。这里把它们补进组合模拟：
        # B 照常入账、持仓在场逐日按收盘价计值、无卖出事件；
        # 单笔胜率等统计（下方 ex/ret 数组、by_year、by_phase）仍只用
        # 完整结算的 trades，口径不被未实现收益污染。
        settled_keys = {(e["date"], e["name"]) for e in settled}
        open_edge: List[dict] = []
        for d in dates:
            day_c = conclusions.get(d) or {}
            if day_c.get("error"):
                continue
            i0 = book.idx.get(d)
            if i0 is None or i0 + 1 >= len(book.dates):
                continue
            b_i = i0 + 1
            for p in day_c["picks"]:
                if (d, p["name"]) in settled_keys:
                    continue
                H = int(p.get("horizon", {}).get("horizon") or 0)
                if H <= 0:
                    continue
                # 注意：p["etf_code"] 是 6 位展示码，账簿键是全码（1.515880），
                # 必须经 etf_of 按买入日解析；展示码只作后备提示。
                sid = book.etf_of(p["name"], book.dates[b_i])
                if not sid and p.get("etf_code"):
                    hits = [k for k in book.series
                            if k.split(".")[-1] == str(p["etf_code"])]
                    sid = hits[0] if len(hits) == 1 else None
                if not sid or not book.open_(sid, b_i):
                    continue
                open_edge.append({
                    "date": d, "sw_name": p["name"], "horizon": H,
                    "composite": p.get("composite", 0),
                    "etf_sid": sid, "action": p.get("action"),
                    "open_edge": True,
                })
        # 组合模拟用「完整结算 + 在途」的全集；单笔统计口径维持 settled-only
        trades_exec = trades + open_edge

        RC = C.RISK_CONTROL
        PF = C.PORTFOLIO
        pf_etf = simulate_etf_portfolio(book, trades_exec, start_date=dates[0],
                                        max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                                        risk_parity=RC["risk_parity"],
                                        rp_window=RC["rp_window"],
                                        vol_target=RC["vol_target"],
                                        vol_window=RC["vol_window"],
                                        vol_floor=RC["vol_floor"],
                                        cash_yield=RC["cash_yield"],
                                        score_tilt=PF["score_tilt"],
                                        max_new_per_day=PF["max_new_per_day"])
        # 对照：未加风控的原始口径（等权 + 满仓），保留以便复核改进幅度
        pf_base = simulate_etf_portfolio(book, trades_exec, start_date=dates[0],
                                         max_holdings=C.CLASSIFY["max_daily_picks"] * 4)
        # 暴露下限敏感性：把「降回撤」与「年化」的取舍显式摆出来
        floor_scan = []
        for f in C.RISK_CONTROL_FLOORS:
            if abs(f - RC["vol_floor"]) < 1e-9:
                s = pf_etf["stats"]
            else:
                s = simulate_etf_portfolio(
                    book, trades_exec, start_date=dates[0],
                    max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                    risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
                    vol_target=RC["vol_target"], vol_window=RC["vol_window"],
                    vol_floor=f, cash_yield=RC["cash_yield"])["stats"]
            e = s.get("effective", {})
            floor_scan.append({
                "floor": f,
                "cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s["sharpe"],
                "calmar": s["calmar"], "underwater": s["max_underwater_days"],
                "monthly_win_rate": s["monthly_win_rate"],
                "eff_cagr": e.get("cagr"), "eff_maxdd": e.get("maxdd"),
                "eff_sharpe": e.get("sharpe"), "eff_calmar": e.get("calmar"),
                "eff_underwater": e.get("max_underwater_days"),
                "eff_monthly_win_rate": e.get("monthly_win_rate"),
                "avg_exposure": s.get("avg_exposure"),
            })
        # 集中度上限扫描：两层架构下多条细分赛道结论会共用同一只一级行业 ETF 作代理，
        # 若不设槽位上限，就等于在这只 ETF 上反复加码。这里把「压回撤要付多少年化」
        # 显式摆出来——实验室结论是它**不构成 Pareto 改进**，因此不进生产配置。
        conc_scan = []
        for cap in (None, 4, 3, 2):
            s_c = simulate_etf_portfolio(
                book, trades_exec, start_date=dates[0],
                max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
                vol_target=RC["vol_target"], vol_window=RC["vol_window"],
                vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
                score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"],
                max_per_sid=cap)
            st_c = s_c["stats"]
            ex_c = np.array([e["ex"] for e in s_c["events"]
                             if e["k"] == "S" and e.get("ex") is not None])
            conc_scan.append({
                "cap": cap,
                "cagr": st_c.get("cagr"), "maxdd": st_c.get("maxdd"),
                "sharpe": st_c.get("sharpe"), "calmar": st_c.get("calmar"),
                "win_excess": round(float((ex_c >= C.REVIEW["outperf_win"]).mean()), 3)
                if ex_c.size else None,
                "n": st_c.get("executed_trades"),
                "blocked": st_c.get("blocked_by_sid_cap"),
                "avg_holdings": st_c.get("avg_holdings"),
            })

        # 对照组：加「市场趋势门控」（复盘结论：原版在 2021-09 后水下 815 日）
        pf_gated = simulate_etf_portfolio(book, trades_exec, start_date=dates[0],
                                          max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                                          market_gate=True)

        # 口径 B：执行结论自带的失效条件（连续 3 日跌破 MA20 提前退出）
        trades_early = [{
            "date": e["date"], "sw_name": e["name"], "horizon": e["horizon"],
            "composite": e["composite"], "net_ret": e.get("etf_early_ret"),
            "ex_bench": e.get("etf_early_ex_bench"), "maxdd": e.get("etf_early_maxdd"),
            "sell_date": e.get("etf_early_sell"), "etf_sid": e.get("etf_early_sid"),
            "reason": e.get("etf_early_reason"),
        } for e in settled if e.get("etf_early_ret") is not None]
        pf_early = simulate_etf_portfolio(
            book, trades_early, start_date=dates[0],
            max_holdings=C.CLASSIFY["max_daily_picks"] * 4)
        ex_arr_b = np.array([t["ex_bench"] for t in trades_early
                             if t["ex_bench"] is not None])
        ret_arr_b = np.array([t["net_ret"] for t in trades_early
                              if t["net_ret"] is not None])
        early_exit_share = (
            float(np.mean([1.0 if e.get("etf_early_exited") else 0.0
                           for e in settled])) if settled else None)

        def _yearly(subset: List[dict]) -> dict:
            g2 = defaultdict(list)
            for t in subset:
                g2[t["date"][:4]].append(t)
            out2 = {}
            for k, v in sorted(g2.items()):
                a = np.array([x["net_ret"] for x in v if x["net_ret"] is not None])
                e2 = np.array([x["ex_bench"] for x in v if x["ex_bench"] is not None])
                out2[k] = {
                    "n": len(v),
                    "win_rate": round(float((e2 >= C.REVIEW["outperf_win"]).mean()), 3) if e2.size else None,
                    "avg_ret": round(float(a.mean()), 4) if a.size else None,
                    "avg_excess": round(float(e2.mean()), 4) if e2.size else None,
                    "avg_maxdd": round(float(np.mean(
                        [x["maxdd"] for x in v if x["maxdd"] is not None] or [0])), 4),
                }
            return out2

        # 方向判断胜率（结算口径）：跑赢基准 ETF / 绝对正收益
        ex_arr_e = np.array([t["ex_bench"] for t in trades
                             if t["ex_bench"] is not None])
        ret_arr_e = np.array([t["net_ret"] for t in trades
                              if t["net_ret"] is not None])
        win_excess = float((ex_arr_e >= C.REVIEW["outperf_win"]).mean()) if ex_arr_e.size else None
        win_abs = float((ret_arr_e > 0).mean()) if ret_arr_e.size else None

        # 平均持仓时间（信号口径：全部可结算结论；对照口径 B：执行失效条件后）
        held_sig = [int(e["etf_held"]) for e in settled
                    if e.get("etf_held") is not None]
        held_early = [int(e["etf_early_days"]) for e in settled
                      if e.get("etf_early_days") is not None]

        # 分年度（结算口径）
        by_year_e = _yearly(trades)

        # 牛熊分段（按基准 ETF 历史高点回撤客观划分）
        phases = market_phases(book)
        by_phase = stats_by_phase(trades, phases)

        # 覆盖度演进：每年末可交易行业数
        coverage = []
        seen_years = sorted(set(d[:4] for d in book.dates))
        for y in seen_years:
            ys = [d for d in book.dates if d.startswith(y)]
            if not ys:
                continue
            coverage.append({"year": y, "tradeable": len(book.tradeable.get(ys[-1], []))})

        signal_only = [e for e in evals if not e.get("settled")]
        settlement = {
            "basis": (
                "收益以真实可交易行业 ETF 结算：信号日 T 收盘后出结论，T+1 开盘买入，"
                f"持有建议周期后收盘卖出；双边成本 {C.ROUND_TRIP_COST*100:.2f}%"
                "（佣金万二 + 滑点万五，ETF 免印花税）。"
                "ETF 上市之前的行业不参与结算，不用行业指数回填。"
                f"主口径含组合风控：持仓按 1/σ 分配风险预算"
                f"（σ 用 {RC['rp_window']} 日已实现波动），"
                f"组合已实现波动高于 {RC['vol_target']:.0%} 时按比例降仓、"
                f"最低 {RC['vol_floor']:.0%}，未投入部分按 {RC['cash_yield']:.1%} 计现金收益。"
                f"组合加权与建仓节奏：持仓权重 ∝ (综合分/50)^{PF['score_tilt']}，"
                f"每日最多新建 {PF['max_new_per_day']} 个仓位。"
                f"结论前置过滤：剔除 {n_demoted} 条「事前即自相矛盾或追高」的结论"
                "（120日相对行业等权超额为负 / 价格位于近60日区间顶部 5%），"
                "它们被降级为回避并保留在回避清单里，可逐条复核。"
            ),
            # 风控开关与「回撤 vs 年化」的显式取舍
            "risk_control": {
                "enabled": bool(RC["risk_parity"] or RC["vol_target"]),
                "rule": ("风险平价权重（1/σ）+ 波动目标降仓"),
                "why": ("主口径最深回撤 -39.5% 发生在 2021-09→2022-04。归因显示根因"
                        "不是持仓结构（回撤期组合已接近容量上限、相当分散），"
                        "而是高波动期仍然满仓。反事实扫描里，移动止损、回撤熔断、"
                        "提高信号门槛、限制同行业建仓都让回撤更差——因为它们不降低"
                        "总暴露，只把仓位在下跌中反复洗牌；只有波动目标能同时"
                        "降低回撤且不牺牲年化。"),
                "invariant": ("两条规则只改变持仓间权重与总暴露，不改变任何一笔"
                              "买卖的时点与标的，因此方向判断胜率与成交笔数不变。"),
                "config": {
                    "rp_window": RC["rp_window"], "vol_target": RC["vol_target"],
                    "vol_window": RC["vol_window"], "vol_floor": RC["vol_floor"],
                    "cash_yield": RC["cash_yield"],
                },
                "floor_scan": floor_scan,
            },
            # 组合执行：权重与建仓节奏（不改成交集合的时点与标的）
            "portfolio": {
                "score_tilt": PF["score_tilt"],
                "max_new_per_day": PF["max_new_per_day"],
                "rule": ("持仓权重 ∝ (综合分/50)^tilt（替代等权），"
                         f"tilt={PF['score_tilt']}；每日最多新建 "
                         f"{PF['max_new_per_day']} 个仓位（总容量上限不变）。"),
                "why": ("实验台（engine/lab_opt.py）对『选的更准』与『配的更稳』做了分离测试："
                        "分数加权对成交集合零影响，只让高分结论占更大权重；"
                        "每日建仓上限解决的是信号密集日质量参差、资金被摊薄的问题。"
                        "tilt 在 0.25~0.5 之间表现一致（宽平台，不是拟合出来的尖峰）。"),
                "invariant": "两条规则都不改变任何一笔买卖的时点与标的。",
            },
            # 集中度上限：只作对照展示，不进生产配置（不构成 Pareto 改进）
            "concentration": {
                "scan": conc_scan,
                "cap": None,
                "rule": ("同一只 ETF 同时最多占 N 个槽位；cap=null 表示不限制"
                         "（当前生产配置）。"),
                "why": ("容量上限只约束总槽位数（32），不约束槽位是否押在同一只 ETF 上；"
                        "两层架构下多条细分赛道结论共用同一只一级行业 ETF 作代理，"
                        "于是同一只 ETF 会被反复加码（当前配置下 328 笔买入落在 25 只 "
                        "ETF 上，最集中的一只被买 63 次）。"),
                "verdict": ("实验台（engine/lab_opt6.py）逐档实测后**未采纳**：设上限确实能"
                            "压回撤（同 ETF≤2 时最大回撤 -29.91%→-26.46%、方向胜率 "
                            "42.7%→43.4%），但年化要付 2.6pp 的代价（12.57%→9.97%），"
                            "不满足「三项同时不劣」的判据。此处如实列出，作为"
                            "「若你更看重回撤、愿意让出年化」的可选档位。"),
            },
            # 结论前置过滤：被降级为「回避」的结论，及其事前理由
            "conclusion_filter": {
                "rs120_min": C.CONCLUSION_FILTER["rs120_min"],
                "pos60_max": C.CONCLUSION_FILTER["pos60_max"],
                "n_demoted": n_demoted,
                "rule": ("不给两类结论：(1) 120日相对行业等权超额为负——这与结论自带的"
                         "失效条件直接冲突，事前就该作废；(2) 价格位于近60日区间顶部 5%"
                         "——属追高。"),
                "evidence": ("实验台实测：被剔除的 (1) 类结论胜率仅 4.8%、平均超额 -7.91%；"
                             "(2) 类剔除后组合年化 +2.9pp、最大回撤 -3.2pp（阈值 0.88~0.98 "
                             "为宽平台）。同时验证了 pos250 / 60日回撤深度虽然分位胜率 "
                             "分离度更大，但做成硬规则会让年化与回撤同时劣化，因此未采纳。"),
            },
            # 对照：未加风控的原始口径（等权 + 满仓 + 无择时）
            "baseline": {                "label": "对照：原始口径（等权 + 满仓，未加风控）",
                "stats": pf_base["stats"],
                "curve": {"dates": pf_base["dates"], "nav": pf_base["nav"],
                          "bench": pf_base["bench"]},
            },
            "bench_etf": book.listing.get(book.bench_etf or "", {}).get("name"),
            "n_settled": len(settled),
            "n_signal_only": len(signal_only),
            "n_total": len(evals),
            # 数据末端「已买入、未结算」的结论数：已进入组合模拟（B 在、S 未到），
            # 不参与单笔胜率统计
            "n_open_edge": len(open_edge),
            "win_rate_excess": round(win_excess, 4) if win_excess is not None else None,
            "win_rate_abs": round(win_abs, 4) if win_abs is not None else None,
            "avg_ret": round(float(ret_arr_e.mean()), 4) if ret_arr_e.size else None,
            "avg_excess": round(float(ex_arr_e.mean()), 4) if ex_arr_e.size else None,
            "avg_maxdd": round(float(np.mean(
                [t["maxdd"] for t in trades if t["maxdd"] is not None] or [0])), 4),
            "stats": pf_etf["stats"],
            "curve": {"dates": pf_etf["dates"], "nav": pf_etf["nav"],
                      "bench": pf_etf["bench"]},
            # 平均持仓时间：组合口径（真实成交的那批）与信号口径（全部可结算结论）
            "holding": {
                "label": "平均持仓时间",
                "portfolio": holding_stats(pf_etf["dates"], pf_etf["events"]),
                "per_signal": {
                    "n": len(held_sig),
                    "avg_days": round(float(np.mean(held_sig)), 1) if held_sig else None,
                    "median_days": (round(float(np.median(held_sig)), 1)
                                    if held_sig else None),
                    "min_days": int(min(held_sig)) if held_sig else None,
                    "max_days": int(max(held_sig)) if held_sig else None,
                },
                "early": {
                    "label": "若执行失效条件提前退出",
                    "avg_days": round(float(np.mean(held_early)), 1) if held_early else None,
                    "n": len(held_early),
                },
                "note": ("平均持仓时间按「买入日 → 卖出日」的交易日间隔计，"
                         "与曲线上的 B/S 点同一批成交；组合口径只统计真实建仓的那批，"
                         "信号口径统计全部可结算结论（含因容量上限未建仓的）。"),
            },
            # 曲线上逐笔成交事件（B/S 点）：与 curve 同一套成交价与成本口径
            "events": pf_etf["events"],
            "etf_names": pf_etf["etf_names"],
            # 逐日仓位（辅助轴）：主口径与对照口径各一条，与 curve.dates 等长
            "position": {
                "label": "组合仓位（持仓只数 / 容量 × 波动目标暴露）",
                "dates": pf_etf["dates"],
                "max_holdings": C.CLASSIFY["max_daily_picks"] * 4,
                "holdings": pf_etf["holdings"],
                "exposure": pf_etf["exposure"],
                "invested": pf_etf["invested"],
                "baseline_invested": pf_base["invested"],
                "avg_invested": pf_etf["stats"].get("avg_invested"),
                "baseline_avg_invested": pf_base["stats"].get("avg_invested"),
                "max_holdings_used": pf_etf["stats"].get("max_holdings_used"),
            },
            # 对照组：加市场趋势门控（复盘驱动改进）
            # 口径 A：固定持有到建议周期结束（当前主口径，见上层 stats/curve）
            # 口径 B：执行结论自带的失效条件
            "early_exit": {
                "label": "执行结论自带的失效条件（收盘跌破 MA20 即退出）",
                "basis": ("买卖价与成本同主口径；唯一差别是当收盘价跌破 MA20（结论里"
                          "已写明的失效条件之一）时，次日开盘平仓，不再死扛到周期结束。"),
                "stats": pf_early["stats"],
                "curve": {"dates": pf_early["dates"], "nav": pf_early["nav"],
                          "bench": pf_early["bench"]},
                "n": len(trades_early),
                "win_rate_excess": round(float((ex_arr_b >= C.REVIEW["outperf_win"]).mean()), 4)
                if ex_arr_b.size else None,
                "win_rate_abs": round(float((ret_arr_b > 0).mean()), 4)
                if ret_arr_b.size else None,
                "avg_ret": round(float(ret_arr_b.mean()), 4) if ret_arr_b.size else None,
                "avg_excess": round(float(ex_arr_b.mean()), 4) if ex_arr_b.size else None,
                "avg_maxdd": round(float(np.mean(
                    [t["maxdd"] for t in trades_early if t["maxdd"] is not None] or [0])), 4),
                "exit_share": round(early_exit_share, 3) if early_exit_share is not None else None,
                "avg_held_days": round(float(np.mean(
                    [e["etf_early_days"] for e in settled
                     if e.get("etf_early_days") is not None] or [0])), 1),
                "by_year": _yearly(trades_early),
            },
            "gated": {
                "label": "对照：市场趋势门控（基准 ETF 站上 MA200 才建仓）",
                "stats": pf_gated["stats"],
                "curve": {"dates": pf_gated["dates"], "nav": pf_gated["nav"],
                          "bench": pf_gated["bench"]},
            },
            "by_year": by_year_e,
            "by_phase": by_phase,
            "coverage": coverage,
            "etf_universe": {sw: book.listing.get(sid, {}).get("name", sid)
                             for sw, sid in book.sw_to_etf.items()},
            "etf_listing": {sw: book.listing.get(sid, {}).get("start")
                            for sw, sid in book.sw_to_etf.items()},
        }

    # 最近 12 个交易日的逐日复盘
    recent = []
    for d in dates[-12:]:
        day = conclusions[d]
        if day.get("error"):
            continue
        evs = [e for e in done if e["date"] == d]
        pend = [p for p in day["picks"] if not any(p["code"] == e["code"] for e in evs)]
        if evs:
            a = np.array([x["ex_ew"] for x in evs])
            vr = "RIGHT" if a.mean() >= C.REVIEW["outperf_win"] else (
                "WRONG" if a.mean() <= C.REVIEW["outperf_loss"] else "NEUTRAL")
        else:
            vr = "PENDING"
        recent.append({
            "date": d, "regime": day["regime"], "summary": day.get("summary", ""),
            "verdict": vr,
            "avg_excess": round(float(np.mean([x["ex_ew"] for x in evs])), 4) if evs else None,
            "picks": [{
                "name": e["name"], "action": e["action"], "horizon": e["horizon"],
                "ret": e["ret"], "ex_ew": e["ex_ew"], "verdict": e["verdict"],
                "attribution": [ATTR_LABEL.get(x, x) if e["verdict"] == "WRONG" else SUCCESS_LABEL.get(x, x)
                                for x in e["attribution"]],
            } for e in evs],
            "pending": [{"name": p["name"], "action": p["action"],
                         "horizon": p["horizon"]["horizon"]} for p in pend],
        })

    return {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "settlement": settlement,
        "overview": {
            "evaluated": total_done, "pending": pending,
            "right": cnt["RIGHT"], "wrong": cnt["WRONG"], "neutral": cnt["NEUTRAL"],
            "win_rate": round(win_rate, 4),
            "avg_excess": round(float(ex_arr.mean()), 4),
            "avg_maxdd": round(float(dd_arr.mean()), 4),
            "date_range": [dates[0], dates[-1]],
            "by_action": group("action"),
            "by_regime": group("regime"),
            "by_horizon": group("horizon"),
            "by_year": _group_by_year(done),
        },
        "attribution": {
            "wrong_distribution": [
                {"code": k, "label": ATTR_LABEL.get(k, k), "count": v,
                 "share": round(v / wrong_n, 3)}
                for k, v in attr_counter.most_common()
            ],
            "success_distribution": [
                {"code": k, "label": SUCCESS_LABEL.get(k, k), "count": v}
                for k, v in succ_counter.most_common()
            ],
        },
        "improvements": build_improvements(done),
        "cases": {
            "worst": [case(e) for e in wrong_sorted[:6]],
            "best": [case(e) for e in right_sorted[:6]],
        },
        "monthly": monthly_rows,
        "portfolio": pf,
        "recent": recent,
        "labels": {"attr": ATTR_LABEL, "success": SUCCESS_LABEL},
    }
