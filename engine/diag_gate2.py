# -*- coding: utf-8 -*-
"""结论闸门分解：定位「哪个条件卡住了某年」——只读诊断。

看的是 classify.classify_day 里 AMBUSH / BUY_NOW 的与条件（全部是绝对阈值）：
  AMBUSH   : composite>=56 且 mainline>=52 且 pullback>=52 且 exhaust>=50
  BUY_NOW  : composite>=64 且 mainline>=58 且 rebound>=55 且 risk>=35
本脚本逐日逐候选统计每个条件单独通过的数量、两两组合通过量、以及最终与条件
通过量，按年/环境聚合，从而区分「单个维度被市场整体压制」与「维度间不共振」。

运行：python3 engine/diag_gate2.py
"""
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import scoring as SC  # noqa: E402
from feature_store import FeatureStore  # noqa: E402

D = SC.DIMS


def main() -> None:
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    regime = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    dims, comp = SC.build_score_matrix(fs, regime, verbose=False)
    stages = SC.compute_stage(fs)

    rows = []
    for i, date in enumerate(fs.dates):
        cands = fs.investable(date)
        if not cands:
            continue
        ci = [fs.codes.index(c) for c in cands]
        dl = {k: dims[k][ci, i] for k in D}
        c_ = comp[ci, i]
        # 逐条件布尔
        c1 = c_ >= C.CLASSIFY["ambush_min"]
        c2 = dl["mainline"] >= 52
        c3 = dl["pullback"] >= 52
        c4 = dl["exhaust"] >= 50
        conj = c1 & c2 & c3 & c4
        # 若只放松「综合分」一个条件（其他三个仍须成立）
        rows.append({
            "date": date, "year": date[:4], "regime": regime.get(date, {}).get("regime", "?"),
            "n": len(cands),
            "ml_max": float(np.nanmax(dl["mainline"])),
            "ml_p90": float(np.nanpercentile(dl["mainline"], 90)),
            "pb_max": float(np.nanmax(dl["pullback"])),
            "ex_max": float(np.nanmax(dl["exhaust"])),
            "cm_max": float(np.nanmax(c_)),
            "p_c1": int(c1.sum()), "p_c2": int(c2.sum()), "p_c3": int(c3.sum()),
            "p_c4": int(c4.sum()),
            "p_c234": int((c2 & c3 & c4).sum()),
            "p_c134": int((c1 & c3 & c4).sum()),
            "p_c124": int((c1 & c2 & c4).sum()),
            "p_c123": int((c1 & c2 & c3).sum()),
            "p_all": int(conj.sum()),
        })

    def agg(key):
        g = defaultdict(list)
        for r in rows:
            g[r[key]].append(r)
        out = {}
        for k, v in g.items():
            out[k] = {
                "days": len(v),
                "ml_max": float(np.mean([x["ml_max"] for x in v])),
                "p90ml": float(np.mean([x["ml_p90"] for x in v])),
                "cm_max": float(np.mean([x["cm_max"] for x in v])),
                "avg_p_all": float(np.mean([x["p_all"] for x in v])),
                "hit_days": float(np.mean([1.0 if x["p_all"] > 0 else 0.0 for x in v])),
                **{f"avg_{k2}": float(np.mean([x[k2] for x in v]))
                   for k2 in ("p_c1", "p_c2", "p_c3", "p_c4", "p_c234", "p_c134", "p_c124", "p_c123")},
            }
        return out

    print("=== 按年：AMBUSH 闸门各条件「日均通过数」（每条件 ×120 个候选）===")
    print(f"{'年':<6}{'日':>5}{'综合≥56':>9}{'主线≥52':>9}{'回调≥52':>9}{'量能≥50':>9}"
          f"{'缺综合':>8}{'缺主线':>8}{'缺回调':>8}{'缺量能':>8}{'全通过':>8}{'命中日':>8}"
          f"{'日最高主线':>11}")
    ya = agg("year")
    for y in sorted(ya):
        s = ya[y]
        print(f"{y:<6}{s['days']:>5.0f}{s['avg_p_c1']:>9.2f}{s['avg_p_c2']:>9.2f}"
              f"{s['avg_p_c3']:>9.2f}{s['avg_p_c4']:>9.2f}"
              f"{s['avg_p_c234']:>8.2f}{s['avg_p_c134']:>8.2f}{s['avg_p_c124']:>8.2f}"
              f"{s['avg_p_c123']:>8.2f}{s['avg_p_all']:>8.2f}{s['hit_days']*100:>7.0f}%"
              f"{s['ml_max']:>11.1f}")

    print("\n（缺X 列 = 保持其余三个条件、只放开该条件时的日均通过数；"
          "数值≈0 说明该条件是硬约束）")

    print("\n=== 按环境 ===")
    print(f"{'环境':<9}{'日':>5}{'综合≥56':>9}{'主线≥52':>9}{'回调≥52':>9}{'量能≥50':>9}"
          f"{'全通过':>8}{'命中日':>8}{'日最高主线':>11}")
    ra = agg("regime")
    for r in sorted(ra, key=lambda x: -ra[x]["days"]):
        s = ra[r]
        print(f"{r:<9}{s['days']:>5.0f}{s['avg_p_c1']:>9.2f}{s['avg_p_c2']:>9.2f}"
              f"{s['avg_p_c3']:>9.2f}{s['avg_p_c4']:>9.2f}{s['avg_p_all']:>8.2f}"
              f"{s['hit_days']*100:>7.0f}%{s['ml_max']:>11.1f}")

    json.dump({"by_year": ya, "by_regime": ra}, open(f"{C.CACHE_DIR}/diag_gate2.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/diag_gate2.json")


if __name__ == "__main__":
    main()
