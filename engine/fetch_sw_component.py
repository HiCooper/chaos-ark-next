# -*- coding: utf-8 -*-
"""抓取申万一级行业成分股 -> L0（把个股评级聚合到行业所需的映射）。

⚠️ 前视风险披露（重要，做结论时必须带上）
    `index_component_sw` 返回的是**当前**成分股。它带「计入日期」，但拿不到
    「剔除日期」，所以用当前成分回溯 2016 年的历史时点会有**幸存者偏差**：
    后来退出该行业的个股不会被计入。
    这是免费数据源的固有限制。用**一级行业**（成分多、单只进出影响小）可以
    缓解但不能消除。

落盘
    kind = "sw_component"，key = 一级行业代码（如 801010）
    records 每条：date（抓取日）/ code / name / weight / since / industry

用法
    python engine/fetch_sw_component.py
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawstore as RS  # noqa: E402

KIND = "sw_component"
SOURCE = "swsresearch:申万一级行业成分股 (akshare index_component_sw)"
NOTE = "当前成分股（含最新权重与计入日期）；前视风险见脚本 docstring"


def _code(v) -> str:
    s = str(v).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def main() -> None:
    import akshare as ak

    info = ak.sw_index_first_info()
    today = time.strftime("%Y-%m-%d")
    n_ind, n_stock, fails = 0, 0, []
    for _, r in info.iterrows():
        raw = str(r["行业代码"]).split(".")[0]
        name = str(r["行业名称"]).strip()
        try:
            cons = ak.index_component_sw(symbol=raw)
        except Exception as e:  # noqa: BLE001
            fails.append({"industry": raw, "name": name, "err": str(e)})
            print(f"  [err] {raw} {name}: {e}", flush=True)
            continue
        recs = []
        for _, c in cons.iterrows():
            code = _code(c.get("证券代码"))
            if not code or code == "nan":
                continue
            w, since = c.get("最新权重"), c.get("计入日期")
            recs.append({
                "date": today,
                "code": code,
                "name": (str(c.get("证券名称")).strip() or None),
                "weight": float(w) if pd.notna(w) else None,
                "since": str(since)[:10] if pd.notna(since) else None,
                "industry": name,
            })
        RS.put(KIND, raw, recs, SOURCE, merge=False, note=NOTE)
        n_ind += 1
        n_stock += len(recs)
        print(f"  {raw} {name}: {len(recs)} 只", flush=True)
        time.sleep(0.25)

    print(f"\n完成：{n_ind} 个一级行业、{n_stock} 条成分记录（含重复个股），失败 {len(fails)}")
    if fails:
        print("失败清单：", fails)


if __name__ == "__main__":
    main()
