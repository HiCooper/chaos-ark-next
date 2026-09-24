# -*- coding: utf-8 -*-
"""生成前端「标的走势核验」数据：web/public/data/etf/<sid>.json。

背景：复盘页逐笔成交明细里，点击「可交易标的」要能弹出该 ETF 的走势图，
并标注 B/S 成交点（价格、原因、份额、价值）与份额折算断点——目的是让
用户逐笔核对「拆分前后算钱是否准确」。

每个文件包含：
    sid / code / name   标的识别
    dates               全局交易日轴（与 etf_panel.dates 一致）
    raw                 不复权收盘价（真实市价，可与行情软件逐日核对）
    adj                 份额折算还原后的连续收盘价（结算取价口径，见 engine/qfq.py）
    breaks              该 ETF 的折算断点（日期 / 原始跳变 / 参照收益 / 因子）

另写 _breaks.json：全池折算断点汇总（前端「份额折算核验样本」清单）。
只读缓存、不访问网络；重复运行幂等（直接覆盖）。

运行：python3 engine/build_etf_webdata.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import qfq  # noqa: E402

OUT_DIR = os.path.join(C.ROOT, "web", "public", "data", "etf")


def main() -> None:
    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    panel, rep = qfq.apply_qfq(ep, None, verbose=False)
    dates = panel["dates"]
    # 弹窗走势只展示复盘窗口（BACKTEST_START 起）：面板日期轴从申万指数基期
    # 1999-12-30 开始，不截断的话图表会显得「从 1999 年开始」且与复盘口径不一致。
    # adj 是 raw×累计因子，截断不改任何数值；断点同样只保留窗口内的。
    beg = C.BACKTEST_START or None
    if beg:
        i0 = next((i for i, d in enumerate(dates) if d >= beg), 0)
        dates = dates[i0:]
    listing = panel.get("listing") or {}
    sw_to_etf = panel.get("sw_to_etf") or {}

    # 复权报告若已有落盘文件则以落盘为准（与生产 review 同源）；否则现算
    rep_path = f"{C.CACHE_DIR}/qfq_report.json"
    breaks_all = json.load(open(rep_path)) if os.path.exists(rep_path) else rep

    os.makedirs(OUT_DIR, exist_ok=True)
    n_files = 0
    for sid, raw_ser in ep["series"].items():
        adj_ser = panel["series"][sid]   # 无断点时与 raw_ser 同一 dict；有断点时是修正副本
        raw_rows, adj_rows = [], []
        for d in dates:
            rrow = raw_ser.get(d)
            arow = adj_ser.get(d)
            # raw：L0 原始层的不复权价（可与行情软件逐日核对）；
            # adj：份额折算还原后的连续价（结算取价口径，见 engine/qfq.py）
            raw_rows.append(round(rrow["close"], 6) if rrow and rrow.get("close") else None)
            adj_rows.append(round(arow["close"], 6) if arow and arow.get("close") else None)
        bl = [b for b in (breaks_all.get(sid) or []) if not beg or b["date"] >= beg]
        obj = {
            "sid": sid,
            "code": sid.split(".")[-1],
            "name": (listing.get(sid) or {}).get("name", sid),
            "sw": next((sw for sw, s in sw_to_etf.items() if s == sid), None),
            "dates": dates,
            "raw": raw_rows,
            "adj": adj_rows,
            "breaks": bl,
        }
        with open(os.path.join(OUT_DIR, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        n_files += 1

    # 折算断点汇总（前端核验清单）：按日期倒序，最新发生的排最前
    flat = []
    for sid, lst in breaks_all.items():
        nm = (listing.get(sid) or {}).get("name", sid)
        for b in lst:
            flat.append({"sid": sid, "code": sid.split(".")[-1], "name": nm, **b})
    flat.sort(key=lambda x: x["date"], reverse=True)
    with open(os.path.join(OUT_DIR, "_breaks.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": True, "n": len(flat), "breaks": flat},
                  f, ensure_ascii=False, separators=(",", ":"))

    # 标的索引：etf_code → sid 映射（前端「今日结论」等页只有 6 位代码，
    # 弹窗按 sid 取数，必须有这层解析；覆盖全池，含从未成交的 ETF）
    index = {}
    for sid in ep["series"]:
        index[sid.split(".")[-1]] = {
            "sid": sid,
            "name": (listing.get(sid) or {}).get("name", sid),
            "sw": next((sw for sw, s in sw_to_etf.items() if s == sid), None),
        }
    with open(os.path.join(OUT_DIR, "_index.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": True, "n": len(index),
                   # 日期轴末端 = 弹窗走势的最后一个交易日。水位检查（ark.py status/verify）
                   # 靠它发现「ETF 面板已更新、但走势文件没重建」这类陈旧——曾经因此
                   # 让弹窗标的涨跌停在 09-21，而账户已经盯市到 09-23。
                   "last": dates[-1] if dates else None,
                   "days": len(dates),
                   "etfs": index},
                  f, ensure_ascii=False, separators=(",", ":"))

    print(f"[etf-webdata] 写出 {n_files} 只 ETF 走势文件 + _breaks.json "
          f"({len(flat)} 处折算断点) + _index.json ({len(index)} 只，行情截至 "
          f"{dates[-1] if dates else '—'}) → {OUT_DIR}")


if __name__ == "__main__":
    main()
