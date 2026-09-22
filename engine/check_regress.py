# -*- coding: utf-8 -*-
"""轻量回归校验：确认 classify/scoring 的旋钮脚手架在「默认值」下不改变结论。

做法：一次性载入上下文，对若干代表性交易日调用**生产同一函数** pipeline.run_day，
把 picks / ranking / regime / n_filtered_conclusions 与线上 day/<date>.json 比对。
比全量重放便宜得多（分钟级），用于每次给信号层加旋钮后跑一遍。

运行：python3 engine/check_regress.py
"""
import json
import os
import sys

sys.path.insert(0, ".")
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402

DATES = ["2024-06-11", "2024-12-26", "2021-09-15", "2023-04-12", "2026-03-13"]


def strip(d: dict) -> dict:
    keep = ("date", "regime", "picks", "ranking", "avoid", "watch_top",
            "n_filtered_conclusions", "universe_size")
    return {k: d.get(k) for k in keep}


def main() -> None:
    ctx = PL.load_context()
    book = PL.load_etf_book()
    ok = 0
    for d in DATES:
        p = f"{C.OUT_DIR}/day/{d}.json"
        if not os.path.exists(p):
            print(f"  {d} 线上无此日，跳过")
            continue
        prod = strip(json.load(open(p)))
        mine = strip(PL.run_day(ctx, d, book=book))
        same = json.dumps(prod, sort_keys=True, ensure_ascii=False) == \
            json.dumps(mine, sort_keys=True, ensure_ascii=False)
        if same:
            ok += 1
            print(f"  {d} 一致 ✓  picks={len(mine['picks'] or [])} "
                  f"regime={mine['regime']}")
        else:
            print(f"  {d} **不一致** ✗")
            for k in ("regime", "n_filtered_conclusions", "universe_size"):
                if prod.get(k) != mine.get(k):
                    print(f"      {k}: 线上={prod.get(k)} 本次={mine.get(k)}")
            pp = {(x["name"], x["action"], x["composite"]) for x in (prod["picks"] or [])}
            mp = {(x["name"], x["action"], x["composite"]) for x in (mine["picks"] or [])}
            if pp != mp:
                print(f"      picks 差异: 仅线上={pp - mp} 仅本次={mp - pp}")
            pr = {(x["name"], x["composite"], x["action"]) for x in (prod["ranking"] or [])}
            mr = {(x["name"], x["composite"], x["action"]) for x in (mine["ranking"] or [])}
            if pr != mr:
                print(f"      ranking 差异 {len(pr - mr)} 条（示例 {list(pr - mr)[:3]}）")
    print(f"\n回归校验：{ok}/{len(DATES)} 个交易日逐字段一致")
    sys.exit(0 if ok == len(DATES) else 1)


if __name__ == "__main__":
    main()
