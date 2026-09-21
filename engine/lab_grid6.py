# -*- coding: utf-8 -*-
"""降回撤方向 6：最终候选的增强对比。"""
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

    hdr = (f"{'方案':<32} | {'运行期cagr':>9} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
           f"{'水下天':>6} {'月胜率':>6} {'暴露':>5}")
    print("\n" + hdr); print("-" * len(hdr))

    def row(tag, kw):
        s = L.sim(book, trades, start_date=sd, **kw)["stats"]
        e = s["effective"]
        print(f"{tag:<32} | {e['cagr']:>9.2%} {e['maxdd']:>8.2%} {e['sharpe']:>7.2f} "
              f"{e['calmar']:>7.2f} {e['max_underwater_days']:>6} "
              f"{str(e['monthly_win_rate']):>6} {str(s.get('avg_exposure')):>5}")
        return s

    row("baseline", {})
    CAND = dict(risk_parity=True, rp_window=60, rp_rebalance=5,
                vol_target=0.16, vol_window=60, vol_floor=0.80)
    row("候选: RP + volT16 floor.80", CAND)
    row("  + 单仓cap4", {**CAND, "rp_cap": 4.0})
    row("  RP下行波动权重", {**CAND, "rp_downside": True})
    row("  volT用实时持仓波动", {**CAND, "vol_target_live": True})
    row("  RP下行 + 实时波动", {**CAND, "rp_downside": True, "vol_target_live": True})
    row("  + 极浅navMA250x0.85", {**CAND, "nav_ma_window": 250, "nav_ma_scale": 0.85})
    row("  + navMA120x0.8", {**CAND, "nav_ma_window": 120, "nav_ma_scale": 0.8})
    row("  floor .75", {**CAND, "vol_floor": 0.75})
    row("  floor .85", {**CAND, "vol_floor": 0.85})
    row("更激进: floor .70", {**CAND, "vol_floor": 0.70})


if __name__ == "__main__":
    main()
