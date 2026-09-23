# -*- coding: utf-8 -*-
"""随机入市日一年期收益验证（L4，一次性产物，不影响生产口径）。

动机：整段曲线的年化（2016 起复利）依赖「第一年就开始满仓滚动」这一
隐含假设。真实用户可能从任意一天入市。本实验把 2018-01-01 之后到
「数据末端前一年」的**每个交易日**都当作一次全新入市（本金 1 元、
从该日起只按系统结论交易、此前结论一概不参与），统计此后 252 个
交易日的收益分布——这是对「年化是否可靠」最严格的滚动检验，比随机
抽几个点更严格；另做固定种子随机抽样 500 次复核统计量一致性。

口径（与生产逐位一致）：
  · trades 从 review.json 的 B/S 事件重建（load_trades_from_events，含
    sell_date 的真实持有区间）；
  · 生产风控全开：risk_parity / vol_target / score_tilt / F5 暴露上限
    （settlement.build_exposure_cap，与 review.py 主口径同源）；
  · 对照：基准 ETF（bench）同窗口买入持有。

运行：cd engine && ../.venv/bin/python lab_entry_dates.py
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import load_trades_from_events  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO
HORIZON = 252          # 一年 = 252 个交易日
START_FROM = "2018-01-01"


class BookView:
    """把账簿截断到 end_i（含）的只读视图。

    simulate_etf_portfolio 没有 end_date 参数、总是跑到数据末端；本实验要
    「从起点起恰好持有一年」，因此把 dates/idx 截到起点+252 个交易日，
    其余属性（series/sw_choice 等，全部按日期字符串键查）委托给原账簿。
    只截末端不截首端，索引 0..end_i 与全量账簿完全对齐，cap_series 切片
    即可对位。
    """

    def __init__(self, book, end_i: int):
        self._b = book
        self.dates = book.dates[:end_i + 1]
        self.idx = {d: i for i, d in enumerate(self.dates)}

    def __getattr__(self, name):
        return getattr(self._b, name)


def stats_of(xs: np.ndarray) -> dict:
    return {
        "n": int(xs.size),
        "mean": float(xs.mean()),
        "median": float(np.median(xs)),
        "std": float(xs.std(ddof=1)) if xs.size > 1 else 0.0,
        "p5": float(np.percentile(xs, 5)),
        "p25": float(np.percentile(xs, 25)),
        "p75": float(np.percentile(xs, 75)),
        "p95": float(np.percentile(xs, 95)),
        "min": float(xs.min()),
        "max": float(xs.max()),
        "win_rate": float((xs > 0).mean()),
    }


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    print(f"从 review.json 重建 {len(trades)} 笔交易")

    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)
    dates = book.dates
    n = len(dates)

    kw = dict(max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
              score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])

    # 生产口径的 F5 暴露上限（与 review.py 主口径同源）
    cap_f5 = ST.build_exposure_cap(book)

    # 基准 ETF 收盘序列（用于基准买入持有对照）
    bpx = np.array([book.close(book.bench_etf, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(bpx)
    idxs = np.where(ok)[0]
    bpx = np.interp(np.arange(n), idxs, bpx[idxs])

    # 环境状态（入市日所属 regime，来自前一日——无前视）
    reg = json.load(open(f"{C.CACHE_DIR}/regime.json"))

    # 第一个 >= START_FROM 的交易日（面板起点早于 2018，不能用 .get 兜底到 0）
    first_i = int(np.searchsorted(np.array(dates), START_FROM, side="left"))
    first_i = min(first_i, n - 1)
    last_start = n - 1 - HORIZON
    starts = list(range(first_i, last_start + 1))
    print(f"入市起点：{dates[first_i]} .. {dates[last_start]}，共 {len(starts)} 个交易日（全样本滚动）")

    sys_rets, bench_rets, base_rets = [], [], []
    rec = []
    for s_i in starts:
        d = dates[s_i]
        # 数据截断到「起点 + 252 个交易日」：起点起恰好持有一年，
        # 防止 simulate 跑到全量数据末端把多年累计误当一年收益
        end_i = min(s_i + HORIZON, n - 1)
        view = BookView(book, end_i)
        cap_v = cap_f5[:end_i + 1]
        # 系统（生产口径，含 F5）
        r_cap = ST.simulate_etf_portfolio(view, trades, start_date=d, **kw, cap_series=cap_v)
        nav = r_cap["nav"]
        sys_ret = nav[-1] / nav[0] - 1.0
        # 无 cap 对照（隔离 F5 在一年期窗口的作用）
        r_base = ST.simulate_etf_portfolio(view, trades, start_date=d, **kw)
        base_ret = r_base["nav"][-1] / r_base["nav"][0] - 1.0
        # 基准买入持有（同窗口）
        bench_ret = bpx[s_i + HORIZON] / bpx[s_i] - 1.0
        e = reg.get(dates[s_i - 1], {}).get("regime", "?") if s_i >= 1 else "?"
        sys_rets.append(sys_ret)
        base_rets.append(base_ret)
        bench_rets.append(bench_ret)
        rec.append({"date": d, "regime": e, "sys": sys_ret, "base": base_ret,
                    "bench": bench_ret})

    sys_arr = np.array(sys_rets)
    base_arr = np.array(base_rets)
    bench_arr = np.array(bench_rets)
    ex_arr = sys_arr - bench_arr

    print(f"\n=== 一年期（252 交易日）收益分布：{len(starts)} 个入市起点 ===")
    print(f"{'口径':<16}{'均值':>8}{'中位':>8}{'std':>7}{'p5':>8}{'p25':>8}{'p75':>8}{'p95':>8}{'最差':>9}{'最好':>9}{'为正':>7}")
    for label, arr in (("系统（含F5）", sys_arr), ("无cap对照", base_arr), ("基准买入持有", bench_arr),
                       ("系统-基准超额", ex_arr)):
        s = stats_of(arr)
        print(f"{label:<16}{s['mean']*100:>7.2f}%{s['median']*100:>7.2f}%{s['std']*100:>6.1f}%"
              f"{s['p5']*100:>7.2f}%{s['p25']*100:>7.2f}%{s['p75']*100:>7.2f}%{s['p95']*100:>7.2f}%"
              f"{s['min']*100:>8.2f}%{s['max']*100:>8.2f}%{s['win_rate']*100:>6.1f}%")

    # 随机抽样复核（500 次，固定种子）：验证全样本统计量与随机抽样一致
    rng = np.random.default_rng(42)
    pick = rng.choice(len(starts), size=min(500, len(starts)), replace=False)
    print(f"\n=== 随机抽样复核（{pick.size} 个起点，seed=42） ===")
    for label, arr in (("系统（含F5）", sys_arr[pick]), ("基准买入持有", bench_arr[pick])):
        s = stats_of(arr)
        print(f"{label:<16} 均值 {s['mean']*100:.2f}% · 中位 {s['median']*100:.2f}% · "
              f"为正 {s['win_rate']*100:.1f}% · 最差 {s['min']*100:.2f}%")

    # 按入市年份分段
    print("\n=== 按入市年份（系统 vs 基准，均值 / 为正比例 / 样本数） ===")
    by_year = {}
    for r in rec:
        by_year.setdefault(r["date"][:4], []).append(r)
    for y in sorted(by_year):
        arr = np.array([x["sys"] for x in by_year[y]])
        barr = np.array([x["bench"] for x in by_year[y]])
        print(f"{y}: 系统 {arr.mean()*100:>7.2f}%（正 {(arr>0).mean()*100:>5.1f}%，n={len(arr):>3}）"
              f"  基准 {barr.mean()*100:>7.2f}%（正 {(barr>0).mean()*100:>5.1f}%）")

    # 按入市日环境分段
    print("\n=== 按入市日环境（前一日 regime） ===")
    by_reg = {}
    for r in rec:
        by_reg.setdefault(r["regime"], []).append(r)
    for g in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC", "?"):
        if g not in by_reg:
            continue
        arr = np.array([x["sys"] for x in by_reg[g]])
        barr = np.array([x["bench"] for x in by_reg[g]])
        print(f"{g:<8}: 系统 {arr.mean()*100:>7.2f}%（正 {(arr>0).mean()*100:>5.1f}%，n={len(arr):>3}）"
              f"  基准 {barr.mean()*100:>7.2f}%（正 {(barr>0).mean()*100:>5.1f}%）")

    # 最差 5 个起点（系统的真实尾部风险）
    worst = sorted(rec, key=lambda x: x["sys"])[:5]
    print("\n=== 最差 5 个入市起点 ===")
    for w in worst:
        print(f"  {w['date']}（{w['regime']}）系统 {w['sys']*100:.2f}% · 基准 {w['bench']*100:.2f}%")

    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "horizon_days": HORIZON,
        "start_from": dates[first_i], "start_to": dates[last_start],
        "n_starts": len(starts),
        "stats": {"sys": stats_of(sys_arr), "base": stats_of(base_arr),
                  "bench": stats_of(bench_arr), "excess": stats_of(ex_arr)},
        "by_year": {y: {"sys_mean": float(np.mean([x["sys"] for x in v])),
                        "sys_win": float(np.mean([x["sys"] > 0 for x in v])),
                        "bench_mean": float(np.mean([x["bench"] for x in v])),
                        "n": len(v)} for y, v in sorted(by_year.items())},
        "by_regime": {g: {"sys_mean": float(np.mean([x["sys"] for x in v])),
                          "sys_win": float(np.mean([x["sys"] > 0 for x in v])),
                          "bench_mean": float(np.mean([x["bench"] for x in v])),
                          "n": len(v)} for g, v in by_reg.items()},
        "worst5": worst[:5],
    }
    json.dump(out, open(f"{C.CACHE_DIR}/lab_entry_dates.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_entry_dates.json")


if __name__ == "__main__":
    main()
