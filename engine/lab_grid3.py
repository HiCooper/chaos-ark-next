# -*- coding: utf-8 -*-
"""降回撤方向 3：风险平价之上的暴露管理（波动目标 / 下行波动 / 净值趋势）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel  # noqa: E402
import lab_dd as L  # noqa: E402


def main():
    book = EtfBook(load_etf_panel())
    days = L.load_history()
    trades = L.build_trades(book, days)
    sd = days[0]["date"]

    hdr = (f"{'方案':<30} | {'全期 cagr':>9} {'maxdd':>8} {'sharpe':>7} | "
           f"{'运行期 cagr':>10} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
           f"{'水下天':>6} {'月胜率':>6} {'均暴露':>6}")
    print("\n" + hdr)
    print("-" * len(hdr))
    def row(tag, s):
        e = s.get("effective") or {}
        print(f"{tag:<30} | {s['cagr']:>9.2%} {s['maxdd']:>8.2%} {s['sharpe']:>7.2f} | "
              f"{e.get('cagr', 0):>10.2%} {e.get('maxdd', 0):>8.2%} "
              f"{e.get('sharpe', 0):>7.2f} {e.get('calmar', 0):>7.2f} "
              f"{e.get('max_underwater_days', 0):>6} "
              f"{str(e.get('monthly_win_rate')):>6} {str(s.get('avg_exposure')):>6}")

    RP = dict(risk_parity=True, rp_window=60)
    cases = [
        ("baseline", {}),
        ("RP", RP),
        ("RP + volT 20%", {**RP, "vol_target": 0.20, "vol_window": 60, "vol_floor": 0.6}),
        ("RP + volT 18%", {**RP, "vol_target": 0.18, "vol_window": 60, "vol_floor": 0.6}),
        ("RP + volT 16%", {**RP, "vol_target": 0.16, "vol_window": 60, "vol_floor": 0.6}),
        ("RP + volT 14%", {**RP, "vol_target": 0.14, "vol_window": 60, "vol_floor": 0.6}),
        ("RP + volT 16% floor.8", {**RP, "vol_target": 0.16, "vol_window": 60, "vol_floor": 0.8}),
        ("RP + 下行volT 12%", {**RP, "semi_vol_target": 0.12, "semi_window": 60, "vol_floor": 0.6}),
        ("RP + 下行volT 10%", {**RP, "semi_vol_target": 0.10, "semi_window": 60, "vol_floor": 0.6}),
        ("RP + 下行volT 14%", {**RP, "semi_vol_target": 0.14, "semi_window": 60, "vol_floor": 0.6}),
        ("RP + navMA60 x0.6", {**RP, "nav_ma_window": 60, "nav_ma_scale": 0.6}),
        ("RP + navMA120 x0.6", {**RP, "nav_ma_window": 120, "nav_ma_scale": 0.6}),
        ("RP + navMA60 x0.4", {**RP, "nav_ma_window": 60, "nav_ma_scale": 0.4}),
        ("RP + navMA60 x0.6 + volT18", {**RP, "nav_ma_window": 60, "nav_ma_scale": 0.6,
                                         "vol_target": 0.18, "vol_window": 60, "vol_floor": 0.6}),
        ("RP + 下行volT12 + navMA60x0.6",
         {**RP, "semi_vol_target": 0.12, "semi_window": 60, "vol_floor": 0.6,
          "nav_ma_window": 60, "nav_ma_scale": 0.6}),
    ]
    res = {}
    for tag, kw in cases:
        r = L.sim(book, trades, start_date=sd, **kw)
        res[tag] = r
        row(tag, r["stats"])
    return book, trades, res


if __name__ == "__main__":
    main()
