# -*- coding: utf-8 -*-
"""降回撤方向网格：单维扫描，先看哪个方向真有边际效果。"""
from __future__ import annotations

import sys, os
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel  # noqa: E402
import lab_dd as L  # noqa: E402

def main():
    book = EtfBook(load_etf_panel())
    days = L.load_history()
    trades = L.build_trades(book, days)
    sd = days[0]["date"]

    base = L.sim(book, trades, start_date=sd)
    b = base["stats"]
    print(f"\n{'方案':<34} {'cagr':>8} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'vol':>7} {'月胜率':>7} {'水下天':>7} {'成交':>5} {'均仓':>5} {'均暴露':>6}")
    def row(tag, s):
        print(f"{tag:<34} {s['cagr']:>8.2%} {s['maxdd']:>8.2%} {s['sharpe']:>7.2f} "
              f"{s['calmar']:>7.2f} {s['vol']:>7.2%} {str(s['monthly_win_rate']):>7} "
              f"{s['max_underwater_days']:>7} {s['executed_trades']:>5} "
              f"{str(s['avg_holdings']):>5} {str(s.get('avg_exposure')):>6}")
    row("baseline（线上口径）", b)

    cases = [
        ("volT 22% w60 floor.6", dict(vol_target=0.22, vol_window=60, vol_floor=0.6)),
        ("volT 20% w60 floor.6", dict(vol_target=0.20, vol_window=60, vol_floor=0.6)),
        ("volT 18% w60 floor.6", dict(vol_target=0.18, vol_window=60, vol_floor=0.6)),
        ("volT 16% w60 floor.6", dict(vol_target=0.16, vol_window=60, vol_floor=0.6)),
        ("volT 18% w120 floor.6", dict(vol_target=0.18, vol_window=120, vol_floor=0.6)),
        ("volT 20% w120 floor.6", dict(vol_target=0.20, vol_window=120, vol_floor=0.6)),
        ("volT 18% w250 floor.6", dict(vol_target=0.18, vol_window=250, vol_floor=0.6)),
        ("riskParity w60", dict(risk_parity=True, rp_window=60)),
        ("riskParity w120", dict(risk_parity=True, rp_window=120)),
        ("trail 20%", dict(trail_stop=0.20)),
        ("trail 15%", dict(trail_stop=0.15)),
        ("trail 12%", dict(trail_stop=0.12)),
        ("trail 10%", dict(trail_stop=0.10)),
        ("ddThrottle 15% -> 0.5", dict(dd_throttle=0.15, dd_restore=0.5)),
        ("ddThrottle 12% -> 0.5", dict(dd_throttle=0.12, dd_restore=0.5)),
        ("ddThrottle 10% -> 0.6", dict(dd_throttle=0.10, dd_restore=0.6)),
        ("ddThrottle 20% -> 0.6", dict(dd_throttle=0.20, dd_restore=0.6)),
        ("minComp 66", dict(min_composite=66)),
        ("minComp 68", dict(min_composite=68)),
        ("minComp 70", dict(min_composite=70)),
        ("maxHold 12", dict(max_holdings=12)),
        ("maxHold 16", dict(max_holdings=16)),
        ("maxHold 20", dict(max_holdings=20)),
        ("volT18% + RP", dict(vol_target=0.18, vol_window=60, vol_floor=0.6, risk_parity=True)),
        ("volT18% + trail15", dict(vol_target=0.18, vol_window=60, vol_floor=0.6, trail_stop=0.15)),
    ]
    for tag, kw in cases:
        r = L.sim(book, trades, start_date=sd, **kw)
        row(tag, r["stats"])

if __name__ == "__main__":
    main()
