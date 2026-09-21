# -*- coding: utf-8 -*-
"""降回撤方向 5：候选方案的稳健性检验（子样本 / 分年度 / 牛熊段 / 调仓频率）。"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from settlement import EtfBook, load_etf_panel, perf_stats  # noqa: E402
import lab_dd as L  # noqa: E402

BASE = dict(risk_parity=True, rp_window=60, vol_target=0.16, vol_window=60,
            vol_floor=0.80)


def yearly(r):
    nav = np.array(r["nav"]); dates = r["dates"]
    ar = np.diff(nav) / nav[:-1]
    g = defaultdict(list)
    for i, d in enumerate(dates[1:], start=1):
        g[d[:4]].append(ar[i - 1])
    out = {}
    for y, a in sorted(g.items()):
        a = np.array(a)
        if a.size < 20:
            continue
        cum = np.cumprod(1 + a)
        pk = np.maximum.accumulate(cum)
        out[y] = (float(cum[-1] - 1), float(((cum / pk) - 1).min()),
                  float(a.std() * np.sqrt(252)))
    return out


def sub(r, d0, d1):
    dates = r["dates"]; nav = np.array(r["nav"]); bench = np.array(r["bench"])
    ix = [i for i, d in enumerate(dates) if d0 <= d <= d1]
    if len(ix) < 60:
        return None
    a, b = ix[0], ix[-1]
    return perf_stats(nav[a:b + 1], dates[a:b + 1], bench[a:b + 1])


def main():
    book = EtfBook(load_etf_panel())
    days = L.load_history()
    trades = L.build_trades(book, days)
    sd = days[0]["date"]

    b = L.sim(book, trades, start_date=sd)
    n = L.sim(book, trades, start_date=sd, **BASE)

    print("=" * 74)
    print("A. 降低风险平价权重重算频率（实盘换手现实性检验）")
    hdr = f"{'重算频率':<22}{'运行期cagr':>10}{'maxdd':>9}{'sharpe':>8}{'calmar':>8}{'水下天':>7}"
    print(hdr); print("-" * len(hdr))
    for rb in (1, 5, 10, 20, 60):
        s = L.sim(book, trades, start_date=sd, **{**BASE, "rp_rebalance": rb})["stats"]
        e = s["effective"]
        print(f"{('每 %d 日' % rb):<22}{e['cagr']:>10.2%}{e['maxdd']:>9.2%}"
              f"{e['sharpe']:>8.2f}{e['calmar']:>8.2f}{e['max_underwater_days']:>7}")
    print("\n加单仓权重上限：")
    for cap in (3.0, 4.0, 5.0):
        s = L.sim(book, trades, start_date=sd, **{**BASE, "rp_cap": cap})["stats"]
        e = s["effective"]
        print(f"{('单仓≤%s倍' % cap):<22}{e['cagr']:>10.2%}{e['maxdd']:>9.2%}"
              f"{e['sharpe']:>8.2f}{e['calmar']:>8.2f}{e['max_underwater_days']:>7}")

    print("\n" + "=" * 74)
    print("B. 分年度：baseline vs 候选（运行期内）")
    yb, yn = yearly(b), yearly(n)
    print(f"{'年':<8}{'基准收益':>10}{'候选收益':>10}{'基准年内DD':>12}{'候选年内DD':>12}"
          f"{'基准波动':>10}{'候选波动':>10}")
    for y in sorted(set(yb) | set(yn)):
        if y < "2019":
            continue
        x, z = yb.get(y), yn.get(y)
        if not x or not z:
            continue
        print(f"{y:<8}{x[0]:>10.2%}{z[0]:>10.2%}{x[1]:>12.2%}{z[1]:>12.2%}{x[2]:>10.2%}{z[2]:>10.2%}")

    print("\n" + "=" * 74)
    print("C. 子样本（切分不同区间，检验是否只在某一段有效）")
    hdr = (f"{'区间':<26}{'基准cagr':>10}{'候选cagr':>10}{'基准maxdd':>11}"
           f"{'候选maxdd':>11}{'基准SR':>8}{'候选SR':>8}")
    print(hdr); print("-" * len(hdr))
    segs = [("2019-01-01", "2021-12-31"), ("2019-01-01", "2022-12-31"),
            ("2022-01-01", "2024-12-31"), ("2023-01-01", "2026-12-31"),
            ("2024-01-01", "2026-12-31"), ("2019-01-01", "2026-12-31")]
    for d0, d1 in segs:
        sb, sn = sub(b, d0, d1), sub(n, d0, d1)
        if not sb or not sn:
            continue
        print(f"{d0[:7]+'~'+d1[:7]:<26}{sb['cagr']:>10.2%}{sn['cagr']:>10.2%}"
              f"{sb['maxdd']:>11.2%}{sn['maxdd']:>11.2%}{sb['sharpe']:>8.2f}{sn['sharpe']:>8.2f}")

    print("\n" + "=" * 74)
    print("D. 滚动起点（每年初入场，看是否依赖特定起点）")
    print(f"{'起点':<12}{'基准cagr':>10}{'候选cagr':>10}{'基准maxdd':>11}{'候选maxdd':>11}"
          f"{'基准SR':>8}{'候选SR':>8}")
    for y in ("2019", "2020", "2021", "2022", "2023"):
        sb = sub(b, f"{y}-01-01", "2026-12-31")
        sn = sub(n, f"{y}-01-01", "2026-12-31")
        if not sb or not sn:
            continue
        print(f"{y+'起':<12}{sb['cagr']:>10.2%}{sn['cagr']:>10.2%}"
              f"{sb['maxdd']:>11.2%}{sn['maxdd']:>11.2%}{sb['sharpe']:>8.2f}{sn['sharpe']:>8.2f}")

    print("\n" + "=" * 74)
    print("E. 回撤事件对比（最深 5 次）")
    for tag, r in (("基准", b), ("候选", n)):
        nav = np.array(r["nav"]); dates = r["dates"]
        pk = np.maximum.accumulate(nav); dd = nav / pk - 1.0
        eps = []
        i = 1
        while i < len(dd):
            if dd[i] < -0.05 and dd[i - 1] >= dd[i]:
                j = i
                while j < len(dd) and dd[j] < -0.01:
                    j += 1
                seg = dd[i:j]
                if seg.size:
                    t = i + int(seg.argmin())
                    p_i = int(np.where(nav[:t + 1] >= nav[:t + 1].max() - 1e-12)[0][0])
                    eps.append((seg.min(), dates[p_i], dates[t], t - p_i))
                i = j
            else:
                i += 1
        eps = sorted(eps)[:5]
        print(f"  {tag}: " + " | ".join(
            f"{a[1][:7]}→{a[2][:7]} {a[0]:.1%}({a[3]}d)" for a in eps))


if __name__ == "__main__":
    main()
