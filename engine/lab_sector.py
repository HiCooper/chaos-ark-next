# -*- coding: utf-8 -*-
"""「行业选错」（SECTOR_PICK）归因审计（离线，不写任何产物）。

背景：2026-09-24 收紧 HORIZON_ERROR 判据后，复盘页「做错了什么」第一名变成
SECTOR_PICK（行业选错：同期存在明显更好的行业，横向比较跑输），占错误 59.5%。
判定规则（review.evaluate_pick）：

    if 本周期收益 < 同行中位数 − 6%:  打 SECTOR_PICK

本脚本要回答三个问题：
  1) 这是"选品错了"还是"衬托错了"——即被选行业真的差，还是中位数被少数暴涨行业抬高？
  2) 出手时的**横向排名**（当日 ranking 里的名次 / 综合分分位）能不能预测结果？
     若不能，说明是排序信号无效；若能，说明是取用规则（每日名额）浪费了排序。
  3) 跑输是"选错行业"还是"同类里的正常离散"——把同一日的结论按行业分组看离散度。

用法：python lab_sector.py [--sample N]
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(x, nd=2):
    return "—" if x is None else f"{x*100:+.{nd}f}%"


def main() -> None:
    ctx = PL.load_context()
    fs, book = ctx["fs"], PL.load_etf_book()
    idx = {d: i for i, d in enumerate(fs.dates)}
    files = sorted(glob.glob(f"{C.OUT_DIR}/day/*.json"))
    if "--sample" in sys.argv:
        files = files[-int(sys.argv[sys.argv.index("--sample") + 1]):]

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
        rank = {r["code"]: n for n, r in enumerate(sorted(
            day.get("ranking") or [], key=lambda x: -(x.get("composite") or 0)))}
        nrank = len(day.get("ranking") or []) or None
        for p in day["picks"]:
            try:
                ev = RV.evaluate_pick(fs, day, p, i, ctx["regime"], book=book)
            except Exception:
                continue
            if not ev:
                continue
            rows.append({
                "date": ev["date"], "code": ev["code"], "name": ev["name"],
                "verdict": ev["verdict"], "attrs": ev["attribution"],
                "ret": ev["ret"], "ex": ev["ex_ew"], "med": ev.get("cross_median"),
                "composite": ev["composite"], "rank": rank.get(ev["code"]),
                "n_rank": nrank, "action": ev["action"], "regime": ev["regime"],
                "level": ev["level"], "horizon": ev["horizon"],
                "etf_ex": ev.get("etf_ex_bench"),
            })

    wrong = [r for r in rows if r["verdict"] == "WRONG"]
    sp = [r for r in wrong if "SECTOR_PICK" in (r["attrs"] or [])]
    print(f"结论 {len(rows)} 条｜WRONG {len(wrong)}｜其中 SECTOR_PICK {len(sp)}"
          f"（占错误 {len(sp)/max(1,len(wrong)):.1%}）")

    print("\n=== 1. 是『行业真的差』还是『中位数被抬高』===")
    dd = [r["ret"] - r["med"] for r in sp if r["ret"] is not None and r["med"] is not None]
    med_abs = [r["med"] for r in sp if r["med"] is not None]
    own = [r["ret"] for r in sp if r["ret"] is not None]
    print(f"  被选中行业本周期收益：均值 {_pct(_mean(own))}（中位 {_pct(st.median(own))}）")
    print(f"  同日同行中位数收益  ：均值 {_pct(_mean(med_abs))}（中位 {_pct(st.median(med_abs))}）")
    print(f"  落后幅度            ：均值 {_pct(_mean(dd))}（中位 {_pct(st.median(dd))}）")
    print(f"  其中被选中行业本周期**绝对收益为正**的比例："
          f"{sum(1 for x in own if x > 0)/max(1,len(own)):.1%}"
          f"（为正却仍被归因选错 = 只是跑输，不是亏钱）")
    print(f"  同行中位数为正的样本占比：{sum(1 for x in med_abs if x > 0)/max(1,len(med_abs)):.1%}")

    print("\n=== 2. 出手时的横向排名能否预测结果 ===")
    rk = [r for r in rows if r["rank"] is not None]
    if rk:
        q = lambda rs, lo, hi: [r for r in rs if lo <= r["rank"] < hi]
        n = max(r["n_rank"] or 1 for r in rk)
        for lo, hi, lab in [(0, 1, "当日第 1 名"), (1, 3, "第 2–3 名"),
                            (3, 5, "第 4–5 名"), (5, 10 ** 6, "第 6 名及以后")]:
            sub = q(rk, lo, hi)
            if sub:
                print(f"  {lab:<12} n={len(sub):<5} 平均超额 {_pct(_mean([r['ex'] for r in sub]))}"
                      f"  胜率 {sum(1 for r in sub if (r['etf_ex'] or 0) > 0)/max(1,sum(1 for r in sub if r['etf_ex'] is not None)):.1%}")
        # 综合分与结果的相关性
        xs = [(r["composite"], r["ex"]) for r in rk if r["ex"] is not None]
        if len(xs) > 30:
            mx, my = _mean([a for a, _ in xs]), _mean([b for _, b in xs])
            cov = sum((a - mx) * (b - my) for a, b in xs) / len(xs)
            sx = (sum((a - mx) ** 2 for a, _ in xs) / len(xs)) ** 0.5
            sy = (sum((b - my) ** 2 for _, b in xs) / len(xs)) ** 0.5
            print(f"  综合分 vs 本周期超额 的相关系数：{cov/(sx*sy):+.3f}（n={len(xs)}）")

    print("\n=== 3. 跑输的形态：行业 / 环境 / 动作 ===")
    for key, lab in [("name", "行业"), ("regime", "环境"), ("action", "动作")]:
        c = Counter(r[key] for r in sp)
        tot = Counter(r[key] for r in wrong)
        top = c.most_common(6)
        print(f"  按{lab}： " + "  ".join(f"{k} {v}（该{lab}错误率 {v/max(1,tot[k]):.0%}）" for k, v in top))
    # 同日多结论的离散度：是"整批都错"还是"个别错"
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    multi = [v for v in byday.values() if len(v) >= 2]
    if multi:
        intra = [max(x["ex"] for x in v if x["ex"] is not None)
                 - min(x["ex"] for x in v if x["ex"] is not None)
                 for v in multi if len([x for x in v if x["ex"] is not None]) >= 2]
        print(f"  同日多结论（{len(multi)} 天）内部超额极差：中位 {_pct(st.median(intra))}"
              f" → 同一天里选哪个行业本身就差这么多")


if __name__ == "__main__":
    main()
