# -*- coding: utf-8 -*-
"""回撤归因：最大回撤区间里，组合到底持有什么、在做什么、亏在哪里。"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

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
    r = L.sim(book, trades, start_date=sd, collect_holdings=True)
    nav = np.array(r["nav"])
    dates = r["dates"]
    bench = np.array(r["bench"])

    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1.0
    ti = int(dd.argmin())
    # 该谷之前、净值最后一次创新高的位置
    pi = int(np.where(nav[:ti + 1] >= nav[:ti + 1].max() - 1e-12)[0][0])
    print(f"峰值 {dates[pi]} nav={nav[pi]:.4f}  谷底 {dates[ti]} nav={nav[ti]:.4f} "
          f"回撤 {dd[ti]:.2%}  历时 {ti-pi} 个交易日")
    print(f"同期基准 {bench[pi]:.4f} -> {bench[ti]:.4f}  {(bench[ti]/bench[pi]-1):.2%}")

    # 区间内日收益
    seg_r = np.diff(nav[pi:ti + 1]) / nav[pi:ti]
    seg_b = np.diff(bench[pi:ti + 1]) / bench[pi:ti]
    print(f"区间组合日均 {seg_r.mean():.4%}  基准日均 {seg_b.mean():.4%}  "
          f"组合年化波动 {seg_r.std()*np.sqrt(252):.2%}")

    # 全样本 beta / 相关
    ar = np.diff(nav) / nav[:-1]
    ab = np.diff(bench) / bench[:-1]
    beta = float(np.cov(ar, ab)[0, 1] / np.var(ab))
    corr = float(np.corrcoef(ar, ab)[0, 1])
    print(f"全样本 beta={beta:.2f}  corr={corr:.2f}  组合vol={ar.std()*np.sqrt(252):.2%} "
          f"基准vol={ab.std()*np.sqrt(252):.2%}")

    # 区间内成交事件：买了什么
    evs = [e for e in r["events"] if pi <= e["i"] <= ti]
    buys = [e for e in evs if e["k"] == "B"]
    sells = [e for e in evs if e["k"] == "S"]
    print(f"\n区间内 B={len(buys)} 笔  S={len(sells)} 笔")
    bc = Counter(e["sw"] for e in buys)
    print("买入行业 TOP12:", ", ".join(f"{k}×{v}" for k, v in bc.most_common(12)))
    lost = [e for e in sells if (e.get("ret") or 0) < 0]
    print(f"区间内卖出 {len(sells)} 笔，其中亏损 {len(lost)} 笔 "
          f"({len(lost)/max(1,len(sells)):.0%})，"
          f"平均 {np.mean([e['ret'] for e in sells]):.3%}")
    worst = sorted(sells, key=lambda e: e.get("ret") or 0)[:12]
    print("\n区间内最亏的 12 笔：")
    for e in worst:
        print(f"  {e['bd']} -> 卖 {dates[e['i']]}  {e['sw']:<10} "
              f"{r.get('etf_names', {}).get(e['sid'], e['sid'])[:16]:<18} {e['ret']:>8.2%}")

    # 水下区间清单
    print("\n主要水下区间（>150 交易日）：")
    under, cur = [], None
    for i, v in enumerate(dd):
        if v < -1e-9:
            cur = i if cur is None else cur
        elif cur is not None:
            if i - cur > 150:
                under.append((cur, i))
            cur = None
    if cur is not None and len(dd) - cur > 150:
        under.append((cur, len(dd) - 1))
    for a, b in under:
        print(f"  {dates[a]} → {dates[b]}  {b-a} 天  最深 {dd[a:b+1].min():.2%}")

    # 组合持仓集中度
    hc = r.get("hold_count") or []
    dts = dates[1:]
    print(f"\n平均同时持仓 {np.mean(hc):.1f}  中位 {np.median(hc):.0f}  "
          f"最大 {max(hc)}  <5个仓的天数占比 {np.mean([1 if x<5 else 0 for x in hc]):.0%}")
    # 区间内持仓数
    hseg = hc[pi:ti]
    print(f"回撤区间平均同时持仓 {np.mean(hseg):.1f}（最大 {max(hseg)}）")

    # 各年度回撤
    print("\n各年度组合表现：")
    by = defaultdict(list)
    for i, d in enumerate(dates[1:], start=1):
        by[d[:4]].append(ar[i - 1])
    for y in sorted(by):
        a = np.array(by[y])
        cum = np.prod(1 + a) - 1
        pk = np.maximum.accumulate(np.cumprod(1 + a))
        print(f"  {y} 收益 {cum:>7.2%}  年内最大回撤 "
              f"{((np.cumprod(1+a)/pk)-1).min():>7.2%}  波动 {a.std()*np.sqrt(252):>6.2%}")


if __name__ == "__main__":
    main()
