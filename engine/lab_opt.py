# -*- coding: utf-8 -*-
"""信号质量 × 组合权重的联合优化实验台（lab）。

与 lab_dd.py 的分工：
    lab_dd  —— 回答「仓位/退出规则能否降回撤」，已收敛为 rp + vol_target；
    lab_opt —— 回答「能不能同时把年化、方向胜率、回撤三个指标一起改善」。

本台子把「选的更准」与「配的更稳」分开测：
    信号侧：综合分门槛 / 每日取前 K / 动作过滤 / 环境过滤 / 过度延伸过滤
    组合侧：风险平价 / 分数加权 / 动作加权
    退出侧：到期后再看信号是否还在，强则延展（让盈利跑）

判定口径：一个改动只有在 cagr、胜率不降且 maxdd 不升的前提下才算 Pareto 改进。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, perf_stats, load_etf_panel  # noqa: E402

HIST = f"{C.OUT_DIR}/history.json"
FEAT = f"{C.CACHE_DIR}/features.npz"
TR_CACHE = f"{C.CACHE_DIR}/lab_opt_trades.json"

# 逐笔快照的特征（在建仓当日可得，绝不含未来信息）
SNAP_FIELDS = [
    "dist_ma20", "dist_ma60", "dist_ma120", "dist_ma250", "ma20_slope",
    "pos60", "pos250", "dd_from_high60", "ret_20", "ret_60", "ret_120",
    "ret_250", "rs_60", "rs_120", "vol20", "vol_ratio", "amount_ratio",
    "up_vol_share", "maxdd_60", "sharpe20", "consec_up", "r5_over_r20",
]


def load_history() -> List[dict]:
    with open(HIST, encoding="utf-8") as f:
        return json.load(f)["days"]


def load_fs():
    from feature_store import FeatureStore
    return FeatureStore.load(FEAT)


def build_trades(book: EtfBook, fs, days: List[dict],
                 cache: str = TR_CACHE, rebuild: bool = False) -> List[dict]:
    """把每日结论结算成逐笔交易，并附上建仓当日的特征快照。"""
    if os.path.exists(cache) and not rebuild:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    trades: List[dict] = []
    for d in days:
        for p in d["picks"]:
            code = p.get("code")
            if code is None or code not in fs.data:
                continue
            H = int(p["horizon"])
            tf = book.settle_trade(p["name"], d["date"], H)
            if not tf:
                continue
            i = fs.idx(d["date"])
            snap = {}
            for f in SNAP_FIELDS:
                v = float(fs.series(code, f)[i])
                snap[f] = None if v != v else round(v, 6)
            trades.append({
                "date": d["date"], "sw_name": p["name"], "code": code,
                "horizon": H, "composite": p.get("composite", 0),
                "action": p.get("action"), "level": p.get("level"),
                "regime": d.get("regime"), "dims": p.get("dims") or {},
                "net_ret": tf["net_ret"], "ex_bench": tf["ex_bench"],
                "maxdd": tf["maxdd"], "sell_date": tf["sell_date"],
                "buy_date": tf["buy_date"], "etf_sid": tf["etf"],
                "etf_name": tf["etf_name"], "held_days": tf["held_days"],
                "feat": snap,
            })
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False)
    return trades


def _sigma(book: EtfBook, sid: str, k: int, window: int) -> float:
    vals = [book.close(sid, j) for j in range(max(0, k - window), k)]
    vals = [v for v in vals if v]
    if len(vals) < 11:
        return 0.25
    rr = np.diff(vals) / np.array(vals[:-1])
    s = float(np.std(rr) * np.sqrt(252))
    return s if s > 1e-4 else 0.25


# ------------------------------------------------------------------ 模拟器
def sim(book: EtfBook, trades: List[dict], *,
        max_holdings: int = 32,
        risk_parity: bool = True, rp_window: int = 60,
        vol_target: Optional[float] = 0.16, vol_window: int = 60,
        vol_floor: float = 0.80, cash_yield: float = 0.015,
        start_date: Optional[str] = None,
        # --- 信号侧开关
        min_composite: Optional[float] = None,
        max_per_day: Optional[int] = None,
        actions: Optional[Set[str]] = None,
        skip_regimes: Optional[Set[str]] = None,
        max_dist_ma250: Optional[float] = None,
        max_pos60: Optional[float] = None,
        min_pos250: Optional[float] = None,
        min_maxdd60: Optional[float] = None,
        min_rs120: Optional[float] = None,
        max_vol20: Optional[float] = None,
        # --- 权重侧开关
        weight_by: Optional[str] = None,      # "score" | "action" | None
        score_tilt: float = 1.0,
        # --- 集中度上限（组合侧，不改变任何一笔买卖的时点；
        #     只在「同一只 ETF / 同一行业已被占用多少槽位」这一点上影响哪个结论进得来）
        max_per_sid: Optional[int] = None,
        max_per_sw: Optional[int] = None,
        # --- 退出侧开关
        extend: bool = False, extend_days: int = 60, extend_max: int = 2,
        extend_ma: int = 20,
        collect_holdings: bool = False) -> dict:
    """与生产同源的组合模拟器 + 一批可开关的信号/权重/退出规则。

    所有开关只作用在「哪些结论进入组合」「持仓间怎么分权」「到期后怎么处理」，
    成交价、成本、现金收益口径与生产完全一致。
    """
    dates = book.dates
    n = len(dates)
    s_i = max(book.idx.get(start_date, 0) if start_date else 0, 1)

    plan: Dict[int, List[dict]] = defaultdict(list)
    n_filtered = 0
    n_skipped = 0
    for t in trades:
        i0 = book.idx.get(t["date"])
        if i0 is None or i0 < s_i:
            continue
        sc = float(t.get("composite") or 0)
        if min_composite is not None and sc < min_composite:
            n_filtered += 1
            continue
        if actions is not None and t.get("action") not in actions:
            n_filtered += 1
            continue
        if skip_regimes and t.get("regime") in skip_regimes:
            n_filtered += 1
            continue
        ft = t.get("feat") or {}
        if max_dist_ma250 is not None and (ft.get("dist_ma250") or 0) > max_dist_ma250:
            n_filtered += 1
            continue
        if max_pos60 is not None and (ft.get("pos60") if ft.get("pos60") is not None else 1) > max_pos60:
            n_filtered += 1
            continue
        if min_pos250 is not None and (ft.get("pos250") if ft.get("pos250") is not None else 0) < min_pos250:
            n_filtered += 1
            continue
        if min_maxdd60 is not None and (ft.get("maxdd_60") if ft.get("maxdd_60") is not None else -1) < min_maxdd60:
            n_filtered += 1
            continue
        if min_rs120 is not None and (ft.get("rs_120") or 0) < min_rs120:
            n_filtered += 1
            continue
        if max_vol20 is not None and (ft.get("vol20") or 0) > max_vol20:
            n_filtered += 1
            continue
        H = int(t["horizon"])
        b_i = i0 + 1
        s_i_ = book.idx.get(t["sell_date"], i0 + 1 + H) if t.get("sell_date") else i0 + 1 + H
        if b_i >= n or s_i_ >= n or s_i_ <= b_i:
            continue
        sid = t.get("etf_sid") or book.etf_of(t["sw_name"], dates[b_i])
        if not sid:
            n_skipped += 1
            continue
        plan[b_i].append({"sw": t["sw_name"], "end": s_i_, "secid": sid,
                          "score": sc, "horizon": H, "action": t.get("action"),
                          "code": t.get("code")})

    holdings: List[dict] = []
    nav, nav_dates = [1.0], [dates[s_i - 1]]
    bench_nav = [1.0]
    cur = bcur = 1.0
    bench_sid = book.bench_etf
    prev_close: Dict[str, float] = {}
    events: List[dict] = []
    n_exec = blocked_cap = blocked_sid = blocked_sw = 0
    hold_count: List[int] = []
    exposure_log: List[float] = []

    rf = cash_yield / 252.0
    ret_hist: List[float] = []

    def _bench_ret(fi: int, ti: int) -> Optional[float]:
        if not bench_sid:
            return None
        bb, bs = book.open_(bench_sid, fi), book.close(bench_sid, ti)
        if not bb or not bs or bb <= 0:
            return None
        return bs / bb - 1.0 - C.ROUND_TRIP_COST

    for k in range(s_i, n):
        d = dates[k]
        day_events: List[dict] = []
        day_rets: List[tuple] = []

        kept = []
        for h in holdings:
            c = book.close(h["secid"], k)
            p = prev_close.get(h["sid"])
            r_i = (c / p - 1.0) if (c and p) else 0.0
            if risk_parity:
                h["sigma"] = _sigma(book, h["secid"], k, rp_window)
                h["w"] = 1.0 / max(h["sigma"], 0.05)
            else:
                h["w"] = 1.0
            if weight_by == "score":
                # 与 settlement.simulate_etf_portfolio 严格同式：
                # 以 50 分为基准归一（score=50 时权重系数为 1.0），避免把绝对分位放大成隐性杠杆
                h["w"] *= max(h.get("score") or 50.0, 1.0) ** score_tilt / (50.0 ** score_tilt)
            elif weight_by == "action":
                h["w"] *= 1.35 if h.get("action") == "BUY_NOW" else 0.8
            day_rets.append((r_i, h["w"]))

            if c:
                h["peak"] = max(h.get("peak", c), c)

            exit_now = h["end"] <= k
            # 到期延展：信号还在（收盘站上 MA20 且仍为正收益）则再持一段，让盈利跑
            if exit_now and extend and (h.get("ext_n") or 0) < extend_max:
                c0 = book.close(h["secid"], k)
                ma = book.ma_at(h["secid"], k, extend_ma)
                if c0 and ma and c0 > ma and c0 > h.get("buy_price", c0):
                    h["end"] = k + extend_days
                    h["ext_n"] = (h.get("ext_n") or 0) + 1
                    exit_now = False
            if exit_now:
                bpx = h.get("buy_price")
                gross = (c / bpx - 1.0) if (c and bpx) else None
                net = (gross - C.ROUND_TRIP_COST) if gross is not None else None
                bre = _bench_ret(h["buy_i"], k)
                day_events.append({
                    "k": "S", "sw": h["sw"], "sid": h["secid"],
                    "price": round(c, 4) if c else None,
                    "bd": dates[h["buy_i"]], "bpx": round(bpx, 4) if bpx else None,
                    "h": h.get("horizon"), "a": h.get("action"),
                    "ret": round(net, 5) if net is not None else None,
                    "ex": round(net - bre, 5) if (net is not None and bre is not None) else None,
                })
            else:
                kept.append(h)
        holdings = kept

        if k in plan:
            adds = sorted(plan[k], key=lambda x: -x["score"])
            if max_per_day is not None:
                adds = adds[:max_per_day]
            for item in adds:
                if len(holdings) >= max_holdings:
                    blocked_cap += 1
                    continue
                sid = item["secid"]
                if max_per_sid is not None:
                    if sum(1 for x in holdings if x["sid"] == sid) >= max_per_sid:
                        blocked_sid += 1
                        continue
                if max_per_sw is not None:
                    if sum(1 for x in holdings if x["sw"] == item["sw"]) >= max_per_sw:
                        blocked_sw += 1
                        continue
                o = book.open_(sid, k)
                if not o:
                    continue
                sigma = _sigma(book, sid, k, rp_window) if risk_parity else 0.25
                bw = (1.0 / max(sigma, 0.05)) if risk_parity else 1.0
                # 建仓当日就要带上分数权重——否则买当天用等权、次日才 tilt，
                # 与生产口径差一个交易日的权重（曾造成 4bp 级别的年化偏差）
                if weight_by == "score":
                    bw *= max(item["score"] or 50.0, 1.0) ** score_tilt / (50.0 ** score_tilt)
                elif weight_by == "action":
                    bw *= 1.35 if item.get("action") == "BUY_NOW" else 0.8
                holdings.append({
                    "sw": item["sw"], "sid": sid, "secid": sid, "end": item["end"],
                    "buy_i": k, "buy_price": o, "horizon": item["horizon"],
                    "action": item["action"], "score": item["score"], "code": item.get("code"),
                    "sigma": sigma, "peak": o, "w": bw,
                    "ext_n": 0,
                })
                n_exec += 1
                day_events.append({
                    "k": "B", "sw": item["sw"], "sid": sid, "price": round(o, 4),
                    "h": item["horizon"], "a": item["action"], "sc": round(item["score"], 1),
                })
                c = book.close(sid, k)
                if c:
                    day_rets.append((c / o - 1.0, holdings[-1]["w"]))

        prev_close = {}
        for h in holdings:
            c = book.close(h["secid"], k)
            if c:
                prev_close[h["sid"]] = c

        if day_rets:
            w_sum = sum(w for _, w in day_rets)
            r_raw = float(sum(r * w for r, w in day_rets) / w_sum) if w_sum > 0 else 0.0
        else:
            r_raw = 0.0

        expo = 1.0
        if vol_target is not None and len(ret_hist) >= 20:
            sig = float(np.std(ret_hist[-vol_window:]) * np.sqrt(252))
            if sig > 1e-6:
                expo = max(min(1.0, vol_target / sig), vol_floor)
        expo = float(np.clip(expo, 0.0, 1.0))

        r = expo * r_raw + (1.0 - expo) * rf
        cur *= (1.0 + r)
        ret_hist.append(r)
        nav.append(cur)
        nav_dates.append(d)
        hold_count.append(len(holdings))
        exposure_log.append(expo)

        if day_events:
            idx = len(nav) - 1
            for ev in day_events:
                ev["i"] = idx
            events.extend(day_events)

        if bench_sid:
            bp, bn = book.close(bench_sid, k - 1), book.close(bench_sid, k)
            if bp and bn:
                bcur *= bn / bp
        bench_nav.append(bcur)

    nav_arr = np.array(nav)
    bench_arr = np.array(bench_nav)
    stats = perf_stats(nav_arr, nav_dates, bench_arr if bench_sid else None)
    first_i = next((i for i, x in enumerate(hold_count) if x > 0), 0) + 1
    if 0 < first_i < len(nav) - 100:
        eff = perf_stats(nav_arr[first_i:], nav_dates[first_i:],
                         bench_arr[first_i:] if bench_sid else None)
        eff["start"] = nav_dates[first_i]
        stats["effective"] = eff
    stats.update({
        "executed_trades": n_exec, "filtered": n_filtered, "skipped": n_skipped,
        "blocked_by_capacity": blocked_cap, "max_holdings": max_holdings,
        "blocked_by_sid_cap": blocked_sid, "blocked_by_sw_cap": blocked_sw,
        "avg_holdings": round(float(np.mean(hold_count)), 2) if hold_count else None,
        "avg_exposure": round(float(np.mean(exposure_log)), 3) if exposure_log else None,
    })
    out = {"dates": nav_dates, "nav": [round(x, 5) for x in nav],
           "bench": [round(x, 5) for x in bench_nav], "stats": stats, "events": events}
    if collect_holdings:
        out["hold_count"] = hold_count
        out["exposure"] = exposure_log
    return out


# ------------------------------------------------------------------ 指标
def trade_stats(events: List[dict], nav_dates: List[str]) -> dict:
    """逐笔成交层面的指标（胜率 / 持仓天数），与曲线同一批事件。

    注意：事件的 i 是净值序列下标，必须用 nav_dates 映射，
    不能用 book.dates（两者差一个对齐用的首点）。
    """
    di = {d: i for i, d in enumerate(nav_dates)}
    sells = [e for e in events if e["k"] == "S"]
    rets = np.array([e["ret"] for e in sells if e.get("ret") is not None])
    exs = np.array([e["ex"] for e in sells if e.get("ex") is not None])
    held = [e["i"] - di[e["bd"]] for e in sells
            if e.get("bd") in di and e.get("i") is not None]
    return {
        "n": len(sells),
        "win_excess": float((exs >= C.REVIEW["outperf_win"]).mean()) if exs.size else None,
        "win_abs": float((rets > 0).mean()) if rets.size else None,
        "avg_ret": float(rets.mean()) if rets.size else None,
        "avg_excess": float(exs.mean()) if exs.size else None,
        "avg_held": float(np.mean(held)) if held else None,
        "med_held": float(np.median(held)) if held else None,
    }


def score(res: dict, tag: str = "") -> dict:
    s = res["stats"]
    e = s.get("effective", {})
    ts = trade_stats(res["events"], res["dates"])
    return {
        "tag": tag,
        "cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s["sharpe"],
        "calmar": s["calmar"], "underwater": s["max_underwater_days"],
        "mwin": s["monthly_win_rate"],
        "eff_cagr": e.get("cagr"), "eff_maxdd": e.get("maxdd"),
        "eff_sharpe": e.get("sharpe"), "eff_calmar": e.get("calmar"),
        "avg_expo": s.get("avg_exposure"), "n_trades": s.get("executed_trades"),
        **ts,
    }


def fmt(r: dict) -> str:
    return (f"{r['tag']:<34} cagr={r['cagr']:>7.2%} dd={r['maxdd']:>7.2%} "
            f"SR={r['sharpe']:>5.2f} Cal={r['calmar']:>5.2f} | "
            f"胜(超)={r['win_excess']:>5.1%} 胜(绝)={r['win_abs']:>5.1%} "
            f"均持={r['avg_held']:>5.1f}d 笔={r['n']:>4d} "
            f"| 运行期 cagr={r['eff_cagr']:>7.2%} dd={r['eff_maxdd']:>7.2%} "
            f"SR={r['eff_sharpe']:>5.2f} 暴露={r['avg_expo']:.2f}")


# ------------------------------------------------------------------ 主流程
def main():
    book = EtfBook(load_etf_panel())
    fs = load_fs()
    days = load_history()
    trades = build_trades(book, fs, days)
    sd = days[0]["date"]
    print(f"days={len(days)}  trades={len(trades)}  start={sd}\n")

    def run(tag: str, **kw) -> dict:
        r = sim(book, trades, start_date=sd, **kw)
        return score(r, tag)

    BASE = dict(max_holdings=32, risk_parity=True, rp_window=60,
                vol_target=0.16, vol_window=60, vol_floor=0.80)
    out: List[dict] = []
    b = run("基线(线上主口径)", **BASE)
    print(fmt(b)); out.append(b)

    grids = {
        # ---- 信号侧：选得更准
        "分门槛≥66": dict(BASE, min_composite=66),
        "分门槛≥70": dict(BASE, min_composite=70),
        "每日≤3条": dict(BASE, max_per_day=3),
        "每日≤5条": dict(BASE, max_per_day=5),
        "只入手": dict(BASE, actions={"BUY_NOW"}),
        "只埋伏": dict(BASE, actions={"AMBUSH"}),
        "跳过PANIC": dict(BASE, skip_regimes={"PANIC"}),
        "跳过PANIC+DEFEND": dict(BASE, skip_regimes={"PANIC", "DEFEND"}),
        "MA250偏离≤40%": dict(BASE, max_dist_ma250=0.40),
        "MA250偏离≤60%": dict(BASE, max_dist_ma250=0.60),
        "pos60≤0.95": dict(BASE, max_pos60=0.95),
        "rs120≥0": dict(BASE, min_rs120=0.0),
        "vol20≤0.35": dict(BASE, max_vol20=0.35),
        # ---- 权重侧
        "分数加权": dict(BASE, weight_by="score", score_tilt=0.5),
        "动作加权(入手1.35)": dict(BASE, weight_by="action"),
        # ---- 退出侧
        "到期延展(信号还在)": dict(BASE, extend=True, extend_days=60, extend_max=2),
    }
    for tag, kw in grids.items():
        try:
            r = run(tag, **kw)
        except Exception as ex:  # noqa: BLE001
            print(f"{tag:<34} ERROR {ex}")
            continue
        out.append(r)
        print(fmt(r))

    best = max(out[1:], key=lambda r: (r["cagr"] - 0, -r["maxdd"]), default=None)
    print()
    if best:
        print("cagr 最高:", best["tag"])
    best_dd = min(out[1:], key=lambda r: r["maxdd"], default=None)
    if best_dd:
        print("maxdd 最低:", best_dd["tag"])
    best_w = max(out[1:], key=lambda r: r["win_excess"] or 0, default=None)
    if best_w:
        print("胜率最高:", best_w["tag"])
    # Pareto：三项都优于基线
    print("\n== Pareto 改进（cagr≥基线 且 maxdd≤基线 且 胜率≥基线）==")
    for r in out[1:]:
        if (r["cagr"] >= b["cagr"] - 1e-9 and r["maxdd"] >= b["maxdd"] - 1e-9
                and (r["win_excess"] or 0) >= (b["win_excess"] or 0) - 1e-9):
            print("  ", fmt(r))
    json.dump(out, open(f"{C.CACHE_DIR}/lab_opt_results.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
