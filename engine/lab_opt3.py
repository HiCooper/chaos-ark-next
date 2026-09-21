# -*- coding: utf-8 -*-
"""lab_opt 第三轮：定胜率杠杆 + 被剔除样本的归因 + 最终候选的稳健性。

第一、二轮已确定：
    · pos60≤0.95（不买在 60 日区间顶部）= 宽平台，年化 +2.9pp、回撤 -3.2pp
    · 分数加权 tilt∈[0.25,0.5] = 年化再 +1.1pp、回撤再 -0.05pp，成交集合不变
    · 跳过 PANIC 建仓 = 是唯一能把「方向判断胜率」推上去的杠杆
本轮要回答：胜率的提升是不是靠缩小分母做出来的？被剔除的那批结论到底错在哪？
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


def sig_win(rows: List[dict]) -> tuple:
    exs = np.array([t["ex_bench"] for t in rows if t.get("ex_bench") is not None])
    rets = np.array([t["net_ret"] for t in rows if t.get("net_ret") is not None])
    return (float((exs >= C.REVIEW["outperf_win"]).mean()) if exs.size else None,
            float((rets > 0).mean()) if rets.size else None,
            int(exs.size),
            float(exs.mean()) if exs.size else None)


def main():
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades = L.build_trades(book, fs, days)
    sd = days[0]["date"]

    # ---------------------------------------------------------- 1) 归因：谁被剔掉了
    print("=== 1) 被剔除样本的归因（信号口径，全部可结算结论）===")
    allw = sig_win(trades)
    print(f"全部结论          : 胜(超)={allw[0]:.1%} 胜(绝)={allw[1]:.1%} 平均超额={allw[3]:+.2%} n={allw[2]}")

    def subset(pred, label: str):
        rows = [t for t in trades if pred(t)]
        if not rows:
            print(f"{label:<18}: 无样本")
            return
        w = sig_win(rows)
        print(f"{label:<18}: 胜(超)={w[0]:.1%} 胜(绝)={w[1]:.1%} 平均超额={w[3]:+.2%} n={w[2]}")

    subset(lambda t: (t.get("feat") or {}).get("pos60", 0) is not None
           and (t["feat"]["pos60"] or 0) > 0.95, "pos60>0.95(剔除)")
    subset(lambda t: (t["feat"].get("pos60") or 0) <= 0.95, "pos60≤0.95(保留)")
    subset(lambda t: t.get("regime") == "PANIC", "PANIC 环境(剔除)")
    subset(lambda t: t.get("regime") != "PANIC", "非 PANIC(保留)")
    subset(lambda t: (t["feat"].get("dist_ma250") or 0) > 0.40, "MA250偏离>40%(剔除)")
    print()
    print("PANIC 结论分年度（看是否只在某一年差）：")
    g = defaultdict(list)
    for t in trades:
        if t.get("regime") == "PANIC":
            g[t["date"][:4]].append(t)
    for y in sorted(g):
        w = sig_win(g[y])
        print(f"  {y}: 胜(超)={w[0]:.1%} 胜(绝)={w[1]:.1%} n={w[2]}")
    print()
    print("pos60>0.95 结论分年度：")
    g2 = defaultdict(list)
    for t in trades:
        if (t["feat"].get("pos60") or 0) > 0.95:
            g2[t["date"][:4]].append(t)
    for y in sorted(g2):
        w = sig_win(g2[y])
        print(f"  {y}: 胜(超)={w[0]:.1%} 胜(绝)={w[1]:.1%} n={w[2]}")

    # ---------------------------------------------------------- 2) 每日结论条数分布
    print("\n=== 2) 每日结论条数分布（判断「剔除后是否会有替补进来」）===")
    cnt = defaultdict(int)
    for d in days:
        cnt[len(d["picks"])] += 1
    for k in sorted(cnt):
        print(f"  {k} 条/日: {cnt[k]} 天")

    # ---------------------------------------------------------- 3) 最终候选对比
    print("\n=== 3) 最终候选（含稳健性）===")
    cands = [
        ("基线", {}),
        ("A: pos60≤0.95+分数0.5", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5)),
        ("B: A+跳过PANIC", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                              skip_regimes={"PANIC"})),
        ("C: A+MA250≤0.40", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                               max_dist_ma250=0.40)),
        ("D: A+每日≤3", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5, max_per_day=3)),
        ("E: A+rs120≥0", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5, min_rs120=0.0)),
        ("F: B+MA250≤0.40", dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
                               skip_regimes={"PANIC"}, max_dist_ma250=0.40)),
        ("G: pos60≤0.94+分数0.5+跳PANIC", dict(max_pos60=0.94, weight_by="score",
                                            score_tilt=0.5, skip_regimes={"PANIC"})),
        ("H: pos60≤0.96+分数0.5+跳PANIC", dict(max_pos60=0.96, weight_by="score",
                                            score_tilt=0.5, skip_regimes={"PANIC"})),
    ]
    rows = []
    for tag, kw in cands:
        r = L2.evaluate(book, trades, sd, tag, **kw)
        rows.append(r)
        print(L2.fmt2(r))

    json.dump([{k: v for k, v in x.items() if k != "_res"} for x in rows],
              open(f"{C.CACHE_DIR}/lab_opt3_results.json", "w"), ensure_ascii=False, indent=1)

    # ---------------------------------------------------------- 4) 稳健性：A 与 B
    for pick in ("A: pos60≤0.95+分数0.5", "B: A+跳过PANIC",
                 "G: pos60≤0.94+分数0.5+跳PANIC"):
        row = next((x for x in rows if x["tag"] == pick), None)
        if row is None:
            continue
        print(f"\n--- 稳健性：{pick} ---")
        kw = dict(cands[[c[0] for c in cands].index(pick)][1])
        bres = next(x for x in rows if x["tag"] == "基线")["_res"]
        cres = row["_res"]

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
        print(f"{'年份':<8}{'基线收益':>10}{'候选收益':>10}{'基线回撤':>10}{'候选回撤':>10}")
        for y in sorted(set(yb) | set(yc)):
            a, b2 = yb.get(y, (0, 0)), yc.get(y, (0, 0))
            flag = '' if (b2[0] >= a[0] - 1e-9 and b2[1] >= a[1] - 1e-9) else '  ←'
            print(f"{y:<8}{a[0]:>9.1%}{b2[0]:>10.1%}{a[1]:>10.1%}{b2[1]:>10.1%}{flag}")

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
