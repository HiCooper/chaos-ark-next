# -*- coding: utf-8 -*-
"""lab_opt 第五轮：把分位诊断里最强的胜率因子做成规则，并检验其稳健性。

第四轮的分位诊断给出三个单调且分离度最大的因子（差 12%~15%）：
    pos250      近 250 日区间位置   低位 30.9% → 高位 42.9%
    maxdd_60    近 60 日最深回撤    深跌 29.5% → 浅跌 44.9%
    rs_120      120 日相对行业等权   落后 29.9% → 领先 43.5%
三者方向一致：**在下跌中继里接刀，是胜率最大的漏损来源**。
本轮把它们做成事前可执行的规则，与已确认的 pos60 + 分数加权组合，看能否三者同向改善。
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
A = dict(max_pos60=0.95, weight_by="score", score_tilt=0.5)


def main():
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades = L.build_trades(book, fs, days)
    sd = days[0]["date"]

    # ---------------------------------------------------------------- 单杠杆
    print("=== 1) 三个胜率因子各自成规则（叠加在 A 之上）===")
    grids = []
    for v in (0.10, 0.25, 0.40, 0.55):
        grids.append((f"A+pos250≥{v:.2f}", dict(A, min_pos250=v)))
    for v in (-0.35, -0.28, -0.22, -0.16):
        grids.append((f"A+60日回撤≥{v:.2f}", dict(A, min_maxdd60=v)))
    for v in (0.20, 0.50, 0.70):
        grids.append((f"A+MA250偏离≤{v:.2f}", dict(A, max_dist_ma250=v)))

    rows = []
    for tag, kw in grids:
        try:
            r = L2.evaluate(book, trades, sd, tag, **kw)
        except TypeError as ex:
            print(f"{tag}: {ex}")
            continue
        rows.append(r)
        print(L2.fmt2(r))

    # ---------------------------------------------------------------- 组合
    print("\n=== 2) 组合（A + rs120≥0 + 每日≤2 + 最强胜率因子）===")
    combos = [
        ("基线", {}),
        ("A", dict(A)),
        ("A+rs120≥0+每日≤2", dict(A, min_rs120=0.0, max_per_day=2)),
        ("A+rs120≥0+每日≤2+pos250≥0.25", dict(A, min_rs120=0.0, max_per_day=2, min_pos250=0.25)),
        ("A+rs120≥0+每日≤2+60日回撤≥-0.28", dict(A, min_rs120=0.0, max_per_day=2,
                                            min_maxdd60=-0.28)),
        ("A+rs120≥0+每日≤2+MA250≤0.50", dict(A, min_rs120=0.0, max_per_day=2,
                                          max_dist_ma250=0.50)),
        ("A+rs120≥0+pos250≥0.25", dict(A, min_rs120=0.0, min_pos250=0.25)),
    ]
    prows = []
    for tag, kw in combos:
        r = L2.evaluate(book, trades, sd, tag, **kw)
        prows.append((tag, kw, r))
        print(L2.fmt2(r))

    json.dump([{k: v for k, v in x[2].items() if k != "_res"} for x in prows],
              open(f"{C.CACHE_DIR}/lab_opt5_results.json", "w"), ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------- 稳健性
    base_res = next(x[2] for x in prows if x[0] == "基线")["_res"]

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

    yb = yearly(base_res)
    for tag in ("A+rs120≥0+每日≤2", "A+rs120≥0+每日≤2+60日回撤≥-0.28",
                "A+rs120≥0+pos250≥0.25"):
        hit = next((x for x in prows if x[0] == tag), None)
        if hit is None:
            continue
        _, kw, r = hit
        print(f"\n--- 稳健性：{tag} ---")
        yc = yearly(r["_res"])
        print(f"{'年份':<8}{'基线收益':>10}{'候选收益':>10}{'基线回撤':>10}{'候选回撤':>10}  判定")
        nb = 0
        tot = 0
        for y in sorted(set(yb) | set(yc)):
            a, b2 = yb.get(y, (0, 0)), yc.get(y, (0, 0))
            if a == (0, 0) and b2 == (0, 0):
                continue
            tot += 1
            ok = b2[0] >= a[0] - 1e-9 and b2[1] >= a[1] - 1e-9
            nb += 1 if ok else 0
            print(f"{y:<8}{a[0]:>9.1%}{b2[0]:>10.1%}{a[1]:>10.1%}{b2[1]:>10.1%}  {'更好/持平' if ok else '变差'}")
        print(f"  逐年「收益与回撤都不劣」：{nb}/{tot}")
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

    # ---------------------------------------------------------------- 剔除集合归因
    print("\n=== 3) 各规则剔除的样本长什么样（信号口径）===")
    def sub(pred, label):
        rows_ = [t for t in trades if pred(t)]
        if not rows_:
            return
        exs = np.array([t["ex_bench"] for t in rows_ if t.get("ex_bench") is not None])
        rets = np.array([t["net_ret"] for t in rows_ if t.get("net_ret") is not None])
        print(f"  {label:<28} 胜(超)={float((exs>=0.02).mean()):.1%} "
              f"胜(绝)={float((rets>0).mean()):.1%} 均超额={float(exs.mean()):+.2%} n={exs.size}")
    sub(lambda t: (t["feat"].get("pos60") or 0) > 0.95, "pos60>0.95 剔除")
    sub(lambda t: (t["feat"].get("rs_120") or 0) < 0, "rs120<0 剔除")
    sub(lambda t: (t["feat"].get("maxdd_60") or 0) < -0.28, "60日回撤<-28% 剔除")
    sub(lambda t: (t["feat"].get("pos250") or 1) < 0.25, "pos250<0.25 剔除")
    sub(lambda t: True, "全部结论")


if __name__ == "__main__":
    main()
