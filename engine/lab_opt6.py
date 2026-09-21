# -*- coding: utf-8 -*-
"""lab_opt 第六轮：集中度上限——「同一个篮子装太多鸡蛋」。

思路来源（这一轮和前几轮的区别）：
    前五轮都在动「选什么」（信号侧）和「怎么分权」（权重侧）。
    本轮动的是**槽位**：容量上限 max_holdings=32 只约束总数，
    却不约束「这 32 个槽位是不是全押在同一只 ETF / 同一个行业上」。
    两层架构下，多条细分赛道结论会共用同一只一级行业 ETF 作代理，
    结算层于是可能出现「同一只 ETF 在不同日期各买一笔、各自持有到期」，
    等价于在这只 ETF 上不断加码。这是一个纯粹的集中度敞口，
    事前看得见、可约束，且不改变任何一笔买卖的时点。

判定口径不变：cagr 不降、回撤不升、胜率不降，才算 Pareto 改进。
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
import lab_opt2 as L2  # noqa: E402

# 生产候选配置：结论预过滤（pos60≤0.95、rs120≥0）+ 分数加权 + 每日≤2
P = dict(max_pos60=0.95, weight_by="score", score_tilt=0.5,
         min_rs120=0.0, max_per_day=2)


def main() -> None:
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades = L.build_trades(book, fs, days)
    sd = days[0]["date"]
    print(f"days={len(days)} trades={len(trades)} start={sd}\n")

    rows: List[dict] = []
    r0 = L2.evaluate(book, trades, sd, "基线(等权满仓)", max_holdings=32)
    rows.append(r0)
    print(L2.fmt2(r0))
    rP = L2.evaluate(book, trades, sd, "生产候选 P", **P)
    rows.append(rP)
    print(L2.fmt2(rP))

    # ---------------------------------------------------------------- 现状诊断
    res = rP["_res"]
    print("\n=== 0) 现状：同一只 ETF 被重复持有多久（P 配置）===")
    from collections import Counter
    # 用成交事件反推：同一 sid 的买入次数
    buys = [e for e in res["events"] if e["k"] == "B"]
    cnt = Counter(e["sid"] for e in buys)
    tot = sum(cnt.values())
    multi = sum(1 for v in cnt.values() if v > 1)
    print(f"  买入 {tot} 笔，落在 {len(cnt)} 只 ETF 上；"
          f"被买过 2 次以上的 ETF 有 {multi} 只，"
          f"最集中的一只被买 {max(cnt.values()) if cnt else 0} 次")
    top = cnt.most_common(6)
    names = {s: s for s in cnt}
    print("  Top6：", [(s.split('.')[-1], n) for s, n in top])
    st = res["stats"]
    print(f"  容量约束挡下 {st.get('blocked_by_capacity')} 笔，"
          f"平均在仓 {st.get('avg_holdings')} 只，最高 {st.get('max_holdings_used', '—')}")
    hold = res.get("hold_count") or []
    if hold:
        print(f"  在仓只数：均值 {np.mean(hold):.1f} 中位 {np.median(hold):.0f} 最高 {max(hold)}")

    # ---------------------------------------------------------------- 网格
    print("\n=== 1) 同一只 ETF 在仓上限 ===")
    for v in (1, 2, 3, 4):
        r = L2.evaluate(book, trades, sd, f"P + 同 ETF≤{v}", **{**P, "max_per_sid": v})
        rows.append(r)
        s = r["_res"]["stats"]
        print(L2.fmt2(r) + f"  挡下={s.get('blocked_by_sid_cap')}")

    print("\n=== 2) 同一行业（信号名）在仓上限 ===")
    for v in (1, 2, 3, 4):
        r = L2.evaluate(book, trades, sd, f"P + 同行业≤{v}", **{**P, "max_per_sw": v})
        rows.append(r)
        s = r["_res"]["stats"]
        print(L2.fmt2(r) + f"  挡下={s.get('blocked_by_sw_cap')}")

    print("\n=== 3) 两者叠加 ===")
    for a, b in ((2, 3), (2, 4), (3, 3), (3, 4)):
        r = L2.evaluate(book, trades, sd, f"P + 同ETF≤{a} + 同行业≤{b}",
                        **{**P, "max_per_sid": a, "max_per_sw": b})
        rows.append(r)
        print(L2.fmt2(r))

    # ---------------------------------------------------------------- Pareto 判定
    print("\n=== 4) Pareto 判定（相对生产候选 P：cagr 不降 / 回撤不升 / 胜率不降）===")
    bc, bd, bw = rP["cagr"], rP["maxdd"], (rP["win_excess"] or 0)
    passed = []
    for r in rows[2:]:
        ok = (r["cagr"] >= bc - 1e-9 and r["maxdd"] >= bd - 1e-9
              and (r["win_excess"] or 0) >= bw - 1e-9)
        if ok:
            passed.append(r)
            print("  ✓", L2.fmt2(r))
    if not passed:
        print("  无——集中度上限要么牺牲年化、要么不改善回撤，不予采纳。")

    # ---------------------------------------------------------------- 逐年稳健性
    print("\n=== 5) 逐年（仅对通过 Pareto 的候选）===")

    def yearly(res):
        d, nav = res["dates"], np.array(res["nav"])
        g = {}
        for i in range(1, len(d)):
            g.setdefault(d[i][:4], []).append(nav[i] / nav[i - 1] - 1.0)
        out = {}
        for y, v in g.items():
            arr = np.cumprod([1 + x for x in v])
            out[y] = (float(arr[-1] - 1), float((arr / np.maximum.accumulate(arr) - 1).min()))
        return out

    yb = yearly(rP["_res"])
    for r in passed:
        yc = yearly(r["_res"])
        nb = tot = 0
        line = []
        for y in sorted(set(yb) | set(yc)):
            a, b2 = yb.get(y, (0, 0)), yc.get(y, (0, 0))
            if a == (0, 0) and b2 == (0, 0):
                continue
            tot += 1
            ok = b2[0] >= a[0] - 1e-9 and b2[1] >= a[1] - 1e-9
            nb += 1 if ok else 0
            line.append(f"{y}:{'OK' if ok else 'X'}")
        print(f"  {r['tag']:<26} 逐年不劣 {nb}/{tot}  {' '.join(line)}")

    json.dump([{k: v for k, v in r.items() if k != "_res"} for r in rows],
              open(f"{C.CACHE_DIR}/lab_opt6_results.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已落盘 {C.CACHE_DIR}/lab_opt6_results.json")


if __name__ == "__main__":
    main()
