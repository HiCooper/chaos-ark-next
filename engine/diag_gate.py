# -*- coding: utf-8 -*-
"""为什么某年「不出结论」——逐日结论闸门诊断（只读）。

对每个交易日，从 day/<date>.json 取全行业综合分，统计：
  maxcomp   当日最高综合分
  gt56/gt64 超过埋伏/入手阈值（降权前）的行业数
  picks     实际入选数（降权 + 过滤 + 名额限制之后）
  avoid     被回避规则命中的行业数
  filt      n_filtered_conclusions（结论前置过滤剔除数）
按 年 / 环境 两个维度聚合，定位「哑火」发生在哪一步。

运行：python3 engine/diag_gate.py
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402

BN, AM = C.CLASSIFY["buy_now_min"], C.CLASSIFY["ambush_min"]


def main() -> None:
    daydir = f"{C.OUT_DIR}/day"
    import glob
    files = sorted(glob.glob(f"{daydir}/*.json"))
    rows = []
    for p in files:
        try:
            d = json.load(open(p))
        except Exception:
            continue
        dt = d.get("date")
        if not dt:
            continue
        rank = d.get("ranking") or []
        comps = [x.get("composite") for x in rank if x.get("composite") is not None]
        if not comps:
            continue
        a = np.array(comps, dtype=float)
        rows.append({
            "date": dt, "year": dt[:4], "regime": d.get("regime") or "?",
            "maxc": float(a.max()),
            "gt56": int((a >= AM).sum()), "gt64": int((a >= BN).sum()),
            "picks": len(d.get("picks") or []),
            "avoid": len(d.get("avoid") or []),
            "filt": int(d.get("n_filtered_conclusions") or 0),
        })
    print(f"共 {len(rows)} 个交易日（{rows[0]['date']} → {rows[-1]['date']}）")

    def agg(key):
        g = defaultdict(list)
        for r in rows:
            g[r[key]].append(r)
        out = {}
        for k, v in g.items():
            out[k] = {
                "days": len(v),
                "silent": sum(1 for x in v if x["picks"] == 0) / len(v),
                "maxc": float(np.mean([x["maxc"] for x in v])),
                "p95c": float(np.percentile([x["maxc"] for x in v], 95)),
                "gt56": float(np.mean([x["gt56"] for x in v])),
                "gt64": float(np.mean([x["gt64"] for x in v])),
                "picks": float(np.mean([x["picks"] for x in v])),
                "avoid": float(np.mean([x["avoid"] for x in v])),
                "filt": float(np.mean([x["filt"] for x in v])),
            }
        return out

    print("\n=== 按年：结论闸门 ===")
    print(f"{'年':<6}{'交易日':>6}{'哑火率':>8}{'日均最高分':>11}{'分95%':>8}"
          f"{'≥56数':>8}{'≥64数':>7}{'实际入选':>9}{'回避':>6}{'被过滤':>7}")
    ya = agg("year")
    for y in sorted(ya):
        s = ya[y]
        print(f"{y:<6}{s['days']:>6}{s['silent']*100:>7.0f}%{s['maxc']:>11.1f}{s['p95c']:>8.1f}"
              f"{s['gt56']:>8.2f}{s['gt64']:>7.2f}{s['picks']:>9.2f}{s['avoid']:>6.2f}{s['filt']:>7.2f}")

    print("\n=== 按环境：结论闸门（全区间，看环境是否压制打分）===")
    print(f"{'环境':<9}{'交易日':>6}{'哑火率':>8}{'日均最高分':>11}{'≥56数':>8}{'实际入选':>9}{'回避':>6}")
    ra = agg("regime")
    for r in sorted(ra, key=lambda x: -ra[x]["days"]):
        s = ra[r]
        print(f"{r:<9}{s['days']:>6}{s['silent']*100:>7.0f}%{s['maxc']:>11.1f}"
              f"{s['gt56']:>8.2f}{s['picks']:>9.2f}{s['avoid']:>6.2f}")

    print("\n=== 2024 逐月：结论闸门 ===")
    mg = defaultdict(list)
    for r in rows:
        if r["year"] == "2024":
            mg[r["date"][:7]].append(r)
    print(f"{'月':<9}{'交易日':>6}{'哑火率':>8}{'日均最高分':>11}{'≥56数':>8}{'≥64数':>7}{'入选':>6}{'回避':>6}")
    for m in sorted(mg):
        v = mg[m]
        print(f"{m:<9}{len(v):>6}{sum(1 for x in v if x['picks']==0)/len(v)*100:>7.0f}%"
              f"{np.mean([x['maxc'] for x in v]):>11.1f}{np.mean([x['gt56'] for x in v]):>8.2f}"
              f"{np.mean([x['gt64'] for x in v]):>7.2f}{np.mean([x['picks'] for x in v]):>6.1f}"
              f"{np.mean([x['avoid'] for x in v]):>6.2f}")

    json.dump({"by_year": ya, "by_regime": ra}, open(f"{C.CACHE_DIR}/diag_gate.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/diag_gate.json")


if __name__ == "__main__":
    main()
