# -*- coding: utf-8 -*-
"""最大回撤归因实验台（lab）。

用途：在不动生产链路（review.py / settlement.py）的前提下，
    1) 用 history.json + etf_panel.json 精确重建线上那 1292 笔可结算结论；
    2) 复现线上主口径指标（cagr 8.56% / maxdd -39.5%），确认实验台与线上同源；
    3) 跑降回撤候选方案的反事实对比。

所有结论只有在能复现线上基线之后才可信。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, perf_stats, load_etf_panel  # noqa: E402

ROOT = C.ROOT
HIST = f"{ROOT}/site/data/history.json"
TRADES_CACHE = f"{C.CACHE_DIR}/lab_trades.json"


# ------------------------------------------------------------------ 输入
def load_history() -> List[dict]:
    with open(HIST, encoding="utf-8") as f:
        return json.load(f)["days"]


def build_trades(book: EtfBook, days: List[dict],
                 cache: str = TRADES_CACHE) -> List[dict]:
    """把每日结论按线上口径结算成逐笔交易（可缓存）。"""
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    trades = []
    for d in days:
        for p in d["picks"]:
            H = int(p["horizon"])
            tf = book.settle_trade(p["name"], d["date"], H)
            if not tf:
                continue
            trades.append({
                "date": d["date"], "sw_name": p["name"], "horizon": H,
                "composite": p.get("composite", 0), "action": p.get("action"),
                "level": p.get("level"), "regime": d.get("regime"),
                "net_ret": tf["net_ret"], "ex_bench": tf["ex_bench"],
                "maxdd": tf["maxdd"], "sell_date": tf["sell_date"],
                "buy_date": tf["buy_date"], "etf_sid": tf["etf"],
                "etf_name": tf["etf_name"], "held_days": tf["held_days"],
                "gross_ret": tf["gross_ret"],
            })
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False)
    return trades


def _sigma(book: EtfBook, sid: str, k: int, window: int,
           downside: bool = False) -> float:
    """标的截至第 k-1 日的年化波动（downside=True 时只取下跌方向）。"""
    vals = [book.close(sid, j) for j in range(max(0, k - window), k)]
    vals = [v for v in vals if v]
    if len(vals) < 11:
        return 0.25
    rr = np.diff(vals) / np.array(vals[:-1])
    if downside:
        neg = rr[rr < 0]
        if neg.size > 5:
            rr = neg
    s = float(np.std(rr) * np.sqrt(252))
    return s if s > 1e-4 else 0.25


# ------------------------------------------------------------------ 通用模拟器
def sim(book: EtfBook, trades: List[dict], *, max_holdings: int = 32,
        vol_target: Optional[float] = None, vol_window: int = 60,
        vol_floor: float = 0.5, semi_vol_target: Optional[float] = None,
        semi_window: int = 60, nav_ma_window: Optional[int] = None,
        nav_ma_scale: float = 0.5, nav_ma_scale2: Optional[float] = None,
        risk_parity: bool = False, rp_downside: bool = False,
        vol_target_live: bool = False,
        rp_window: int = 60, rp_rebalance: int = 1, rp_cap: Optional[float] = None, trail_stop: Optional[float] = None,
        trail_confirm: int = 1, dd_throttle: Optional[float] = None,
        dd_restore: float = 0.0, group_cap: Optional[int] = None,
        per_sector_max: Optional[int] = None, no_averaging_down: bool = False,
        cooldown_days: int = 0, min_composite: Optional[float] = None,
        cash_yield: float = 0.015, start_date: Optional[str] = None,
        collect_holdings: bool = False) -> dict:
    """带风控开关的组合模拟器。

    与生产口径（settlement.simulate_etf_portfolio）保持同一套成交价、
    同一套成本、同一套「每日等权」假设；新增开关只作用于仓位大小与退出时点，
    因此任何差异都只能归因于被测试的那一条规则。

    开关：
      max_holdings  : 同时持仓上限（容量约束）
      vol_target    : 组合目标年化波动；实际波动高于目标时按比例降仓，剩余吃现金收益
      risk_parity   : 持仓间按 1/σ 加权，而非等权
      trail_stop    : 从持仓期最高收盘价回撤超过该比例（如 0.15）即次日开盘退出
      dd_throttle   : 组合自身回撤超过该比例（如 0.12）时仓位乘 throttle_scale
      group_cap     : 同一 L1 母行业下最多同时持有几个方向
      cooldown_days : 同一行业两次建仓之间的最小间隔（防连亏加仓）
      min_composite : 只在综合分不低于该值时建仓
    """
    dates = book.dates
    n = len(dates)
    s_i = book.idx.get(start_date, 0) if start_date else 0
    s_i = max(s_i, 1)

    # ---------- 建仓计划 ----------
    plan: Dict[int, List[dict]] = defaultdict(list)
    skipped = 0
    filtered = 0
    for t in trades:
        i0 = book.idx.get(t["date"])
        if i0 is None or i0 < s_i:
            continue
        if min_composite is not None and float(t.get("composite") or 0) < min_composite:
            filtered += 1
            continue
        H = int(t["horizon"])
        b_i = i0 + 1
        s_i_ = book.idx.get(t["sell_date"], i0 + 1 + H) if t.get("sell_date") else i0 + 1 + H
        if b_i >= n or s_i_ >= n or s_i_ <= b_i:
            continue
        sid = t.get("etf_sid") or book.etf_of(t["sw_name"], dates[b_i])
        if not sid:
            skipped += 1
            continue
        plan[b_i].append({"sw": t["sw_name"], "end": s_i_, "secid": sid,
                          "score": float(t.get("composite") or 0),
                          "horizon": H, "action": t.get("action"),
                          "group": t["sw_name"].split("-")[0]})

    holdings: List[dict] = []
    nav, nav_dates = [1.0], [dates[s_i - 1]]
    bench_nav = [1.0]
    cur = bcur = 1.0
    bench_sid = book.bench_etf
    prev_close: Dict[str, float] = {}
    events: List[dict] = []
    n_exec = blocked_cap = blocked_gate = 0
    last_exit: Dict[str, int] = {}
    hold_count: List[int] = []
    exposure_log: List[float] = []

    rf = cash_yield / 252.0
    ret_hist: List[float] = []
    cur_expo = 1.0
    peak_nav = 1.0

    for k in range(s_i, n):
        d = dates[k]
        day_events: List[dict] = []
        day_rets: List[float] = []

        # 1) 已有持仓推进
        kept = []
        for h in holdings:
            c = book.close(h["secid"], k)
            p = prev_close.get(h["sid"])
            r_i = (c / p - 1.0) if (c and p) else 0.0
            # 逐笔风险权重（按 1/σ 分配风险预算，rp_rebalance 控制重算频率）
            if risk_parity:
                if rp_rebalance <= 1 or (k % rp_rebalance) == 0 or h.get("w") is None:
                    h["sigma"] = _sigma(book, h["secid"], k, rp_window, rp_downside)
                    w = 1.0 / max(h["sigma"], 0.05)
                    if rp_cap is not None:
                        w = min(w, rp_cap)
                    h["w"] = w
            else:
                h["w"] = 1.0
            day_rets.append((r_i, h["w"]))

            if c:
                h["peak"] = max(h.get("peak", c), c)

            exit_now = False
            # 到期退出
            if h["end"] <= k:
                exit_now = True
            # 移动止盈止损
            if not exit_now and trail_stop is not None:
                pk = h.get("peak")
                if pk and c and c / pk - 1.0 <= -abs(trail_stop):
                    exit_now = True
                    h["trail_hit"] = True
            if exit_now:
                px = c
                bpx = h.get("buy_price")
                gross = (px / bpx - 1.0) if (px and bpx) else None
                net = (gross - C.ROUND_TRIP_COST) if gross is not None else None
                bre = None
                if bench_sid:
                    bb = book.open_(bench_sid, h["buy_i"])
                    bs = book.close(bench_sid, k)
                    if bb and bs and bb > 0:
                        bre = bs / bb - 1.0 - C.ROUND_TRIP_COST
                day_events.append({
                    "k": "S", "sw": h["sw"], "sid": h["secid"],
                    "price": round(px, 4) if px else None,
                    "bd": dates[h["buy_i"]], "bpx": round(bpx, 4) if bpx else None,
                    "h": h.get("horizon"), "a": h.get("action"),
                    "ret": round(net, 5) if net is not None else None,
                    "ex": round(net - bre, 5) if (net is not None and bre is not None) else None,
                })
                last_exit[h["sw"]] = k
            else:
                kept.append(h)
        holdings = kept

        # 2) 新开仓
        if k in plan:
            adds = sorted(plan[k], key=lambda x: -x["score"])
            for item in adds:
                if len(holdings) >= max_holdings:
                    blocked_cap += 1
                    continue
                if cooldown_days and (k - last_exit.get(item["sw"], -10 ** 9)) < cooldown_days:
                    blocked_gate += 1
                    continue
                if group_cap is not None:
                    g = item["group"]
                    if sum(1 for h in holdings if h["group"] == g) >= group_cap:
                        blocked_gate += 1
                        continue
                # 同行业同时持仓上限：禁止把多个槽位押在同一个方向上
                if per_sector_max is not None:
                    if sum(1 for h in holdings if h["sw"] == item["sw"]) >= per_sector_max:
                        blocked_gate += 1
                        continue
                # 浮亏时不加仓（不向下摊平）
                if no_averaging_down:
                    dup = [h for h in holdings if h["sw"] == item["sw"]]
                    if dup:
                        c0 = book.close(dup[0]["secid"], k)
                        bp0 = dup[0].get("buy_price")
                        if c0 and bp0 and c0 < bp0:
                            blocked_gate += 1
                            continue
                sid = item["secid"]
                o = book.open_(sid, k)
                if not o:
                    continue
                # 个股波动率（用于 risk_parity）
                sigma = _sigma(book, sid, k, rp_window, rp_downside) \
                    if (risk_parity or vol_target_live) else 0.25
                holdings.append({
                    "sw": item["sw"], "sid": sid, "secid": sid, "end": item["end"],
                    "buy_i": k, "buy_price": o, "horizon": item["horizon"],
                    "action": item["action"], "group": item["group"], "sigma": sigma,
                    "peak": o, "w": (1.0 / max(sigma, 0.05)) if risk_parity else 1.0,
                })
                n_exec += 1
                day_events.append({
                    "k": "B", "sw": item["sw"], "sid": sid, "price": round(o, 4),
                    "h": item["horizon"], "a": item["action"], "sc": round(item["score"], 1),
                })
                c = book.close(sid, k)
                if c:
                    day_rets.append((c / o - 1.0, holdings[-1]["w"]))

        # 3) 刷新持仓收盘价缓存（次日收益的比较基准）
        prev_close = {}
        for h in holdings:
            c = book.close(h["secid"], k)
            if c:
                prev_close[h["sid"]] = c

        # 3b) 组合当日收益（加权）
        if day_rets:
            w_sum = sum(w for _, w in day_rets)
            r_raw = float(sum(r * w for r, w in day_rets) / w_sum) if w_sum > 0 else 0.0
        else:
            r_raw = 0.0

        # 4) 仓位缩放：波动率目标 / 回撤熔断
        expo = 1.0
        if vol_target is not None and len(ret_hist) >= 20:
            win = ret_hist[-vol_window:]
            sig = float(np.std(win) * np.sqrt(252))
            if sig > 1e-6:
                expo = min(1.0, vol_target / sig)
                expo = max(expo, vol_floor)
        # 前瞻式波动目标：用当前持仓自身的实时波动估计组合波动（比实现收益波动更灵敏）
        if vol_target_live and holdings:
            ws = [h.get("w", 1.0) for h in holdings]
            sw = sum(ws)
            if sw > 0:
                var = sum((w * (h.get("sigma") or 0.25)) ** 2 for w, h in zip(ws, holdings))
                sig_live = float(np.sqrt(var) / sw)
                if sig_live > 1e-6:
                    expo = min(expo, max(min(1.0, vol_target / sig_live), vol_floor))
        # 下行波动目标：只对下跌方向的波动敏感，上涨段保持满仓
        if semi_vol_target is not None and len(ret_hist) >= 20:
            win = ret_hist[-semi_window:]
            neg = [x for x in win if x < 0]
            if len(neg) > 5:
                sig = float(np.std(neg) * np.sqrt(252))
                if sig > 1e-6:
                    expo = min(expo, max(min(1.0, semi_vol_target / sig), vol_floor))
        # 组合自身趋势：净值跌破自身均线即减仓（趋势跟踪，非预测）
        if nav_ma_window is not None and len(nav) > nav_ma_window:
            ma = float(np.mean(nav[-nav_ma_window:]))
            if nav[-1] < ma:
                expo = min(expo, nav_ma_scale)
        if dd_throttle is not None:
            peak_nav = max(peak_nav, cur)
            dd = cur / peak_nav - 1.0
            if dd <= -abs(dd_throttle):
                expo = min(expo, dd_restore if dd_restore > 0 else 0.5)
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
    stats = perf_stats(nav_arr, nav_dates, np.array(bench_nav) if bench_sid else None)
    # 策略实际运行口径：ETF 数据在 2019 年之前覆盖不足，空仓期会稀释年化，
    # 因此另算一条「自首次建仓日起」的指标，用于如实展示策略运行期的表现。
    first_i = next((i for i, x in enumerate(hold_count) if x > 0), 0) + 1
    if first_i > 0 and first_i < len(nav) - 100:
        eff = perf_stats(nav_arr[first_i:], nav_dates[first_i:],
                         np.array(bench_nav[first_i:]) if bench_sid else None)
        eff["start"] = nav_dates[first_i]
        stats["effective"] = eff
    stats.update({
        "executed_trades": n_exec, "planned_trades": sum(len(v) for v in plan.values()),
        "blocked_by_capacity": blocked_cap, "blocked_by_rule": blocked_gate,
        "filtered_by_score": filtered, "skipped_untradable": skipped,
        "max_holdings": max_holdings,
        "avg_holdings": round(float(np.mean(hold_count)), 2) if hold_count else None,
        "max_concurrent": int(max(hold_count)) if hold_count else None,
        "avg_exposure": round(float(np.mean(exposure_log)), 3) if exposure_log else None,
    })
    out = {"dates": nav_dates, "nav": [round(x, 5) for x in nav],
           "bench": [round(x, 5) for x in bench_nav], "stats": stats, "events": events}
    if collect_holdings:
        out["hold_count"] = hold_count
        out["exposure"] = exposure_log
    return out


# ------------------------------------------------------------------ 报告
def fmt(tag: str, s: dict) -> str:
    return (f"{tag:<26} cagr={s.get('cagr'):>7.2%}  maxdd={s.get('maxdd'):>7.2%}  "
            f"sharpe={s.get('sharpe'):>5.2f}  calmar={s.get('calmar'):>5.2f}  "
            f"vol={s.get('vol'):>6.2%}  mwin={s.get('monthly_win_rate')}  "
            f"underwater={s.get('max_underwater_days')}d  trades={s.get('executed_trades')}")


def main():
    book = EtfBook(load_etf_panel())
    days = load_history()
    trades = build_trades(book, days)
    print(f"history days={len(days)}  rebuilt settled trades={len(trades)}")
    base = sim(book, trades, start_date=days[0]["date"], collect_holdings=True)
    print(fmt("baseline(线上口径)", base["stats"]))
    print("avg holdings", base["stats"]["avg_holdings"],
          "max concurrent", base["stats"]["max_concurrent"])
    return book, trades, base


if __name__ == "__main__":
    main()
