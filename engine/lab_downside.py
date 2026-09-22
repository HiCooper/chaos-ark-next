"""下行市场表现优化实验台（L4，一次性产物，不影响生产口径）。

背景：复盘显示组合在下行年份亏损——组合口径 2016 -5.3% / 2017 -7.8% /
2022 -11.0% / 2023 -3.3% / 2024 -9.5% / 2026 -15.3%。历史反事实已证明
止损/熔断/提高信号门槛都更差（不降总暴露只洗仓位），MA200 二元门控也
被否（年化 5.15%、回撤 -64%，急跌后踏空反弹）。

本实验只测「连续降暴露」类变体：全部通过 settlement.simulate_etf_portfolio
的 floor_series 参数实现，只改变暴露水平，不改变任何一笔买卖的时点与标的。
交易清单从生产 review.json 的 B/S 事件重建（同源），基线先复现生产净值做校验。

变体：
  V1 固定 floor=0.75          —— 现有 floor_scan 的最优档，作参照
  V2 趋势连续 floor           —— 基准 ETF 相对自身 MA200 的偏离连续映射：
                                 floor = clip(0.8 + 2.0*(px/ma-1), 0.25, 0.8)
                                 （二元门控的连续版，允许随时回流）
  V3 回撤比例 floor           —— 组合自身回撤越深、总暴露越低：
                                 floor = clip(0.8 - 1.2*dd, 0.25, 0.8)
                                 （dd 用基线净值逐日计算并右移一日，无前视；
                                 回撤是实盘可观测状态，不构成循环依赖）
  V4 V2+V3 取小              —— 两个状态变量取更保守者

运行：python lab_downside.py  （离线，只读 L1/L3 缓存）
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import settlement as ST  # noqa: E402

RC = C.RISK_CONTROL if hasattr(C, "RISK_CONTROL") else {
    "risk_parity": True, "rp_window": 60, "vol_target": 0.16,
    "vol_window": 60, "vol_floor": 0.8, "cash_yield": 0.015,
}


def load_trades_from_events() -> tuple[list[dict], list[str]]:
    """从生产 review.json 的 B/S 事件重建 simulate 所需的交易清单。

    B 事件给买入索引/标的/周期/评分/动作；S 事件用 (买入日, 行业, 标的) 对回
    找到真实卖出索引。open-edge（尚未卖出）的持仓不给 sell_date，让 simulate
    按建议周期推算到期日——与生产入账口径一致。
    """
    r = json.load(open(f"{C.OUT_DIR}/review.json")) if hasattr(C, "OUT_DIR") else \
        json.load(open("../web/public/data/review.json"))
    stl = r["settlement"]
    dates = stl["curve"]["dates"]
    events = stl["events"]

    sells = defaultdict(list)  # (bd, sw, sid) -> [sell_index]（FIFO 配对）
    for e in events:
        if e["k"] == "S" and e.get("bd"):
            sells[(e["bd"], e["sw"], e["sid"])].append(e["i"])

    trades = []
    for e in events:
        if e["k"] != "B":
            continue
        # B 事件的 i 是买入日（T+1 开盘）；生产交易清单的 date 是结论日（买入前一天），
        # simulate 内部会再做 i0+1。错用买入日会让全部成交晚一天、净值系统性偏离。
        bd = dates[e["i"] - 1]
        key = (dates[e["i"]], e["sw"], e["sid"])
        t = {
            "date": bd, "sw_name": e["sw"], "horizon": e["h"],
            "composite": e.get("sc", 0), "etf_sid": e["sid"],
            "action": e.get("a"),
        }
        ss = sells.get(key)
        if ss:
            t["sell_date"] = dates[ss.pop(0)]
        trades.append(t)
    return trades, dates


def bench_ma_ratio(book: "ST.EtfBook", window: int = 200) -> np.ndarray:
    """基准 ETF 相对自身 MA(window) 的逐日比值 px/ma（无前视：第 k 日用 ≤k 的数据）。"""
    sid = book.bench_etf
    n = len(book.dates)
    px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(px)
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(n), idxs, px[idxs])
    ma = np.convolve(px, np.ones(window) / window, mode="full")[:n]
    ma = np.where(np.arange(n) >= window - 1, ma, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return px / ma


def dd_series(nav_by_day: dict, dates: list[str]) -> np.ndarray:
    """组合回撤深度序列（右移一日：第 k 日用 ≤k-1 的净值，无前视）。"""
    nav = np.array([nav_by_day.get(d, np.nan) for d in dates], dtype=float)
    ok = ~np.isnan(nav)
    idxs = np.where(ok)[0]
    nav = np.interp(np.arange(len(nav)), idxs, nav[idxs])
    peak = np.maximum.accumulate(np.where(np.isnan(nav), 1.0, nav))
    dd = 1.0 - nav / peak
    return np.concatenate([[0.0], dd[:-1]])  # 右移一日


def yearly(nav: list, dates: list[str], bench: list) -> dict:
    yr = {}
    for i, d in enumerate(dates):
        y = d[:4]
        yr.setdefault(y, {"first": i, "last": i,
                          "base": nav[i - 1] if i > 0 else nav[0],
                          "bbase": bench[i - 1] if i > 0 else bench[0]})
        yr[y]["last"] = i
    return {y: (nav[v["last"]] / v["base"] - 1,
                bench[v["last"]] / v["bbase"] - 1)
            for y, v in yr.items()}


def ma_ratio_of(book: "ST.EtfBook", window: int = 120) -> dict:
    """全部标的相对自身 MA(window) 的逐日比值（无前视：第 k 日用 ≤k 数据）。"""
    out = {}
    n = len(book.dates)
    for sid in book.series.keys():
        px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
        ok = ~np.isnan(px)
        if ok.sum() < window + 10:
            continue
        idxs = np.where(ok)[0]
        p = np.interp(np.arange(n), idxs, px[idxs])
        ma = np.convolve(p, np.ones(window) / window, mode="full")[:n]
        ma = np.where(np.arange(n) >= window - 1, ma, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            r = p / ma
        out[sid] = r
    return out


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    print(f"从 review.json 重建 {len(trades)} 笔交易")

    book = ST.EtfBook(ST.load_etf_panel(f"{C.CACHE_DIR}/etf_panel.json"))
    kw = dict(start_date=prod_dates[0],
              max_holdings=32,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"])

    # ---- 基线复现校验（floor_series=None 应与生产 nav 一致）----
    base = ST.simulate_etf_portfolio(book, trades, **kw)
    prod = json.load(open("../web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    nav_diff = max(diffs) if diffs else float("nan")
    print(f"基线复现: cagr={base['stats']['cagr']:.4f} maxdd={base['stats']['maxdd']:.4f} "
          f"| 生产 nav 最大偏差={nav_diff:.2e}")

    # ---- 变体 ----
    ma_ratio = bench_ma_ratio(book)
    n = len(book.dates)
    trend_floor = np.clip(0.8 + 2.0 * (ma_ratio - 1.0), 0.25, 0.8)
    trend_floor = np.where(np.isnan(trend_floor), 0.8, trend_floor)
    # 无前视：第 k 日只用 ≤k-1 的趋势状态
    trend_floor = np.concatenate([[0.8], trend_floor[:-1]])

    dd = dd_series(dict(zip(base["dates"], base["nav"])), book.dates)
    dd_floor = np.clip(0.8 - 1.2 * dd, 0.25, 0.8)

    # V12 环境自适应 floor：用引擎自己的市场环境判定（regime.json，实盘可得）
    reg = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    floor_by_env = {"PANIC": 0.35, "DEFEND": 0.60, "ATTACK": 0.80, "NEUTRAL": 0.80}
    env_floor = np.array([
        floor_by_env.get(reg.get(book.dates[max(0, k - 1)], {}).get("regime"), 0.8)
        for k in range(n)
    ])

    combo_floor = np.minimum(trend_floor, dd_floor)

    # ---- 行业级入场闸变体：不给跌破自身趋势的行业开新仓 ----
    r120 = ma_ratio_of(book, 120)
    r200 = ma_ratio_of(book, 200)
    bench_r = ma_ratio_of(book, 200).get(book.bench_etf)

    # 跌透闸：价格距自身 120 日滚动高点的回撤 >15% 视为「已崩主线」，禁新仓。
    # 与 V5 的 MA 闸不同：允许买趋势内的回调（高于 MA），只拦深崩后的抄底。
    knife: dict = {}
    n_days = len(book.dates)
    from numpy.lib.stride_tricks import sliding_window_view

    def drawdown120(book, sid):
        px = np.array([book.close(sid, i) or np.nan for i in range(n_days)], dtype=float)
        ok = ~np.isnan(px)
        if ok.sum() < 30:
            return None
        idxs = np.where(ok)[0]
        p = np.interp(np.arange(n_days), idxs, px[idxs])
        pad = np.concatenate([np.full(119, p[0]), p])
        roll_max = sliding_window_view(pad, 120).max(axis=1)
        return p / roll_max  # 越小 = 跌得越深

    for sid in book.series.keys():
        dd120 = drawdown120(book, sid)
        if dd120 is not None:
            knife[sid] = dd120

    def gate_knife15(sid: str, k: int):
        dd = knife.get(sid)
        return bool(dd is None or k >= len(dd) or np.isnan(dd[k]) or dd[k] > 0.85)

    def gate_knife25(sid: str, k: int):
        dd = knife.get(sid)
        return bool(dd is None or k >= len(dd) or np.isnan(dd[k]) or dd[k] > 0.75)

    def gate_ind120(sid: str, k: int):
        r = r120.get(sid)
        return bool(r is not None and k < len(r) and not np.isnan(r[k]) and r[k] > 1.0)

    def gate_ind_bench(sid: str, k: int):
        if not gate_ind120(sid, k):
            return False
        b = bench_r[k - 1] if (bench_r is not None and k >= 1 and k - 1 < len(bench_r)) else None
        return bool(b is not None and not np.isnan(b) and b > 1.0)

    def gate_ind_disc(sid: str, k: int):
        """折价入场闸：行业低于 MA120 时禁止「追高型（BUY_NOW）」新仓，仍允许埋伏。"""
        r = r120.get(sid)
        if r is None or k >= len(r) or np.isnan(r[k]):
            return True
        return not (r[k] < 1.0)

    variants = [
        ("V1 固定floor=0.75", {**kw, "vol_floor": 0.75}),
        ("V2 趋势连续floor", {**kw, "floor_series": trend_floor}),
        ("V3 回撤比例floor", {**kw, "floor_series": dd_floor}),
        ("V4 趋势+回撤取小", {**kw, "floor_series": combo_floor}),
        ("V5 行业MA120入场闸", {**kw, "entry_gate": gate_ind120}),
        ("V6 行业MA120+大盘MA200", {**kw, "entry_gate": gate_ind_bench}),
        ("V7 折价禁追高(BUY_NOW)", {**kw, "entry_gate": gate_ind_disc}),
        ("V8a 亏损冷却20日", {**kw, "sid_cooldown": 20}),
        ("V8b 亏损冷却40日", {**kw, "sid_cooldown": 40}),
        ("V8c 亏损冷却60日", {**kw, "sid_cooldown": 60}),
        ("V9 冷却40日+floor0.75", {**kw, "sid_cooldown": 40, "vol_floor": 0.75}),
        ("V10 跌透闸-15%", {**kw, "entry_gate": gate_knife15}),
        ("V11 跌透闸-25%", {**kw, "entry_gate": gate_knife25}),
        ("V12 环境自适应floor", {**kw, "floor_series": env_floor}),
    ]

    rows = []
    for name, k2 in variants:
        r = ST.simulate_etf_portfolio(book, trades, **k2)
        s = r["stats"]
        rows.append((name, r, s))

    # ---- 汇总 ----
    print("\n=== 全区间指标 ===")
    print(f"{'变体':<18} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'月胜率':>6} {'暴露':>6} {'水下日':>6}")
    b = base["stats"]
    print(f"{'基线(floor=0.8)':<16} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f} {b['avg_exposure']:>6.3f} "
          f"{b['max_underwater_days']:>6}")
    for name, _, s in rows:
        print(f"{name:<18} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f} {s['avg_exposure']:>6.3f} "
              f"{s['max_underwater_days']:>6}")

    print("\n=== 逐年组合收益（基线 -> 最优变体，基准） ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    for name, r, _ in rows:
        yv = yearly(r["nav"], r["dates"], r["bench"])
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = " *" if abs(c - a) > 0.005 else ""
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    # 落盘供复盘引用
    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline": {"cagr": b["cagr"], "maxdd": b["maxdd"],
                        "sharpe": b["sharpe"], "calmar": b["calmar"]},
           "variants": {name: {"cagr": s["cagr"], "maxdd": s["maxdd"],
                               "sharpe": s["sharpe"], "calmar": s["calmar"],
                               "avg_exposure": s["avg_exposure"]}
                        for name, _, s in rows}}
    json.dump(out, open("../cache/lab_downside.json", "w"), ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downside.json")


if __name__ == "__main__":
    main()
