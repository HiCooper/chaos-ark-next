# -*- coding: utf-8 -*-
"""信号层 × 组合层叠加实验：把暴露上限 cap 网格套到各信号变体的交易清单上。

lab_signal_break 的 9 次全量重放产出了各变体的 review.json（含各自 B/S 事件）。
组合层（暴露上限）不改变成交集合，只改权重，因此可以用 settlement 模拟直接
叠加——无需再重放。本台回答「哪个信号变体 × 哪档 cap」是全局帕累托最优。

变体：G4_P85 / G4_P90 / G4_F55 / G3_055（信号层胜者）
cap： 生产 F5(floor0.35-温和-max) 与 floor{0.25,0.35}-更陡-max

运行：python3 engine/lab_combo.py  （离线，秒级）
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downyear5 import ENV_MILD, ENV_VSTEEP, mom_cap, env_cap  # noqa: E402
from lab_stability import metrics_of, year_table  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO


def load_trades_from(path: str):
    """lab_downside.load_trades_from_events 的参数化版本。"""
    r = json.load(open(path))
    stl = r["settlement"]
    dates = stl["curve"]["dates"]
    events = stl["events"]
    sells = defaultdict(list)
    for e in events:
        if e["k"] == "S" and e.get("bd"):
            sells[(e["bd"], e["sw"], e["sid"])].append(e["i"])
    trades = []
    for e in events:
        if e["k"] != "B":
            continue
        bd = dates[e["i"] - 1]
        key = (dates[e["i"]], e["sw"], e["sid"])
        t = {"date": bd, "sw_name": e["sw"], "horizon": e["h"],
             "composite": e.get("sc", 0), "etf_sid": e["sid"], "action": e.get("a")}
        ss = sells.get(key)
        if ss:
            t["sell_date"] = dates[ss.pop(0)]
        trades.append(t)
    return trades, dates


def main() -> None:
    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)
    n = len(book.dates)

    cap_mild = np.maximum(mom_cap(book, 250, 0.35), env_cap(book, ENV_MILD))
    cap_v25 = np.maximum(mom_cap(book, 250, 0.25), env_cap(book, ENV_VSTEEP))
    cap_v35 = np.maximum(mom_cap(book, 250, 0.35), env_cap(book, ENV_VSTEEP))
    caps = [("F5温和(生产)", cap_mild), ("更陡f0.25", cap_v25), ("更陡f0.35", cap_v35)]

    variants = [
        ("生产P0", f"{C.ROOT}/web/public/data/review.json"),
        ("G4_P85", f"{C.CACHE_DIR}/lab_gate_G4_P85/review.json"),
        ("G4_P90", f"{C.CACHE_DIR}/lab_gate_G4_P90/review.json"),
        ("G4_F55", f"{C.CACHE_DIR}/lab_gate_G4_F55/review.json"),
        ("G3_055", f"{C.CACHE_DIR}/lab_gate_G3_055/review.json"),
    ]

    print(f"{'信号×cap':<22}{'cagr':>8}{'maxdd':>9}{'sharpe':>8}{'calmar':>8}"
          f"{'水下日':>7}{'滚动p5':>8}{'负年':>5}{'最差年':>9}")
    out = {}
    for vtag, path in variants:
        trades, prod_dates = load_trades_from(path)
        kw = dict(start_date=prod_dates[0],
                  max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                  risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
                  vol_target=RC["vol_target"], vol_window=RC["vol_window"],
                  vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
                  score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])
        for ctag, cap in caps:
            r = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=cap)
            m = metrics_of(r["nav"], r["dates"])
            ys = year_table(r["nav"], r["dates"])
            m["neg_years"] = sum(1 for v in ys.values() if v < 0)
            m["worst_year"] = min(ys.values())
            m["yearly"] = ys
            name = f"{vtag}+{ctag}"
            out[name] = m
            print(f"{name:<22}{m['cagr']*100:>7.2f}%{m['maxdd']*100:>8.2f}%"
                  f"{m['sharpe']:>8.3f}{m['calmar']:>8.3f}{m['underwater_days']:>7}"
                  f"{m['roll_p5']*100:>7.2f}%{m['neg_years']:>5}{m['worst_year']*100:>8.2f}%")

    print("\n=== 逐年（叠加后） ===")
    years = sorted({y for m in out.values() for y in m["yearly"]})
    print(f"{'信号×cap':<22}" + "".join(f"{y:>9}" for y in years))
    for name, m in out.items():
        print(f"{name:<22}" + "".join(
            f"{m['yearly'].get(y, float('nan'))*100:>8.2f}%" for y in years))

    json.dump(out, open(f"{C.CACHE_DIR}/lab_combo.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_combo.json")


if __name__ == "__main__":
    main()
