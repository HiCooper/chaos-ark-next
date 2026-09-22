# -*- coding: utf-8 -*-
"""从实验台的 day/<date>.json 重建交易清单并结算（免跑全量 review 阶段）。

动机：`pipeline.py --all` 的 review 阶段会跑 10+ 次组合模拟，是整条链最慢的一环；
而实验只需要「结论 → 交易清单 → 组合曲线」这一段。本脚本按与 review.build_review
相同的口径重建：

  · 交易清单：每个出结论日的每个 pick 一笔，date=结论日，horizon=建议周期，
    etf_sid 按买入日（T+1）解析（与 review.py 的 open-edge 段一致）
  · 卖出日：settle_trade 固定 T+1+H（生产结算不开 early_exit），
    卖出日无行情则顺延到最近有成交日
  · 组合参数：完全复制 review.py 的调用（max_holdings=max_daily_picks*4、
    risk_parity/vol_target/vol_floor/cash_yield/score_tilt/max_new_per_day）

自校验：对生产 day 目录跑一遍，应与线上 review.json 的
cagr 9.59% / maxdd -35.75% 吻合（容差 1e-4）。

用法：
  python3 engine/lab_gate_settle.py                 # 汇总全部已有实验目录
  python3 engine/lab_gate_settle.py --dir <day目录>  # 只算一个
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402

ROOT = C.ROOT


def trades_from_daydir(daydir: str, book: ST.EtfBook, qfq_on: bool) -> list:
    dates = book.dates
    out = []
    for p in sorted(glob.glob(f"{daydir}/*.json")):
        d0 = json.load(open(p))
        d = d0.get("date")
        if not d or d0.get("error"):
            continue
        i0 = book.idx.get(d)
        if i0 is None or i0 + 1 >= len(dates):
            continue
        b_i = i0 + 1
        for pk in (d0.get("picks") or []):
            hz = pk.get("horizon") or {}
            H = int(hz.get("horizon") or (hz if isinstance(hz, int) else 0) or 0)
            if H <= 0:
                continue
            sid = book.etf_of(pk["name"], dates[b_i])
            if not sid:
                continue
            t = {"date": d, "sw_name": pk["name"], "horizon": H,
                 "composite": pk.get("composite", 0), "etf_sid": sid,
                 "action": pk.get("action")}
            s_i = i0 + 1 + H
            if s_i < len(dates):
                # 卖出日无行情则顺延（与 settle_trade 一致）
                k = s_i
                while k > b_i and not book.close(sid, k):
                    k -= 1
                if k > b_i:
                    t["sell_date"] = dates[k]
            out.append(t)
    return out


def settle(trades: list, book: ST.EtfBook) -> dict:
    RC = C.RISK_CONTROL
    PF = C.PORTFOLIO
    dates = book.dates
    start = dates[0]
    return ST.simulate_etf_portfolio(
        book, trades, start_date=start,
        max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
        risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
        vol_target=RC["vol_target"], vol_window=RC["vol_window"],
        vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
        score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])


def yearly(nav, dates, bench) -> dict:
    yr = {}
    for i, d in enumerate(dates):
        v = yr.setdefault(d[:4], {"last": i})
        v["last"] = i
    return {y: (nav[v["last"]] / (nav[v["last"] - 1] if v["last"] else nav[0]) - 1,
                bench[v["last"]] / (bench[v["last"] - 1] if v["last"] else bench[0]) - 1)
            for y, v in yr.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--threshold", type=float, default=1e-4)
    args = ap.parse_args()

    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    prod_day = f"{ROOT}/web/public/data/day"
    results = {}

    targets = [("生产(线上)", prod_day, False)]
    if args.dir:
        targets = [("指定", args.dir, True)]
    else:
        for tag in ["P0", "B0", "G1", "G1b", "G2", "G3", "G4", "G5", "G6", "G6b"]:
            dd = f"{C.CACHE_DIR}/lab_gate_{tag}/day"
            if os.path.exists(dd):
                targets.append((tag, dd, True))

    # 一次只持有一份账簿（折算复权会把面板再复制一份，两份同时在场会 OOM）
    _book_cache = {}

    def get_book(use_qfq: bool) -> ST.EtfBook:
        if use_qfq not in _book_cache:
            src, _r = qfq.apply_qfq(ep, None, verbose=False) if use_qfq else (ep, None)
            _book_cache[use_qfq] = ST.EtfBook(src)
        return _book_cache[use_qfq]

    print(f"{'标签':<10}{'交易数':>7}{'cagr':>9}{'maxdd':>9}{'sharpe':>8}{'calmar':>8}"
          f"{'成交':>6}{'暴露':>7}")
    for tag, dd, use_qfq in targets:
        book = get_book(use_qfq)
        tr = trades_from_daydir(dd, book, use_qfq)
        if not tr:
            continue
        r = settle(tr, book)
        s = r["stats"]
        results[tag] = {
            "n_trades": len(tr), "cagr": s["cagr"], "maxdd": s["maxdd"],
            "sharpe": s.get("sharpe"), "calmar": s.get("calmar"),
            "vol": s.get("vol"), "executed": s.get("executed_trades"),
            "avg_exposure": s.get("avg_exposure"),
            "yearly": yearly(r["nav"], r["dates"], r["bench"]),
            "avg_holdings": s.get("avg_holdings"),
        }
        print(f"{tag:<10}{len(tr):>7}{s['cagr']:>9.4f}{s['maxdd']:>9.4f}"
              f"{(s.get('sharpe') or 0):>8.3f}{(s.get('calmar') or 0):>8.3f}"
              f"{s.get('executed_trades'):>6}{(s.get('avg_exposure') or 0):>7.3f}")
        if len(_book_cache) > 1:
            # 释放不再需要的账簿（不同复权口径交替出现时）
            keep = use_qfq
            for k in list(_book_cache):
                if k != keep:
                    del _book_cache[k]
            import gc
            gc.collect()

    if "生产(线上)" in results:
        prod = json.load(open(f"{ROOT}/web/public/data/review.json"))["settlement"]["stats"]
        got = results["生产(线上)"]
        dc = abs(got["cagr"] - prod["cagr"])
        dd = abs(got["maxdd"] - prod["maxdd"])
        print(f"\n自校验（重建 vs 线上 review.json）：Δcagr={dc:.2e} Δmaxdd={dd:.2e} "
              f"{'✓' if max(dc, dd) < 1e-4 else '✗ 需核对重建口径'}")

    if not args.dir:
        print("\n=== 逐年组合收益（生产重建 -> 各变体）===")
        base = results.get("生产(线上)") or results.get("B0")
        for tag, v in results.items():
            if tag == "生产(线上)":
                continue
            print(f"\n{tag}")
            for y in sorted(base["yearly"]):
                a = base["yearly"][y][0]
                c = v["yearly"].get(y)
                if c is None:
                    continue
                print(f"  {y}: {a*100:>6.1f}% -> {c[0]*100:>6.1f}%"
                      f"{'  *' if abs(c[0]-a) > 0.005 else ''}   (基准 {c[1]*100:>5.1f}%)")
        json.dump({k: v for k, v in results.items()},
                  open(f"{C.CACHE_DIR}/lab_gate_settle.json", "w"),
                  ensure_ascii=False, indent=1)
        print(f"\n已写 {C.CACHE_DIR}/lab_gate_settle.json")


if __name__ == "__main__":
    main()
