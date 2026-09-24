# -*- coding: utf-8 -*-
"""退出规则实测：固定到期 / 到期延展 / 破位早退 / 混合（离线，不写产物）。

同一批候选（web/public/data/sim.json，与生产同源），只换退出规则：
  基线        到期收盘卖出
  纯破位     连续 3 日收盘跌破 MA20 → 次日开盘平仓（不延展）
  混合       到期仍在 MA20 上方 → 延长 60 个交易日（仅一次）；连续 3 日破位 → 次日开盘平仓

用法：python lab_exit.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import settlement as ST  # noqa: E402

VARIANTS = [
    ("基线（到期卖出）", {}),
    ("纯破位（3 日破 MA20 次日开盘卖）", dict(break_days=3)),
    ("到期延展 60 日", dict(extend_on_trend=True, extend_days=60)),
    ("混合（延展 60 + 3 日破位）", dict(extend_on_trend=True, extend_days=60, break_days=3)),
    ("混合（延展 40 + 3 日破位）", dict(extend_on_trend=True, extend_days=40, break_days=3)),
]


def main() -> None:
    book = PL.load_etf_book()
    sim = json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))
    trades = []
    for d, day in sim["days"].items():
        for sw, sid, w, sc, sell, H, act in day["p"]:
            trades.append({"date": d, "sw_name": sw, "horizon": H, "composite": sc,
                           "sell_date": sell, "etf_sid": sid, "action": act})
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    base = dict(max_holdings=sim["max_holdings"], target_positions=sim["K"],
                risk_parity=True, rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cash_yield=RC["cash_yield"], score_tilt=PF["score_tilt"],
                max_new_per_day=sim["max_new_per_day"])
    print(f"候选 {len(trades)} 条（与生产同源）\n")
    print(f"{'变体':<30}{'年化':>8}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}{'平均投出':>9}{'平均持仓':>9}{'成交':>6}{'卖出':>6}")
    for label, kw in VARIANTS:
        r = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START,
                                     **{**base, **kw})
        s = r["stats"]
        print(f"{label:<30}{s['cagr']:>8.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}"
              f"{s['calmar']:>8.3f}{s['avg_deployed']:>9.1%}{s['avg_holdings']:>9.1f}"
              f"{s['executed_trades']:>6}{s['n_sells']:>6}")
    print("\n（口径：同一批候选、同一结算账簿；现金不计息；K=24；每日最多新建 2 笔）")


if __name__ == "__main__":
    main()
