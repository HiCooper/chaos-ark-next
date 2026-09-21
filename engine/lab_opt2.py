# -*- coding: utf-8 -*-
"""lab_opt 第二轮：对第一轮筛出的方向做精调 + 稳健性检验。

第一轮结论（engine/lab_opt.py）：
    · pos60≤0.95（不买在 60 日区间顶部）—— cagr 8.76%→11.69%、回撤 -33.11%→-29.95%
    · 分数加权（按综合分倾斜权重）—— cagr 8.76%→10.65%、回撤 -33.11%→-32.38%，成交集合不变
    · 跳过 PANIC 建仓 —— 胜率 38.9%→41.4%，回撤略差
本轮要回答：阈值是不是尖峰？两个机制叠加是否互斥？分年度/子区间是否每一段都更好？
"""
from __future__ import annotations

import json
import os
import sys
from typing import List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel  # noqa: E402
import lab_opt as L  # noqa: E402

BASE = dict(max_holdings=32, risk_parity=True, rp_window=60,
            vol_target=0.16, vol_window=60, vol_floor=0.80, cash_yield=0.015)


def signal_stats(trades: List[dict], **kw) -> dict:
    """信号口径胜率：所有通过过滤且可结算的结论，与页面「方向判断胜率」同定义。"""
    exs, rets = [], []
    for t in trades:
        if kw.get("min_composite") is not None and float(t.get("composite") or 0) < kw["min_composite"]:
            continue
        if kw.get("actions") is not None and t.get("action") not in kw["actions"]:
            continue
        if kw.get("skip_regimes") and t.get("regime") in kw["skip_regimes"]:
            continue
        ft = t.get("feat") or {}
        if kw.get("max_dist_ma250") is not None and (ft.get("dist_ma250") or 0) > kw["max_dist_ma250"]:
            continue
        if kw.get("max_pos60") is not None and (ft.get("pos60") if ft.get("pos60") is not None else 1) > kw["max_pos60"]:
            continue
        if kw.get("min_rs120") is not None and (ft.get("rs_120") or 0) < kw["min_rs120"]:
            continue
        if kw.get("max_vol20") is not None and (ft.get("vol20") or 0) > kw["max_vol20"]:
            continue
        if t.get("ex_bench") is not None:
            exs.append(t["ex_bench"])
        if t.get("net_ret") is not None:
            rets.append(t["net_ret"])
    exs, rets = np.array(exs), np.array(rets)
    return {
        "n_sig": int(exs.size),
        "sig_win_excess": float((exs >= C.REVIEW["outperf_win"]).mean()) if exs.size else None,
        "sig_win_abs": float((rets > 0).mean()) if rets.size else None,
        "sig_avg_excess": float(exs.mean()) if exs.size else None,
        "sig_avg_ret": float(rets.mean()) if rets.size else None,
    }


def evaluate(book, trades, sd, tag: str, **kw) -> dict:
    full = dict(BASE)
    full.update(kw)
    r = L.sim(book, trades, start_date=sd, **full)
    row = L.score(r, tag)
    row.update(signal_stats(trades, **kw))
    row["_res"] = r
    return row


def fmt2(r: dict) -> str:
    return (f"{r['tag']:<30} cagr={r['cagr']:>7.2%} dd={r['maxdd']:>7.2%} SR={r['sharpe']:>5.2f} "
            f"Cal={r['calmar']:>5.2f} | 信号胜(超)={r['sig_win_excess']:>5.1%} 胜(绝)={r['sig_win_abs']:>5.1%} "
            f"命中={r['n_sig']:>4d} | 组合胜(超)={r['win_excess']:>5.1%} 笔={r['n']:>4d} "
            f"均持={r['avg_held']:>5.1f}d | 运行期 cagr={r['eff_cagr']:>7.2%} dd={r['eff_maxdd']:>7.2%}")


def main():
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades = L.build_trades(book, fs, days)
    sd = days[0]["date"]
    print(f"days={len(days)} trades={len(trades)} start={sd}\n")

    rows: List[dict] = []
    b = evaluate(book, trades, sd, "基线(线上主口径)")
    rows.append(b)
    print(fmt2(b))

    print("\n=== 1) pos60 阈值精调 ===")
    for v in (0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.98):
        r = evaluate(book, trades, sd, f"pos60≤{v:.2f}", max_pos60=v)
        rows.append(r); print(fmt2(r))

    print("\n=== 2) 分数加权倾斜度精调 ===")
    for t in (0.25, 0.5, 0.75, 1.0, 1.5):
        r = evaluate(book, trades, sd, f"分数加权(tilt={t})", weight_by="score", score_tilt=t)
        rows.append(r); print(fmt2(r))

    print("\n=== 3) 叠加：pos60 + 分数加权 ===")
    for v in (0.92, 0.95):
        for t in (0.5, 0.75):
            r = evaluate(book, trades, sd, f"pos60≤{v:.2f}+分数{t}", max_pos60=v,
                         weight_by="score", score_tilt=t)
            rows.append(r); print(fmt2(r))

    print("\n=== 4) 再叠：跳过 PANIC 建仓 ===")
    for v in (0.92, 0.95):
        r = evaluate(book, trades, sd, f"pos60≤{v:.2f}+分数0.5+跳PANIC", max_pos60=v,
                     weight_by="score", score_tilt=0.5, skip_regimes={"PANIC"})
        rows.append(r); print(fmt2(r))

    print("\n=== 5) 对照：只跳 PANIC / 只每日≤3 ===")
    rows.append(r := evaluate(book, trades, sd, "只跳PANIC", skip_regimes={"PANIC"})); print(fmt2(r))
    rows.append(r := evaluate(book, trades, sd, "每日≤3+分数0.5+pos60≤0.95",
                              max_per_day=3, weight_by="score", score_tilt=0.5, max_pos60=0.95))
    print(fmt2(r))

    json.dump([{k: v for k, v in x.items() if k != "_res"} for x in rows],
              open(f"{C.CACHE_DIR}/lab_opt2_results.json", "w"), ensure_ascii=False, indent=1)

    # ---------------- 稳健性：分年度 / 子区间 / 滚动起点 ----------------
    best = [x for x in rows if x["tag"] == "pos60≤0.95+分数0.75"]
    cand = best[0] if best else None
    if cand is None:
        return
    print("\n=== 6) 稳健性检验：候选 vs 基线 ===")
    print(f"候选 = {cand['tag']}")
    bres, cres = b["_res"], cand["_res"]

    def yearly(res):
        d = res["dates"]
        nav = np.array(res["nav"])
        g = {}
        for i in range(1, len(d)):
            y = d[i][:4]
            g.setdefault(y, []).append(nav[i] / nav[i - 1] - 1.0)
        out = {}
        for y, v in g.items():
            cur = float(np.prod([1 + x for x in v]) - 1.0)
            arr = np.cumprod([1 + x for x in v])
            out[y] = (cur, float((arr / np.maximum.accumulate(arr) - 1).min()))
        return out

    yb, yc = yearly(bres), yearly(cres)
    print(f"{'年份':<6}{'基线收益':>10}{'候选收益':>10}{'基线回撤':>10}{'候选回撤':>10}")
    for y in sorted(set(yb) | set(yc)):
        a, b2 = yb.get(y, (None, None)), yc.get(y, (None, None))
        print(f"{y:<6}{(a[0] or 0):>9.1%}{(b2[0] or 0):>10.1%}"
              f"{(a[1] or 0):>10.1%}{(b2[1] or 0):>10.1%}")

    print("\n子区间（按年切起点的滚动回测）：")
    print(f"{'起点':<12}{'基线cagr':>10}{'候选cagr':>10}{'基线dd':>10}{'候选dd':>10}")
    for y in ("2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023"):
        i = next((k for k, d in enumerate(days) if d["date"] >= f"{y}-01-01"), None)
        if i is None:
            continue
        s = days[i]["date"]
        rb = L.sim(book, trades, start_date=s, **BASE)
        rc = L.sim(book, trades, start_date=s, max_pos60=0.95,
                   weight_by="score", score_tilt=0.75, **BASE)
        print(f"{s:<12}{rb['stats']['cagr']:>10.2%}{rc['stats']['cagr']:>10.2%}"
              f"{rb['stats']['maxdd']:>10.2%}{rc['stats']['maxdd']:>10.2%}")


if __name__ == "__main__":
    main()
