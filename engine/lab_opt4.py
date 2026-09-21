# -*- coding: utf-8 -*-
"""lab_opt 第四轮：单变量分位诊断——找真正稳定的胜率杠杆。

前三轮教训：
    · pos60≤0.95 是宽平台（0.88~0.98 都有效），可落地；
    · 跳过 PANIC 分年度胜率 0%~67.5%，in-sample 有效但逐年不自洽 → 不可落地；
    · 「每日≤K」「分数加权」属执行层，不改变成交集合的胜率结构。
本轮改用「先看单变量分位上的胜率单调性，再决定是否成规则」，
避免把一个只在某一年凑效的过滤器包装成规律。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel  # noqa: E402
import lab_opt as L  # noqa: E402
import lab_opt2 as L2  # noqa: E402

BASE = L2.BASE
FEATS = ["composite", "horizon", "pos60", "pos250", "dist_ma20", "dist_ma60",
         "dist_ma120", "dist_ma250", "ma20_slope", "dd_from_high60", "ret_20",
         "ret_60", "ret_120", "ret_250", "rs_60", "rs_120", "vol20",
         "vol_ratio", "amount_ratio", "up_vol_share", "maxdd_60", "sharpe20",
         "consec_up", "r5_over_r20"]


def get(t: dict, f: str):
    if f in ("composite", "horizon"):
        return t.get(f)
    return (t.get("feat") or {}).get(f)


def wr(rows: List[dict]) -> tuple:
    exs = np.array([t["ex_bench"] for t in rows if t.get("ex_bench") is not None])
    rets = np.array([t["net_ret"] for t in rows if t.get("net_ret") is not None])
    if not exs.size:
        return (None, None, 0, None)
    return (float((exs >= C.REVIEW["outperf_win"]).mean()),
            float((rets > 0).mean()), int(exs.size), float(exs.mean()))


def main():
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades = L.build_trades(book, fs, days)
    sd = days[0]["date"]
    allw = wr(trades)
    print(f"全样本: 胜(超)={allw[0]:.1%} 胜(绝)={allw[1]:.1%} 超额均值={allw[3]:+.2%} n={allw[2]}\n")

    # ---------------------------------------------------------- 1) 分位诊断
    print("=== 1) 单变量分位诊断（每档胜率是否单调）===")
    print(f"{'特征':<15}{'低档':>22}{'中档':>22}{'高档':>22}  单调?")
    report = {}
    for f in FEATS:
        vals = [(get(t, f), t) for t in trades]
        vals = [(v, t) for v, t in vals if v is not None and np.isfinite(v)]
        if len(vals) < 200:
            continue
        vals.sort(key=lambda x: x[0])
        k = len(vals) // 3
        groups = [vals[:k], vals[k:2 * k], vals[2 * k:]]
        cells = []
        for g in groups:
            w = wr([t for _, t in g])
            cells.append(w)
        if any(c[0] is None for c in cells):
            continue
        lo, mid, hi = cells
        mono = "↑" if (hi[0] > mid[0] > lo[0]) else ("↓" if (hi[0] < mid[0] < lo[0]) else "-")
        spread = abs(hi[0] - lo[0])
        report[f] = {"lo": lo, "mid": mid, "hi": hi, "mono": mono, "spread": spread}
        print(f"{f:<15}"
              f"{f'{lo[0]:.1%}/{lo[3]:+.1%}':>22}"
              f"{f'{mid[0]:.1%}/{mid[3]:+.1%}':>22}"
              f"{f'{hi[0]:.1%}/{hi[3]:+.1%}':>22}  {mono} 差{spread:.1%}")

    # ---------------------------------------------------------- 2) 离散维度
    print("\n=== 2) 离散维度 ===")
    for key, fn in (("action", lambda t: t.get("action")),
                    ("regime", lambda t: t.get("regime")),
                    ("level", lambda t: t.get("level"))):
        g = defaultdict(list)
        for t in trades:
            g[fn(t)].append(t)
        print(f"-- {key}")
        for k, v in sorted(g.items(), key=lambda x: -len(x[1])):
            w = wr(v)
            print(f"   {str(k):<10} 胜(超)={w[0]:.1%} 胜(绝)={w[1]:.1%} 超额均值={w[3]:+.2%} n={w[2]}")

    # ---------------------------------------------------------- 3) 候选组合
    print("\n=== 3) 候选（A = pos60≤0.95 + 分数加权0.5，逐条叠加稳定杠杆）===")
    cands = [
        ("基线", {}),
        ("A", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5)),
        ("A+rs120≥0", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5, min_rs120=0.0)),
        ("A+每日≤3", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5, max_per_day=3)),
        ("A+只埋伏", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                       actions={"AMBUSH"})),
        ("A+vol20≤0.40", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                            max_vol20=0.40)),
        ("A+rs120≥0+每日≤3", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                               min_rs120=0.0, max_per_day=3)),
        ("A+rs120≥0+每日≤2", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                               min_rs120=0.0, max_per_day=2)),
    ]
    rows = []
    for tag, kw in cands:
        r = L2.evaluate(book, trades, sd, tag, **kw)
        rows.append(r)
        print(L2.fmt2(r))

    json.dump([{k: v for k, v in x.items() if k != "_res"} for x in rows],
              open(f"{C.CACHE_DIR}/lab_opt4_results.json", "w"), ensure_ascii=False, indent=1)

    # ---------------------------------------------------------- 4) 稳健性
    for pick in ("A", "A+rs120≥0+每日≤3"):
        row = next((x for x in rows if x["tag"] == pick), None)
        kw = dict(cands[[c[0] for c in cands].index(pick)][1])
        if row is None:
            continue
        bres = next(x for x in rows if x["tag"] == "基线")["_res"]
        cres = row["_res"]
        print(f"\n--- 稳健性：{pick} ---")

        def yearly(res):
            d, nav = res["dates"], np.array(res["nav"])
            g = defaultdict(list)
            for i in range(1, len(d)):
                g[d[i][:4]].append(nav[i] / nav[i - 1] - 1.0)
            out = {}
            for y, v in g.items():
                arr = np.cumprod([1 + x for x in v])
                out[y] = (float(arr[-1] - 1), float((arr / np.maximum.accumulate(arr) - 1).min()))
            return out
        yb, yc = yearly(bres), yearly(cres)
        print(f"{'年份':<8}{'基线收益':>10}{'候选收益':>10}{'基线回撤':>10}{'候选回撤':>10}  判定")
        for y in sorted(set(yb) | set(yc)):
            a, b2 = yb.get(y, (0, 0)), yc.get(y, (0, 0))
            ok = b2[0] >= a[0] - 1e-9 and b2[1] >= a[1] - 1e-9
            print(f"{y:<8}{a[0]:>9.1%}{b2[0]:>10.1%}{a[1]:>10.1%}{b2[1]:>10.1%}  {'更好/持平' if ok else '变差'}")
        n_better = sum(1 for y in set(yb) & set(yc)
                       if yc[y][0] >= yb[y][0] - 1e-9 and yc[y][1] >= yb[y][1] - 1e-9)
        print(f"逐年「收益与回撤都不劣」的年份：{n_better}/{len(set(yb) & set(yc))}")

        print("滚动起点：")
        print(f"{'起点':<12}{'基线cagr':>10}{'候选cagr':>10}{'基线dd':>10}{'候选dd':>10}")
        for y in ("2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023"):
            i = next((k for k, d in enumerate(days) if d["date"] >= f"{y}-01-01"), None)
            if i is None:
                continue
            s = days[i]["date"]
            rb = L.sim(book, trades, start_date=s, **BASE)
            rc = L.sim(book, trades, start_date=s, **{**BASE, **kw})
            print(f"{s:<12}{rb['stats']['cagr']:>10.2%}{rc['stats']['cagr']:>10.2%}"
                  f"{rb['stats']['maxdd']:>10.2%}{rc['stats']['maxdd']:>10.2%}")


if __name__ == "__main__":
    main()
