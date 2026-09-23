# -*- coding: utf-8 -*-
"""F5（动量∨环境暴露上限）全周期稳健性验证。

回答「策略是否只在设计样本上有效」：
  R1 全周期逐年表（2016→2026，与基线逐段对照）
  R2 参数敏感性：动量地板 {0.25,0.35,0.45} × 环境档位 {温和,陡,更陡} × 合并 {max,min}
     —— 若年化/回撤对参数不敏感（宽平台），则非精调过拟合
  R3 分时段稳定性：2016-2019 / 2020-2022 / 2023-2026 三段子区间年化与回撤
  R4 成本/假设敏感性：现金收益 0 vs 1.5% vs 3%；单边成本 ×2
  R5 无前视复检：cap 序列整体再右移一日（两日滞后），收益衰减幅度
     —— 若明显衰减，说明规则依赖了过近的信息（隐性前视风险）

运行：绝对路径直接跑。
"""
import json
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import load_trades_from_events, yearly  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO

ENV_MILD = {"PANIC": 0.30, "DEFEND": 0.55, "NEUTRAL": 0.80, "ATTACK": 1.00}
ENV_STEEP = {"PANIC": 0.20, "DEFEND": 0.45, "NEUTRAL": 0.75, "ATTACK": 1.00}
ENV_VSTEEP = {"PANIC": 0.10, "DEFEND": 0.35, "NEUTRAL": 0.70, "ATTACK": 1.00}


def mom_cap(book, window=250, floor=0.35, extra_lag=0):
    """mom cap。默认已含 1 日右移（无前视基准）；extra_lag 是额外滞后。"""
    n = len(book.dates)
    sid = book.bench_etf
    px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(px)
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(n), idxs, px[idxs])
    mom = np.full(n, np.nan)
    mom[window:] = px[window:] / px[:-window] - 1.0
    cap = np.where(mom < 0, floor, 1.0)
    cap[np.isnan(mom)] = 1.0
    cap = np.concatenate([[cap[0]], cap[:-1]])   # 基础右移一日（无前视）
    for _ in range(extra_lag):
        cap = np.concatenate([[cap[0]], cap[:-1]])
    return cap


def env_cap(book, mapping):
    n = len(book.dates)
    reg = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    cap = np.ones(n)
    for k in range(n):
        e = reg.get(book.dates[k - 1], {}).get("regime") if k >= 1 else None
        cap[k] = mapping.get(e, 1.0)
    return cap


def subperiods(nav, dates, bench):
    segs = [("2016-2019", "2016", "2019"), ("2020-2022", "2020", "2022"),
            ("2023-2026", "2023", "2026")]
    out = {}
    nav_a = np.array(nav, dtype=float)
    for tag, y0, y1 in segs:
        ii = [i for i, d in enumerate(dates) if y0 <= d[:4] <= y1]
        if len(ii) < 100:
            continue
        s, e = ii[0] - 1, ii[-1]
        yrs = (e - s) / 252.0
        cagr = float(nav_a[e] / nav_a[s]) ** (1.0 / yrs) - 1.0
        seg = nav_a[s:e + 1]
        peak = np.maximum.accumulate(seg)
        mdd = float((seg / peak - 1.0).min())
        bcagr = float(bench[e] / bench[s]) ** (1.0 / yrs) - 1.0
        out[tag] = {"cagr": round(cagr, 4), "maxdd": round(mdd, 4),
                    "bench_cagr": round(bcagr, 4)}
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
    print(f"基线复现偏差={max(diffs):.2e}")

    # ---------------- R2 参数敏感性 ----------------
    print("\n=== R2 参数敏感性（全周期 年化/回撤/Sharpe） ===")
    print(f"{'变体':<28} {'cagr':>7} {'maxdd':>8} {'sharpe':>7}")
    b = base["stats"]
    print(f"{'基线':<28} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f}")
    grid = []
    for fl in (0.25, 0.35, 0.45):
        for tag, mapping in (("温和", ENV_MILD), ("陡", ENV_STEEP), ("更陡", ENV_VSTEEP)):
            for comb in ("max", "min"):
                c = np.maximum(mom_cap(book, 250, fl), env_cap(book, mapping)) \
                    if comb == "max" else \
                    np.minimum(mom_cap(book, 250, fl), env_cap(book, mapping))
                grid.append((f"floor{fl}-{tag}-{comb}", c))
    results = {}
    for name, c in grid:
        r = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=c)
        s = r["stats"]
        results[name] = s
        print(f"{name:<28} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f}")

    # ---------------- R1 全周期逐年（推荐参数 floor0.35-温和-max = F5） ----------------
    f5 = ST.simulate_etf_portfolio(book, trades, **kw,
                                   cap_series=grid[[g[0] for g in grid].index(
                                       "floor0.35-温和-max")][1])
    print("\n=== R1 全周期逐年：基线 -> F5 ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    yv = yearly(f5["nav"], f5["dates"], f5["bench"])
    for y in sorted(yb):
        a, bb = yb[y]
        c, _ = yv[y]
        print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%)")

    # ---------------- R3 分时段 ----------------
    print("\n=== R3 分时段（年化/最大回撤） ===")
    sb = subperiods(base["nav"], base["dates"], base["bench"])
    sf = subperiods(f5["nav"], f5["dates"], f5["bench"])
    for tag in sb:
        print(f"  {tag}: 基线 {sb[tag]['cagr']*100:>6.2f}%/{sb[tag]['maxdd']*100:>6.1f}%"
              f"  ->  F5 {sf[tag]['cagr']*100:>6.2f}%/{sf[tag]['maxdd']*100:>6.1f}%"
              f"  (基准年化 {sf[tag]['bench_cagr']*100:>6.2f}%)")

    # ---------------- R4 假设敏感性 ----------------
    print("\n=== R4 假设敏感性（F5 参数） ===")
    cap_f5 = grid[[g[0] for g in grid].index("floor0.35-温和-max")][1]
    for tag, over in (("现金收益0%", {"cash_yield": 0.0}),
                      ("现金收益3%", {"cash_yield": 0.03})):
        r = ST.simulate_etf_portfolio(book, trades, **{**kw, **over}, cap_series=cap_f5)
        s = r["stats"]
        print(f"  {tag:<10} cagr={s['cagr']:.4f} maxdd={s['maxdd']:.4f}")
    # 双倍交易成本（间接：把 ROUND_TRIP_COST 翻倍后重跑）
    old = C.ROUND_TRIP_COST
    C.ROUND_TRIP_COST = old * 2
    r = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=cap_f5)
    s = r["stats"]
    C.ROUND_TRIP_COST = old
    print(f"  {'成本×2':<10} cagr={s['cagr']:.4f} maxdd={s['maxdd']:.4f}")

    # ---------------- R5 隐性前视复检 ----------------
    print("\n=== R5 滞后复检（相对无前视基准再右移 0/1/5/10 日；lag-1 = 含同日前视） ===")
    for lag in (0, 1, 5, 10):
        c_lag = cap_f5.copy()
        for _ in range(lag):
            c_lag = np.concatenate([[c_lag[0]], c_lag[:-1]])
        r = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=c_lag)
        s = r["stats"]
        print(f"  额外滞后{lag:>2}日: cagr={s['cagr']:.4f} maxdd={s['maxdd']:.4f}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "grid": {n: {"cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s["sharpe"]}
                    for n, s in results.items()},
           "subperiods": {"baseline": sb, "f5": sf}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_downyear5.json", "w"),
              ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downyear5.json")


if __name__ == "__main__":
    main()
