# -*- coding: utf-8 -*-
"""实验 A（续）：同池版抄底通道的稳健性研究（离线，不写产物、不改生产）。

lab_dip.py 已经确认：
  · 独立 20% 专用额度 → 年化 −1.91pp，否决
  · 同池竞争（抄底信号并入候选池，共享 K=24 槽位与现金）→ 全窗口 +1.00pp，
    独立起账均值 +0.75pp、为正 58.5%→62.2%

本脚本回答"这个改善是不是真的、能不能落参"的四件事：

  ① 分段（样本内/样本外）：2019-06~2022-12 与 2023-01~2026-10 各自独立起账对比
  ② 参数在分段上的迁移：训练段挑最优（跌幅 × 回升），拿到检验段看是否还成立
  ③ 持有期扫描（10/20/40 日）：H=20 是统计口径定的，还是也能被数据支持
  ④ 同池里的"抢槽位"诊断：DIP 建仓占多少、和主策略有多大重叠、换手变化

用法：python lab_dip_wf.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import lab_dip as LD  # noqa: E402
import pipeline as PL  # noqa: E402
import settlement as ST  # noqa: E402

SEGMENTS = [
    ("样本内 2019-06 ~ 2022-12", "2019-06-14", "2022-12-30"),
    ("样本外 2023-01 ~ 2026-10", "2023-01-03", None),
]


def load_prod() -> List[dict]:
    sim = json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))
    out = []
    for d, day in sim["days"].items():
        for sw, sid, w, sc, sell, H, act in day["p"]:
            out.append({"date": d, "sw_name": sw, "horizon": H, "composite": sc,
                        "sell_date": sell, "etf_sid": sid, "action": act})
    return out


def base_kw(book=None) -> dict:
    sim = json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    return dict(max_holdings=sim["max_holdings"], target_positions=sim["K"],
                risk_parity=True, rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cash_yield=RC["cash_yield"], score_tilt=PF["score_tilt"],
                max_new_per_day=sim["max_new_per_day"])


def run(book, trades, start, end=None, **kw) -> dict:
    r = ST.simulate_cash_account(book, trades, start_date=start, end_date=end, **kw)
    return r


def line(label: str, s: dict) -> str:
    return (f"{label:<26}{s['cagr']:>9.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}"
            f"{s['calmar']:>8.3f}{s['avg_deployed']:>9.1%}{s['executed_trades']:>7d}"
            f"{s.get('avg_holdings', 0):>7.1f}")


HDR = (f"{'口径':<26}{'年化':>9}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}"
       f"{'均投出':>9}{'成交':>7}{'均持仓':>7}")


def main() -> None:
    book = PL.load_etf_book()
    panel = json.load(open(f"{C.CACHE_DIR}/panel.json", encoding="utf-8"))
    prod = load_prod()
    kw = base_kw()
    sigs = LD.dip_signals(panel, book)
    dip = LD._dip_trades(sigs, book)
    cut = min(t["date"] for t in prod)
    dip = [t for t in dip if t["date"] >= cut]
    print(f"生产候选 {len(prod)} 条 · 抄底信号 {len(dip)} 条（回测窗口内）\n")

    # ① 分段
    print("[① 分段独立起账]")
    print(HDR)
    for name, s0, s1 in SEGMENTS:
        b = run(book, prod, s0, s1, **kw)["stats"]
        d = run(book, prod + dip, s0, s1, **kw)["stats"]
        print(line(f"{name} · 基线", b))
        print(line(f"{name} · 同池+抄底", d))
        print(f"{'':<26}增量 年化 {d['cagr']-b['cagr']:+.2%} · 回撤 {d['maxdd']-b['maxdd']:+.2%}"
              f" · Calmar {d['calmar']-b['calmar']:+.3f} · 均投出 {d['avg_deployed']-b['avg_deployed']:+.1%}")
    print()

    # ② 参数迁移：训练段挑最优，检验段验证
    print("[② 参数迁移] 训练段挑最优 → 检验段看是否还成立")
    tr_s, tr_e = SEGMENTS[0][1], SEGMENTS[0][2]
    grid = [(dd, reb) for dd in (-0.10, -0.12, -0.15, -0.20) for reb in (0.01, 0.03, 0.05)]
    rows = []
    for dd, reb in grid:
        sg = LD.dip_signals(panel, book, dd_trig=dd, reb_trig=reb)
        dp = [t for t in LD._dip_trades(sg, book) if t["date"] >= cut]
        s = run(book, prod + dp, tr_s, tr_e, **kw)["stats"]
        rows.append((dd, reb, len(dp), s))
    base_tr = run(book, prod, tr_s, tr_e, **kw)["stats"]
    best = max(rows, key=lambda r: r[3]["cagr"])
    print(f"  训练段基线：年化 {base_tr['cagr']:.2%} / 回撤 {base_tr['maxdd']:.2%} / Calmar {base_tr['calmar']:.3f}")
    print(f"  训练段最优：跌幅 {best[0]:+.0%} × 回升 {best[1]:.0%} → 年化 {best[3]['cagr']:.2%}"
          f"（信号 {best[2]} 条）")
    te_s, te_e = SEGMENTS[1][1], SEGMENTS[1][2]
    base_te = run(book, prod, te_s, te_e, **kw)["stats"]
    for dd, reb in [(best[0], best[1]), (LD.DD_TRIGGER, LD.REB_TRIGGER)]:
        sg = LD.dip_signals(panel, book, dd_trig=dd, reb_trig=reb)
        dp = [t for t in LD._dip_trades(sg, book) if t["date"] >= cut]
        s = run(book, prod + dp, te_s, te_e, **kw)["stats"]
        tag = "训练最优" if (dd, reb) == (best[0], best[1]) else "预设(-15%/3%)"
        print(f"  检验段 {tag} 跌幅 {dd:+.0%} × 回升 {reb:.0%} → 年化 {s['cagr']:.2%}"
              f"（基线 {base_te['cagr']:.2%}，增量 {s['cagr']-base_te['cagr']:+.2%}）"
              f" · 回撤 {s['maxdd']:.2%}（基线 {base_te['maxdd']:.2%}）")
    print()

    # ③ 持有期扫描
    print("[③ 持有期扫描] 同池口径，只改抄底信号的 horizon")
    print(HDR)
    for H in (10, 20, 40):
        sg = LD.dip_signals(panel, book)
        dp = []
        for bi, lst in sg.items():
            d = book.dates[bi - 1]
            if d < cut:
                continue
            for x in lst:
                dp.append({"date": d, "sw_name": x["sw"], "horizon": H, "composite": 52.0,
                           "sell_date": None, "etf_sid": x["sid"], "action": "DIP"})
        s = run(book, prod + dp, C.BACKTEST_START, None, **kw)["stats"]
        print(line(f"持有 {H} 个交易日", s))
    s0 = run(book, prod, C.BACKTEST_START, None, **kw)["stats"]
    print(line("基线（无抄底）", s0))
    print()

    # ④ 抢槽位诊断
    print("[④ 同池诊断]")
    r0 = run(book, prod, C.BACKTEST_START, None, **kw)
    r1 = run(book, prod + dip, C.BACKTEST_START, None, **kw)
    buys0 = [e for e in r0["events"] if e["k"] == "B"]
    buys1 = [e for e in r1["events"] if e["k"] == "B"]
    dbuy = [e for e in buys1 if e.get("a") == "DIP"]
    print(f"  建仓笔数：基线 {len(buys0)} → 同池 {len(buys1)}（DIP {len(dbuy)} 笔，占 {len(dbuy)/max(1,len(buys1)):.1%}）")
    # DIP 交易的实际结果
    sells1 = [e for e in r1["events"] if e["k"] == "S"]
    dip_sell = [e for e in sells1 if e.get("a") == "DIP"]
    if dip_sell:
        rs = np.array([e["ret"] for e in dip_sell if e.get("ret") is not None])
        print(f"  DIP 已结算 {len(rs)} 笔：胜率 {(rs>0).mean():.1%} · 单笔均值 {rs.mean():+.2%}"
              f" · 最差 {rs.min():+.2%} · 合计 {rs.sum():+.2f}(每笔等权)")
    prod_sell = [e for e in sells1 if e.get("a") != "DIP"]
    if prod_sell:
        rs2 = np.array([e["ret"] for e in prod_sell if e.get("ret") is not None])
        print(f"  主策略已结算 {len(rs2)} 笔：胜率 {(rs2>0).mean():.1%} · 单笔均值 {rs2.mean():+.2%}")
    # 与主策略同日/同行业重叠
    prod_days = {(e["sid"], e["bd"]) for e in sells1 if e.get("a") != "DIP"}
    ov = sum(1 for e in dbuy if any((e["sid"], d) == e["bd"] for d in []))  # 占位，下面用真正口径
    same_sid = sum(1 for e in dbuy if e["sid"] in {x["sid"] for x in buys0})
    print(f"  DIP 建仓落在主策略也用过的标的上：{same_sid}/{len(dbuy)}")

    # ⑤ 条件化版本：只在「主策略当日无候选」的空窗日补位（并检查 regime 是否冗余）
    print("\n[⑤ 条件化] 只在主策略空窗日补位，逐段验证")
    per_day = {d: len(day["p"]) for d, day in
               json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))["days"].items()}
    reg = {x["date"]: x.get("regime") for x in
           json.load(open(f"{C.OUT_DIR}/history.json", encoding="utf-8"))["days"]}
    d0 = [t for t in dip if per_day.get(t["date"], 0) == 0]
    d_pd = [t for t in d0 if reg.get(t["date"]) in ("PANIC", "DEFEND")]
    print(f"  空窗日信号 {len(d0)} 条；其中 PANIC/DEFEND {len(d_pd)} 条"
          f"（{'完全重合，regime 闸门冗余' if len(d0) == len(d_pd) else '不完全重合'}）")
    print(HDR)
    for name, s0, s1 in SEGMENTS + [("全窗口", C.BACKTEST_START, None)]:
        b = run(book, prod, s0, s1, **kw)["stats"]
        print(line(f"{name} · 基线", b))
        for tag, sub in [("无条件 DIP", dip), ("仅空窗日", d0)]:
            s = run(book, prod + sub, s0, s1, **kw)["stats"]
            print(line(f"{name} · {tag}", s)
                  + f"   Δ年化 {s['cagr']-b['cagr']:+.2%} Δ回撤 {s['maxdd']-b['maxdd']:+.2%}")
    # 权重（候选分）扫描：分越低，仓位越轻、也越不抢槽位
    print("\n  空窗日 + 候选分扫描（全窗口）：")
    for sc in (30.0, 40.0, 45.0, 52.0):
        dp = [{**t, "composite": sc} for t in d0]
        s = run(book, prod + dp, C.BACKTEST_START, None, **kw)["stats"]
        print(f"    分={sc:>4.0f}  年化 {s['cagr']:.2%}  回撤 {s['maxdd']:.2%}"
              f"  Calmar {s['calmar']:.3f}  均投出 {s['avg_deployed']:.1%}  成交 {s['executed_trades']}")


if __name__ == "__main__":
    main()
