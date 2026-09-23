# -*- coding: utf-8 -*-
"""下跌年份不亏损实验台（L4，一次性产物，不影响生产口径）。

背景：生产结算曲线逐年收益——2017 -6.4% / 2018 -0.1% / 2022 -10.5% /
2023 -2.3% / 2024 -5.5% / 2026YTD -2.5%。用户目标：下跌年份可以不赚，
但不能亏损。

lab_downside 的教训（2026-09-23 复跑确认）：
  · floor 类降仓无效。floor_series 是暴露「下限」，只在波动目标触发时
    （已实现波动 > 目标/floor）才起作用；阴跌年份波动不高，暴露依然是 1，
    所以 V1-V4/V12 对下跌年份几乎零改善。
  · 入场闸类（V5/V6/V10）年化代价惨重（-3~ -5pp），因为它们把「反弹初段」
    的建仓也拦掉了，本质又回到 MA200 二元门控的踏空问题。
  · 冷却类（V8/V9）对年度收益几乎无影响。

本实验的新杠杆：**暴露上限 cap_series**（settlement.simulate_etf_portfolio
新增的向后兼容参数）。expo = min(波动目标 expo, cap[k])：
  - 与 floor 相反，cap 是「强制不超过」的硬上限，阴跌年份波动不高也生效；
  - 不改变任何一笔买卖的时点与标的，只缩放每日组合收益（等价于按比例
    整体减仓，减出的部分按 cash_yield 计现金收益）；
  - 状态变量全部右移一日（第 k 日只用 ≤k-1 信息），无前视。

变体（cap 的状态变量）：
  C1 趋势温和    cap = clip(0.85 + 2.0*(px/MA200 - 1), 0.40, 1.0)
  C2 趋势陡      cap = clip(0.80 + 3.0*(px/MA200 - 1), 0.25, 1.0)
  C3 环境温和    PANIC 0.30 / DEFEND 0.55 / NEUTRAL 0.80 / ATTACK 1.00
  C4 环境陡      PANIC 0.20 / DEFEND 0.45 / NEUTRAL 0.75 / ATTACK 1.00
  C5 趋势∧环境   min(C2, C4)
  C6 年线动量    基准 250 日收益 < 0 -> cap 0.35，否则 1.0
  C7 回撤上限    cap = clip(0.85 - 1.5*组合回撤, 0.30, 1.0)
  C8 三重取小    min(C2, C4, C7)

运行：cd engine && ../.venv/bin/python lab_downyear.py   （离线，只读缓存）
"""
import json
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import (load_trades_from_events, bench_ma_ratio,  # noqa: E402
                          yearly, dd_series)

RC = C.RISK_CONTROL
PF = C.PORTFOLIO


def build_caps(book: "ST.EtfBook", base: dict) -> dict:
    """全部 cap 序列，长度 = len(book.dates)，已右移一日（无前视）。"""
    n = len(book.dates)
    caps = {}

    r200 = bench_ma_ratio(book, 200)          # px/MA200（NaN = 窗口未满）
    trend_mild = np.clip(0.85 + 2.0 * (r200 - 1.0), 0.40, 1.0)
    trend_steep = np.clip(0.80 + 3.0 * (r200 - 1.0), 0.25, 1.0)
    for a in (trend_mild, trend_steep):
        a[np.isnan(a)] = 1.0                  # MA200 未满时不设限（早期数据）

    reg = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    env_mild_map = {"PANIC": 0.30, "DEFEND": 0.55, "NEUTRAL": 0.80, "ATTACK": 1.00}
    env_steep_map = {"PANIC": 0.20, "DEFEND": 0.45, "NEUTRAL": 0.75, "ATTACK": 1.00}
    env_mild = np.ones(n)
    env_steep = np.ones(n)
    for k in range(n):
        e = reg.get(book.dates[k - 1], {}).get("regime") if k >= 1 else None
        env_mild[k] = env_mild_map.get(e, 1.0)
        env_steep[k] = env_steep_map.get(e, 1.0)

    # 年线动量：基准 250 日收益（右移一日）
    sid = book.bench_etf
    px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(px)
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(n), idxs, px[idxs])
    mom250 = np.full(n, np.nan)
    mom250[250:] = px[250:] / px[:-250] - 1.0
    yr_cap = np.where(mom250 < 0, 0.35, 1.0)
    yr_cap[np.isnan(mom250)] = 1.0

    dd = dd_series(dict(zip(base["dates"], base["nav"])), book.dates)
    dd_cap = np.clip(0.85 - 1.5 * dd, 0.30, 1.0)

    def shift1(a):
        return np.concatenate([[a[0]], a[:-1]])

    caps["C1 趋势温和"] = shift1(trend_mild)
    caps["C2 趋势陡"] = shift1(trend_steep)
    caps["C3 环境温和"] = env_mild
    caps["C4 环境陡"] = env_steep
    caps["C5 趋势∧环境"] = np.minimum(caps["C2 趋势陡"], caps["C4 环境陡"])
    caps["C6 年线动量"] = shift1(yr_cap)
    caps["C7 回撤上限"] = shift1(dd_cap)
    caps["C8 三重取小"] = np.minimum(caps["C5 趋势∧环境"], caps["C7 回撤上限"])
    return caps


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    print(f"从 review.json 重建 {len(trades)} 笔交易")

    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)

    # 生产同参（score_tilt / max_new_per_day 缺一不可，否则基线对不上线上）
    kw = dict(start_date=prod_dates[0],
              max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
              score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])

    base = ST.simulate_etf_portfolio(book, trades, **kw)
    prod = json.load(open("../web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    nav_diff = max(diffs) if diffs else float("nan")
    print(f"基线复现: cagr={base['stats']['cagr']:.4f} maxdd={base['stats']['maxdd']:.4f} "
          f"| 生产 nav 最大偏差={nav_diff:.2e} "
          f"{'✓' if nav_diff < 1e-4 else '✗ 基线未对齐，结论不可信'}")

    caps = build_caps(book, base)

    rows = []
    for name, cap in caps.items():
        r = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=cap)
        rows.append((name, r, r["stats"], cap))

    print("\n=== 全区间指标（基线 = 生产口径） ===")
    print(f"{'变体':<12} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'月胜率':>6} {'暴露':>6} {'水下日':>6}")
    b = base["stats"]
    print(f"{'基线':<12} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f} {b['avg_exposure']:>6.3f} "
          f"{b['max_underwater_days']:>6}")
    for name, _, s, _c in rows:
        print(f"{name:<12} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f} {s['avg_exposure']:>6.3f} "
              f"{s['max_underwater_days']:>6}")

    print("\n=== 逐年收益：基线 -> 变体（括号内为基准） ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    yv_all = {name: yearly(r["nav"], r["dates"], r["bench"]) for name, r, _, _ in rows}
    down_years = [y for y, (a, _) in yb.items() if a < 0]
    print(f"基线亏损年份：{', '.join(sorted(down_years))}\n")
    for name, _r, _s, _c in rows:
        yv = yv_all[name]
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = ""
            if y in down_years and c > -0.005 >= a:
                mark = " ✓转正"
            elif abs(c - a) > 0.005:
                mark = " *"
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    # 下跌年份暴露诊断（最优意图：验证 cap 确实压低了熊年暴露）
    print("\n=== 基线亏损年份的平均暴露（基线 -> 变体） ===")
    inv_base = base["invested"]
    dmap = {d: i for i, d in enumerate(base["dates"])}
    for name, _r, _s, _c in rows:
        inv = _r["invested"]
        seg = []
        for y in down_years:
            ii = [dmap[d] for d in base["dates"] if d.startswith(y)]
            if not ii:
                continue
            a = float(np.mean([inv_base[i] for i in ii]))
            c = float(np.mean([inv[i] for i in ii]))
            seg.append(f"{y}: {a:.2f}->{c:.2f}")
        print(f"{name:<12} {'  '.join(seg)}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline_parity_dev": nav_diff,
           "baseline": {"cagr": b["cagr"], "maxdd": b["maxdd"], "sharpe": b["sharpe"],
                        "calmar": b["calmar"], "avg_exposure": b["avg_exposure"]},
           "variants": {name: {"cagr": s["cagr"], "maxdd": s["maxdd"],
                               "sharpe": s["sharpe"], "calmar": s["calmar"],
                               "avg_exposure": s["avg_exposure"],
                               "yearly": {y: round(v[0], 4) for y, v in yv_all[name].items()}}
                        for name, _r, s, _c in rows}}
    json.dump(out, open("../cache/lab_downyear.json", "w"), ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downyear.json")


if __name__ == "__main__":
    main()
