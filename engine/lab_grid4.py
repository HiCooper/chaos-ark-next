# -*- coding: utf-8 -*-
"""降回撤方向 4：最优候选的参数稳健性扫描（拒绝过拟合的参数尖峰）。"""
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

    hdr = (f"{'参数':<26} | {'运行期cagr':>9} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
           f"{'水下天':>6} {'月胜率':>6} {'均暴露':>6}")
    print("\n" + hdr); print("-" * len(hdr))

    def run(tag, kw):
        r = L.sim(book, trades, start_date=sd, **kw)["stats"]
        e = r["effective"]
        print(f"{tag:<26} | {e['cagr']:>9.2%} {e['maxdd']:>8.2%} {e['sharpe']:>7.2f} "
              f"{e['calmar']:>7.2f} {e['max_underwater_days']:>6} "
              f"{str(e['monthly_win_rate']):>6} {str(r.get('avg_exposure')):>6}")
        return r

    base = dict(risk_parity=True, rp_window=60, vol_target=0.16,
                vol_window=60, vol_floor=0.8)
    b = L.sim(book, trades, start_date=sd)["stats"]
    print(f"{'baseline（线上口径）':<26} | {b['effective']['cagr']:>9.2%} "
          f"{b['effective']['maxdd']:>8.2%} {b['effective']['sharpe']:>7.2f} "
          f"{b['effective']['calmar']:>7.2f} {b['effective']['max_underwater_days']:>6} "
          f"{str(b['effective']['monthly_win_rate']):>6} {'1.0':>6}")
    rp = L.sim(book, trades, start_date=sd, risk_parity=True, rp_window=60)["stats"]
    print(f"{'仅风险平价':<26} | {rp['effective']['cagr']:>9.2%} "
          f"{rp['effective']['maxdd']:>8.2%} {rp['effective']['sharpe']:>7.2f} "
          f"{rp['effective']['calmar']:>7.2f} {rp['effective']['max_underwater_days']:>6} "
          f"{str(rp['effective']['monthly_win_rate']):>6} {'1.0':>6}")

    print("\n— 基准候选 —")
    run("RP60 + volT16 floor.80", base)

    print("\n— rp_window 敏感性（volT16 floor.80）—")
    for w in (30, 40, 60, 90, 120):
        run(f"rp_window={w}", {**base, "rp_window": w})

    print("\n— vol_target 敏感性（rp60 floor.80）—")
    for v in (0.14, 0.15, 0.16, 0.17, 0.18, 0.20):
        run(f"volT={v:.0%}", {**base, "vol_target": v})

    print("\n— vol_floor 敏感性（rp60 volT16）—")
    for f in (0.70, 0.75, 0.80, 0.85, 0.90, 1.00):
        run(f"floor={f:.2f}", {**base, "vol_floor": f})

    print("\n— vol_window 敏感性（rp60 volT16 floor.80）—")
    for w in (30, 40, 60, 90, 120):
        run(f"vol_window={w}", {**base, "vol_window": w})


if __name__ == "__main__":
    main()
