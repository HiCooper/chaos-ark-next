# -*- coding: utf-8 -*-
"""生产口径 vs 实验台口径 的一致性校验（parity check）。

为什么必须做这一步：
    实验室里跑出来的「改进 1.2pp」如果无法在生产管线里复现，那它就是个假信号。
    历史上这里踩过一次坑——lab_opt.sim 的分数加权写成了 `** tilt / 50.0`，
    而生产 settlement.simulate_etf_portfolio 写的是 `** tilt / (50.0 ** tilt)`，
    前者在 tilt>0 时把绝对分位直接当乘数（60 分 → 1.2、80 分 → 1.6），
    等于给高分结论加了隐性杠杆，于是实验台虚报年化。

本脚本把两份模拟器喂同样的输入、同样的开关，逐项比对 cagr / maxdd / 胜率 / 笔数。
出现任何一项不一致，实验结论作废——先修实现，再谈策略。

用法：
    python verify_parity.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel, simulate_etf_portfolio  # noqa: E402
import lab_opt as L  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO
MAXH = C.CLASSIFY["max_daily_picks"] * 4

# (标签, score_tilt, max_new_per_day, max_per_sid, 是否加风控)
CASES = [
    ("基线：等权满仓",                    0.0,    None, None, False),
    ("只风控（rp+vol_target）",            0.0,    None, None, True),
    ("风控 + 每日≤2",                     0.0,    2,    None, True),
    ("风控 + 分数加权0.5",                 0.5,    None, None, True),
    ("风控 + 每日≤2 + 分数加权0.5（生产）", PF["score_tilt"], PF["max_new_per_day"], None, True),
    ("生产 + 同ETF≤2",                    PF["score_tilt"], PF["max_new_per_day"], 2, True),
]


def main() -> int:
    book = EtfBook(load_etf_panel())
    fs = L.load_fs()
    days = L.load_history()
    trades_lab = L.build_trades(book, fs, days)
    sd = days[0]["date"]

    # 生产侧 trades：字段名与 review.py 一致（net_ret/ex_bench/sell_date/etf_sid/action/reason）
    trades_prod = [{
        "date": t["date"], "sw_name": t["sw_name"], "horizon": t["horizon"],
        "composite": t["composite"], "net_ret": t.get("net_ret"),
        "ex_bench": t.get("ex_bench"), "maxdd": t.get("maxdd"),
        "sell_date": t.get("sell_date"), "etf_sid": t.get("etf_sid"),
        "action": t.get("action"),
    } for t in trades_lab]

    print(f"结论样本：lab {len(trades_lab)} 笔 / prod {len(trades_prod)} 笔")
    print(f"风控参数：rp={RC['risk_parity']} rp_win={RC['rp_window']} "
          f"vol_target={RC['vol_target']} vol_win={RC['vol_window']} "
          f"floor={RC['vol_floor']} cash={RC['cash_yield']}")
    print(f"容量上限：{MAXH} 笔\n")
    print(f"{'用例':<34}{'cagr(lab)':>11}{'cagr(prod)':>12}{'Δ':>8}"
          f"{'dd(lab)':>10}{'dd(prod)':>10}{'笔(lab)':>9}{'笔(prod)':>10}  判定")
    print("-" * 116)

    bad = 0
    for tag, tilt, mnpd, cap, risk_on in CASES:
        kw_lab = dict(
            max_holdings=MAXH, start_date=sd,
            risk_parity=RC["risk_parity"] if risk_on else False,
            rp_window=RC["rp_window"],
            vol_target=RC["vol_target"] if risk_on else None,
            vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
            cash_yield=RC["cash_yield"],
        )
        if tilt:
            kw_lab.update(weight_by="score", score_tilt=tilt)
        if mnpd is not None:
            kw_lab["max_per_day"] = mnpd
        if cap is not None:
            kw_lab["max_per_sid"] = cap

        rl = L.sim(book, trades_lab, **kw_lab)

        kw_prod = dict(
            start_date=sd, max_holdings=MAXH,
            risk_parity=RC["risk_parity"] if risk_on else False,
            rp_window=RC["rp_window"],
            vol_target=RC["vol_target"] if risk_on else None,
            vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
            cash_yield=RC["cash_yield"],
            score_tilt=tilt, max_new_per_day=mnpd, max_per_sid=cap,
        )
        rp = simulate_etf_portfolio(book, trades_prod, **kw_prod)

        cl, cp = rl["stats"]["cagr"], rp["stats"]["cagr"]
        dl, dp = rl["stats"]["maxdd"], rp["stats"]["maxdd"]
        nl, np_ = rl["stats"].get("executed_trades", 0), rp["stats"].get("executed_trades", 0)
        ok = (abs(cl - cp) < 5e-4) and (abs(dl - dp) < 5e-4)
        bad += 0 if ok else 1
        print(f"{tag:<34}{cl:>10.2%}{cp:>12.2%}{cl-cp:>+8.2%}"
              f"{dl:>10.2%}{dp:>10.2%}{nl:>9}{np_:>10}  {'✓' if ok else '✗ 不一致'}")

    print("-" * 116)
    if bad:
        print(f"❌ {bad}/{len(CASES)} 个用例口径不一致——实验结论不可采信，先修实现。")
        return 1
    print("✅ 全部用例口径一致：实验台结论可直接外推到生产。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
