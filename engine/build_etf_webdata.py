# -*- coding: utf-8 -*-
"""展示层：标的走势核验弹窗数据 → web/public/view/etf/<sid>.json。

定位（重要）
------------
这是**展示层**，不是数据层。它不参与 L0→L1→L2→L3 的重建链，也**不持有任何自己的
算法**——所有价格都由数据层给出，本脚本只做「取字段 + 切片成小文件」：

    adj    份额折算还原后的连续收盘价（结算取价口径）  ← pipeline.load_etf_book()，与结算层**同一次调用**
    breaks 折算断点（日期/原始跳变/参照收益/因子）    ← 同一次调用返回的复权报告
    raw    不复权收盘价（可与行情软件逐日核对）        ← cache/etf_panel.json（L1 面板原样）
    dates / 名称 / 行业映射                            ← 同上

为什么必须"只消费、不自算"
--------------------------
2026-09-24 之前本脚本**自己调用 qfq.apply_qfq()** 重算复权，于是仓库里有两份复权实现：
结算层走 load_etf_book()（尊重 config.ETF_QFQ 开关），展示层直接调 qfq（**无视该开关**）。
两处参照或参数一旦不同（例如漏传 sw_panel 退化成全池中位数），因子就不同，同一个持仓
在页面上与弹窗里显示成两个市值——当时只能靠 ark.py 逐位比对的红线兜住。

现在改成**唯一来源**：展示层只消费结算账簿。红线从"需要校验"变成"结构上不可能违反"，
`ark.py verify` 的逐位比对降级成回归断言（保留，作为重构的安全网）。

目录也分开了：`web/public/view/` 是独立的展示层根（镜像到 `site/view/`），
与数据层 `web/public/data/` 不再混住。水位检查里它叫 `VIEW_etf_modal`，
是「陈旧守卫」而不是数据层水位。

产物
----
    etf/<sid>.json   每只 ETF 的 dates / raw / adj / breaks
    etf/_breaks.json 全池折算断点汇总（复盘页「份额折算核验样本」清单）
    etf/_index.json  etf_code → sid 索引 + last（走势数据的最后交易日，水位检查用）

只读缓存、不访问网络；重复运行幂等（直接覆盖）。

运行：python3 engine/build_etf_webdata.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402     # 权威账簿来源（load_etf_book）
import settlement as ST  # noqa: E402   # L1 面板原样（只取 raw / 名称 / 映射）

OUT_DIR = os.path.join(C.ROOT, "web", "public", "view", "etf")


def main() -> None:
    # 权威价格与断点：**与结算层同一次调用**（同一函数、同一参数、同一开关），
    # 因此 adj 必然逐位等于 sim.json 的 px.c —— 不再需要"传参照"这类约定。
    book, breaks_all = PL.load_etf_book(with_report=True)
    if book is None:
        raise SystemExit("缺少 cache/etf_panel.json：无法导出展示层走势数据")

    # L1 面板原样：raw（不复权）与名称/映射。**不经复权**，与 book 的 dates 同轴。
    raw_panel = ST.load_etf_panel(f"{C.CACHE_DIR}/etf_panel.json")

    dates = list(book.dates)
    # 弹窗走势只展示复盘窗口（BACKTEST_START 起）：面板日期轴从申万指数基期
    # 1999-12-30 开始，不截断的话图表会显得「从 1999 年开始」且与复盘口径不一致。
    # adj 是 raw×累计因子，截断不改任何数值；断点同样只保留窗口内的。
    beg = C.BACKTEST_START or None
    if beg:
        i0 = next((i for i, d in enumerate(dates) if d >= beg), 0)
        dates = dates[i0:]

    listing = raw_panel.get("listing") or {}
    sw_to_etf = raw_panel.get("sw_to_etf") or {}
    # 一个 ETF 可能同时服务多个行业（多对一）：取**首个**命中的行业，与旧实现
    #   next((sw for sw, s in sw_to_etf.items() if s == sid), None)
    # 逐位一致（字典保持插入顺序，setdefault 只在首次命中时写入）。
    sid_to_sw: dict = {}
    for sw, s in sw_to_etf.items():
        sid_to_sw.setdefault(s, sw)

    os.makedirs(OUT_DIR, exist_ok=True)
    n_files = 0
    for sid, raw_ser in raw_panel["series"].items():
        adj_ser = (book.series.get(sid) or {})
        raw_rows, adj_rows = [], []
        for d in dates:
            rrow = raw_ser.get(d)
            arow = adj_ser.get(d)
            # raw：L0 原始层的不复权价（可与行情软件逐日核对）
            # adj：结算账簿的取价（与 sim.json px.c 同源，见 engine/qfq.py）
            raw_rows.append(round(rrow["close"], 6) if rrow and rrow.get("close") else None)
            adj_rows.append(round(arow["close"], 6) if arow and arow.get("close") else None)
        obj = {
            "sid": sid,
            "code": sid.split(".")[-1],
            "name": (listing.get(sid) or {}).get("name", sid),
            "sw": sid_to_sw.get(sid),
            "dates": dates,
            "raw": raw_rows,
            "adj": adj_rows,
            "breaks": [b for b in (breaks_all.get(sid) or []) if not beg or b["date"] >= beg],
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
    for sid in raw_panel["series"]:
        index[sid.split(".")[-1]] = {
            "sid": sid,
            "name": (listing.get(sid) or {}).get("name", sid),
            "sw": sid_to_sw.get(sid),
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

    print(f"[view/etf] 写出 {n_files} 只 ETF 走势文件 + _breaks.json "
          f"({len(flat)} 处折算断点) + _index.json ({len(index)} 只，行情截至 "
          f"{dates[-1] if dates else '—'}) → {OUT_DIR}")


if __name__ == "__main__":
    main()
