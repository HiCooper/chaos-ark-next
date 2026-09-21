# -*- coding: utf-8 -*-
"""降回撤方向 2：集中度约束（同行业重复建仓）与不向下摊平。"""
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

    print(f"\n{'方案':<34} {'cagr':>8} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} "
          f"{'vol':>7} {'月胜率':>7} {'水下天':>7} {'成交':>5} {'均仓':>5} {'阻塞':>5}")
    def row(tag, s):
        print(f"{tag:<34} {s['cagr']:>8.2%} {s['maxdd']:>8.2%} {s['sharpe']:>7.2f} "
              f"{s['calmar']:>7.2f} {s['vol']:>7.2%} {str(s['monthly_win_rate']):>7} "
              f"{s['max_underwater_days']:>7} {s['executed_trades']:>5} "
              f"{str(s['avg_holdings']):>5} {s['blocked_by_rule']:>5}")

    row("baseline（线上口径）", L.sim(book, trades, start_date=sd)["stats"])

    cases = [
        ("每行业≤1仓", dict(per_sector_max=1)),
        ("每行业≤2仓", dict(per_sector_max=2)),
        ("每行业≤3仓", dict(per_sector_max=3)),
        ("每行业≤4仓", dict(per_sector_max=4)),
        ("每行业≤1 + 不上杠杆", dict(per_sector_max=1, max_holdings=12)),
        ("每行业≤2 + maxHold16", dict(per_sector_max=2, max_holdings=16)),
        ("每行业≤2 + 不摊平", dict(per_sector_max=2, no_averaging_down=True)),
        ("每行业≤1 + 不摊平", dict(per_sector_max=1, no_averaging_down=True)),
        ("不摊平", dict(no_averaging_down=True)),
        ("每行业≤1 + RP", dict(per_sector_max=1, risk_parity=True, rp_window=60)),
        ("每行业≤2 + RP", dict(per_sector_max=2, risk_parity=True, rp_window=60)),
        ("RP", dict(risk_parity=True, rp_window=60)),
        ("每行业≤1 + cooldown20", dict(per_sector_max=1, cooldown_days=20)),
        ("每行业≤1 + cooldown40", dict(per_sector_max=1, cooldown_days=40)),
        ("每行业≤1 + volT20", dict(per_sector_max=1, vol_target=0.20,
                                   vol_window=60, vol_floor=0.6)),
        ("每行业≤1 + RP + volT20", dict(per_sector_max=1, risk_parity=True,
                                        rp_window=60, vol_target=0.20,
                                        vol_window=60, vol_floor=0.6)),
    ]
    for tag, kw in cases:
        row(tag, L.sim(book, trades, start_date=sd, **kw)["stats"])


if __name__ == "__main__":
    main()
