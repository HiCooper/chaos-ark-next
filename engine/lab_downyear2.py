# -*- coding: utf-8 -*-
"""下跌年份优化第二轮：浮亏降权（loser_deweight）+ 首轮最优 cap 组合。

首轮结论（lab_downyear）：
  · C6 年线动量 cap 是帕累托改进（年化 +0.13pp、回撤 -12.5pp、2022 修复到 -1.6%），
    但治不了主题型亏损年（2017/2023/2024-12/2026，基准都在上涨或震荡）。
  · 归因：亏损年的共性是「浮亏仓位死扛到期 + 主题见顶后仍满仓」。

本轮杠杆：loser_deweight —— 浮亏持仓按因子降权，降出的权重按现金计。
不卖出、不改变任何持有期与卖出时点；浮亏收复权重自动恢复（无前视）。

变体：
  E1 浮亏减半        ur<0 -> 0.5；ur<-10% -> 0.25
  E2 浮亏温和降权    ur<0 -> 0.7；ur<-10% -> 0.4
  E3 浮亏减半+深亏清零  ur<0 -> 0.5；ur<-15% -> 0.05
  E4 E1 + C6         组合首轮最优市场级 cap
  E5 E2 + C6
  E6 E1 + C6∧C1      再叠加温和趋势 cap
  E7 E1 + C6∧C7      再叠加回撤 cap

运行：cd engine && ../.venv/bin/python lab_downyear2.py
"""
import json
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import load_trades_from_events, yearly  # noqa: E402
from lab_downyear import build_caps  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO

E1 = {"threshold": 0.0, "factor": 0.5, "deep_threshold": -0.10, "deep_factor": 0.25}
E2 = {"threshold": 0.0, "factor": 0.7, "deep_threshold": -0.10, "deep_factor": 0.4}
E3 = {"threshold": 0.0, "factor": 0.5, "deep_threshold": -0.15, "deep_factor": 0.05}


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)

    kw = dict(start_date=prod_dates[0],
              max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
              score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])

    base = ST.simulate_etf_portfolio(book, trades, **kw)
    prod = json.load(open(f"{C.ROOT}/web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    nav_diff = max(diffs) if diffs else float("nan")
    print(f"基线复现: cagr={base['stats']['cagr']:.4f} maxdd={base['stats']['maxdd']:.4f} "
          f"| 生产 nav 最大偏差={nav_diff:.2e}")

    caps = build_caps(book, base)
    c6 = caps["C6 年线动量"]
    c1 = caps["C1 趋势温和"]
    c7 = caps["C7 回撤上限"]

    variants = [
        ("E1 浮亏减半", {**kw, "loser_deweight": E1}),
        ("E2 浮亏温和", {**kw, "loser_deweight": E2}),
        ("E3 减半+深亏清零", {**kw, "loser_deweight": E3}),
        ("E4 E1+C6", {**kw, "loser_deweight": E1, "cap_series": c6}),
        ("E5 E2+C6", {**kw, "loser_deweight": E2, "cap_series": c6}),
        ("E6 E1+C6∧C1", {**kw, "loser_deweight": E1, "cap_series": np.minimum(c6, c1)}),
        ("E7 E1+C6∧C7", {**kw, "loser_deweight": E1, "cap_series": np.minimum(c6, c7)}),
    ]

    rows = [(name, ST.simulate_etf_portfolio(book, trades, **k2), k2)
            for name, k2 in variants]

    print("\n=== 全区间指标 ===")
    print(f"{'变体':<16} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'月胜率':>6} {'暴露':>6} {'水下日':>6}")
    b = base["stats"]
    print(f"{'基线':<16} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f} {b['avg_exposure']:>6.3f} "
          f"{b['max_underwater_days']:>6}")
    for name, r, _ in rows:
        s = r["stats"]
        print(f"{name:<16} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f} {s['avg_exposure']:>6.3f} "
              f"{s['max_underwater_days']:>6}")

    print("\n=== 逐年收益 ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    down_years = [y for y, (a, _) in yb.items() if a < 0]
    for name, r, _ in rows:
        yv = yearly(r["nav"], r["dates"], r["bench"])
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = ""
            if y in down_years and c > a + 0.004:
                mark = " ↑" + f"{(c-a)*100:+.1f}pp"
            elif abs(c - a) > 0.005:
                mark = " *"
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline_parity_dev": nav_diff,
           "variants": {name: {k: s[k] for k in ("cagr", "maxdd", "sharpe", "calmar",
                                                 "avg_exposure", "monthly_win_rate")}
                        for name, r in [(n, rr) for n, rr, _ in rows]
                        for s in [r["stats"]]}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_downyear2.json", "w"), ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downyear2.json")


if __name__ == "__main__":
    main()
