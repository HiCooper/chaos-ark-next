# -*- coding: utf-8 -*-
"""下跌年份优化第三轮：C6（年报动量 cap）的精细化组合。

第二轮教训（lab_downyear2，无前视+计成本后的诚实结果）：
  · 浮亏降权（loser_deweight）不是免费午餐：回撤 -35.8% -> -27.0%，
    但年化 10.49% -> 7.96%。权重在水位线附近反复翻转，洗掉反弹初段收益。
  · C6 年线动量 cap 仍是唯一帕累托改进（年化 10.62%、回撤 -23.3%）。

C6 的短板：2023（-2.3% -> -3.5%）/ 2026（-2.4% -> -4.6%）。
原因：mom250 阈值在拐点反应慢——2023 年 1 月反弹时 2022 年的负动量仍压着 cap。

本轮变体（全部无前视，右移一日）：
  F1 C6∧C1        年线动量与温和趋势取小（更保守）
  F2 C6∨C1        取大（任一状态好转即释放，防拐点钝化）
  F3 连续动量      cap = clip(0.35 + 3.25*mom250, 0.35, 1.0)（无阈值跳变）
  F4 mom120       年线动量改用 120 日窗口（更快反应）
  F5 C6∨C3        与环境温和 cap 取大
  F6 C6+C1∨C7     C6 硬约束 +（趋势∨回撤）软约束取大
  F7 C6+E2        叠加温和浮亏降权（测回撤下限的组合）

运行：绝对路径直接跑（已改为 C.ROOT 相对定位）。
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

E2 = {"threshold": 0.0, "factor": 0.7, "deep_threshold": -0.10, "deep_factor": 0.4}


def extra_caps(book: "ST.EtfBook") -> dict:
    n = len(book.dates)
    sid = book.bench_etf
    px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(px)
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(n), idxs, px[idxs])
    out = {}
    for tag, win in ("mom250", 250), ("mom120", 120):
        mom = np.full(n, np.nan)
        mom[win:] = px[win:] / px[:-win] - 1.0
        cap = np.where(mom < 0, 0.35, 1.0)
        cap[np.isnan(mom)] = 1.0
        out[tag] = np.concatenate([[cap[0]], cap[:-1]])
        if tag == "mom250":
            cont = np.clip(0.35 + 3.25 * mom, 0.35, 1.0)
            cont[np.isnan(cont)] = 1.0
            out["cont"] = np.concatenate([[cont[0]], cont[:-1]])
    return out


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
    ex = extra_caps(book)
    c1, c3, c6, c7 = caps["C1 趋势温和"], caps["C3 环境温和"], ex["mom250"], caps["C7 回撤上限"]

    variants = [
        ("F1 C6∧C1", {**kw, "cap_series": np.minimum(c6, c1)}),
        ("F2 C6∨C1", {**kw, "cap_series": np.maximum(c6, c1)}),
        ("F3 连续动量", {**kw, "cap_series": ex["cont"]}),
        ("F4 mom120", {**kw, "cap_series": ex["mom120"]}),
        ("F5 C6∨C3", {**kw, "cap_series": np.maximum(c6, c3)}),
        ("F6 C6+(C1∨C7)", {**kw, "cap_series": np.minimum(c6, np.maximum(c1, c7))}),
        ("F7 C6+E2", {**kw, "cap_series": c6, "loser_deweight": E2}),
    ]

    rows = [(name, ST.simulate_etf_portfolio(book, trades, **k2))
            for name, k2 in variants]

    print("\n=== 全区间指标 ===")
    print(f"{'变体':<14} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'月胜率':>6} {'暴露':>6}")
    b = base["stats"]
    print(f"{'基线':<14} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f} {b['avg_exposure']:>6.3f}")
    s6 = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=c6)["stats"]
    print(f"{'C6(参照)':<13} {s6['cagr']:>7.4f} {s6['maxdd']:>8.4f} {s6['sharpe']:>7.3f} "
          f"{s6['calmar']:>7.3f} {s6['monthly_win_rate']:>6.3f} {s6['avg_exposure']:>6.3f}")
    for name, r in rows:
        s = r["stats"]
        print(f"{name:<14} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f} {s['avg_exposure']:>6.3f}")

    print("\n=== 逐年收益 ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    down_years = [y for y, (a, _) in yb.items() if a < 0]
    for name, r in rows:
        yv = yearly(r["nav"], r["dates"], r["bench"])
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = ""
            if y in down_years and c > a + 0.004:
                mark = " ↑" + f"{(c - a) * 100:+.1f}pp"
            elif abs(c - a) > 0.005:
                mark = " *"
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline_parity_dev": nav_diff,
           "variants": {name: {k: r["stats"][k] for k in
                               ("cagr", "maxdd", "sharpe", "calmar", "avg_exposure")}
                        for name, r in rows}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_downyear3.json", "w"),
              ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downyear3.json")


if __name__ == "__main__":
    main()
