# -*- coding: utf-8 -*-
"""HORIZON_ERROR（周期估错）归因审计 + 周期选择的机会上限（离线，不写任何产物）。

背景：复盘页「做错了什么」里 HORIZON_ERROR 占错误 56.4%，是第一名。但这条归因的判定规则是
（engine/review.py evaluate_pick）：

    longer  = fwd_stats(..., min(H*2, 120))     # 两倍周期（上限 120 日）
    shorter = fwd_stats(..., max(H//2, 5))      # 一半周期
    若 longer 或 shorter 的超额 > 本周期超额 + 5pp  →  打 HORIZON_ERROR

问题在于这条规则有两个结构性偏差，必须先审计清楚，否则会去优化一个假问题：

  1) **只保留 2 个主因**：一条错误结论最多带 2 个标签，页面上的"占比"是
     「标签数 ÷ 错误条数」，可以超过实际受影响的比例；
  2) **同一条规则套在"对的结论"上**：如果 RIGHT 的结论里也有同样高的比例满足该条件，
     那它衡量的是"未来有波动"，而不是"我们选错了窗口"——这是对照组的 base rate。

本脚本用**生产同源函数**（review.evaluate_pick / fwd_stats）逐条重算，输出：
  · 归因复现（与页面口径对齐）与"受影响错误条数"的真实比例
  · 对照组 base rate（RIGHT 结论满足同条件的比例）
  · 触发方向（更长 / 更短）与边际幅度分布
  · **周期选择的机会上限**：若上帝视角每笔都选到 {60,80,100,120,150} 里最优的那个周期，
    平均超额能提高多少（这是"继续打磨周期估计"的理论天花板）

用法：python lab_horizon.py [--sample N]
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402

GRID = [40, 60, 80, 100, 120, 150]


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(x: Optional[float], nd: int = 2) -> str:
    return "—" if x is None else f"{x*100:+.{nd}f}%"


def main() -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    book = PL.load_etf_book()
    idx = {d: i for i, d in enumerate(fs.dates)}
    files = sorted(glob.glob(f"{C.OUT_DIR}/day/*.json"))
    if "--sample" in sys.argv:
        n = int(sys.argv[sys.argv.index("--sample") + 1])
        files = files[-n:]

    rows: List[dict] = []
    for f in files:
        try:
            day = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if day.get("error") or not day.get("picks"):
            continue
        i = idx.get(day["date"])
        if i is None:
            continue
        for p in day["picks"]:
            try:
                ev = RV.evaluate_pick(fs, day, p, i, ctx["regime"], book=book)
            except Exception:
                continue
            if not ev:
                continue
            H = ev["horizon"]
            i0 = idx.get(ev["date"])
            ex = ev["ex_ew"]
            longer = RV.fwd_stats(fs, ev["code"], i0, min(H * 2, 120))
            shorter = RV.fwd_stats(fs, ev["code"], i0, max(H // 2, 5))
            ex_long = (longer or {}).get("ex_ew")
            ex_short = (shorter or {}).get("ex_ew")
            # 逐周期超额（用于机会上限）
            grid_ex = {}
            for g in GRID:
                s = RV.fwd_stats(fs, ev["code"], i0, g)
                grid_ex[g] = (s or {}).get("ex_ew")
            rows.append({
                "etf_ret": ev.get("etf_ret"), "etf_early": ev.get("etf_early_ret"),
                "etf_ex": ev.get("etf_ex_bench"), "etf_early_ex": ev.get("etf_early_ex_bench"),
                "etf_early_days": ev.get("etf_early_days"), "etf_early_exited": ev.get("etf_early_exited"),
                "settled": ev.get("settled"),
                "date": ev["date"], "name": ev["name"], "H": H,
                "verdict": ev["verdict"], "attrs": ev["attribution"],
                "ex": ex, "ret": ev["ret"], "ex_long": ex_long, "ex_short": ex_short,
                "hasHE": "HORIZON_ERROR" in (ev["attribution"] or []),
                "long_better": (ex_long is not None and ex is not None and ex_long > ex + 0.05),
                "short_better": (ex_short is not None and ex is not None and ex_short > ex + 0.05),
                "long_better10": (ex_long is not None and ex is not None and ex_long > ex + 0.10),
                "grid_ex": grid_ex,
            })

    n = len(rows)
    wrong = [r for r in rows if r["verdict"] == "WRONG"]
    right = [r for r in rows if r["verdict"] == "RIGHT"]
    hit = [r for r in wrong if r["hasHE"]]
    tag_n = sum(len(r["attrs"]) for r in wrong)
    print(f"结论样本 {n} 条｜WRONG {len(wrong)}｜RIGHT {len(right)}｜NEUTRAL {n-len(wrong)-len(right)}")
    print(f"归因标签总数 {tag_n}（每条最多 2 个）")

    print("\n=== 1. 页面口径复现 ===")
    print(f"  HORIZON_ERROR 标签数 {len(hit)} / 错误条数 {len(wrong)} = {len(hit)/max(1,len(wrong)):.1%}"
          f"（页面显示的『占错误』就是这个数）")
    print(f"  实际受影响的错误结论比例：{len(hit)}/{len(wrong)} = {len(hit)/max(1,len(wrong)):.1%}（每个受影响结论只算一次）")

    print("\n=== 2. 对照组 base rate（同一条规则套在『对的结论』上）===")
    rc = [r for r in right if r["long_better"] or r["short_better"]]
    print(f"  RIGHT 结论中满足同一条件（另一周期超额高 5pp 以上）的比例：{len(rc)}/{len(right)} = {len(rc)/max(1,len(right)):.1%}")
    print(f"  WRONG 结论中满足该条件的比例：{sum(1 for r in wrong if r['long_better'] or r['short_better'])}/{len(wrong)}")
    print("  → 两者接近就说明这条规则主要在度量『未来有波动』，而不是『我们选错了窗口』")

    print("\n=== 3. 触发方向与边际（仅 WRONG）===")
    lb = [r for r in wrong if r["long_better"]]
    sb = [r for r in wrong if r["short_better"]]
    print(f"  更长周期更好：{len(lb)} 条（其中同时更短也更好 {len([r for r in lb if r['short_better']])}）"
          f"｜更短周期更好：{len(sb)} 条")
    dl = [r["ex_long"] - r["ex"] for r in lb if r["ex_long"] is not None]
    ds = [r["ex_short"] - r["ex"] for r in sb if r["ex_short"] is not None]
    print(f"  更长侧边际：中位 {_pct(st.median(dl))} · 均值 {_pct(_mean(dl))} · n={len(dl)}")
    print(f"  更短侧边际：中位 {_pct(st.median(ds))} · 均值 {_pct(_mean(ds))} · n={len(ds)}")
    print(f"  提高门槛到 10pp 后 WRONG 受影响比例："
          f"{sum(1 for r in wrong if r['long_better10'])}/{len(wrong)} = "
          f"{sum(1 for r in wrong if r['long_better10'])/max(1,len(wrong)):.1%}")

    print("\n=== 4. 这些『错』到底错在哪：本周期收益 vs 备选周期 ===")
    neg = [r for r in hit if (r["ret"] or 0) < 0]
    print(f"  HORIZON_ERROR 命中里，本周期绝对收益为负的：{len(neg)}/{len(hit)} = {len(neg)/max(1,len(hit)):.1%}")
    print(f"  本周期平均超额 {_pct(_mean([r['ex'] for r in hit]))}"
          f"｜更长周期平均超额 {_pct(_mean([r['ex_long'] for r in hit]))}")

    print("\n=== 5. 周期选择的机会上限（上帝视角选最优周期）===")
    hz_now = [r["ex"] for r in rows if r["ex"] is not None]
    hz_best = [max([v for v in r["grid_ex"].values() if v is not None] or [r["ex"] or 0]) for r in rows]
    hz_worst = [min([v for v in r["grid_ex"].values() if v is not None] or [r["ex"] or 0]) for r in rows]
    print(f"  当前周期平均超额      {_pct(_mean(hz_now))}")
    print(f"  上帝视角最优周期      {_pct(_mean(hz_best))}   ← 继续打磨周期估计的理论天花板")
    print(f"  上帝视角最差周期      {_pct(_mean(hz_worst))}")
    print(f"  随机换一个周期的期望  ≈ 各周期均值 "
          f"{_pct(_mean([v for r in rows for v in r['grid_ex'].values() if v is not None]))}")
    pick_best = [max(r["grid_ex"], key=lambda k: (r["grid_ex"][k] if r["grid_ex"][k] is not None else -9))
                 for r in rows]
    from collections import Counter
    print(f"  最优周期分布：{dict(sorted(Counter(pick_best).items()))}")
    print(f"  当前周期分布：{dict(sorted(Counter(r['H'] for r in rows).items()))}")
    _section6(rows)


def _section6(rows: List[dict]) -> None:
    """固定到期 vs 破位提前退出（口径 B）：同一条结论上的逐笔对照。"""
    ss = [r for r in rows if r["settled"] and r["etf_ret"] is not None and r["etf_early"] is not None]
    print("\n=== 6. 提前退出（收盘跌破 MA20 次日开盘平仓）vs 固定到期 ===")
    print(f"  可对照样本 {len(ss)} 笔")
    if not ss:
        return
    win = lambda xs: sum(1 for x in xs if x > 0) / len(xs)
    fx = [r["etf_ret"] for r in ss]
    er = [r["etf_early"] for r in ss]
    fx_ex = [r["etf_ex"] for r in ss if r["etf_ex"] is not None]
    er_ex = [r["etf_early_ex"] for r in ss if r["etf_early_ex"] is not None]
    d_early = [r["etf_early_days"] for r in ss if r["etf_early_days"]]
    print(f"  平均净收益：固定到期 {_pct(_mean(fx))}（胜率 {win(fx):.1%}）"
          f"｜提前退出 {_pct(_mean(er))}（胜率 {win(er):.1%}）")
    print(f"  平均超额  ：固定到期 {_pct(_mean(fx_ex))}（胜率 {win(fx_ex):.1%}）"
          f"｜提前退出 {_pct(_mean(er_ex))}（胜率 {win(er_ex):.1%}）")
    print(f"  平均持有  ：固定 {_mean([r['H'] for r in ss]):.1f} 个交易日"
          f"｜提前退出 {_mean(d_early):.1f} 个交易日（提前触发比例 "
          f"{sum(1 for r in ss if r['etf_early_exited'])/len(ss):.1%}）")
    print(f"  资金效率  ：每持有 1 日的净收益 固定 {_mean(fx)/_mean([r['H'] for r in ss]):.4%}"
          f"｜提前 {_mean(er)/_mean(d_early):.4%}")
    he = [r for r in ss if r["hasHE"]]
    if he:
        better = [r for r in he if (r["etf_early"] or 0) > (r["etf_ret"] or 0)]
        print(f"  在 HORIZON_ERROR 命中里（{len(he)} 笔）：提前退出更优 {len(better)} 笔 = {len(better)/len(he):.1%}"
              f"｜平均改善 {_pct(_mean([(r['etf_early'] - r['etf_ret']) for r in he]))}")


if __name__ == "__main__":
    main()
