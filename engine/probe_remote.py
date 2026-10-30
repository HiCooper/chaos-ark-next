#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""远程可达性探测：判断这台机器能否稳定抓到 ARK 依赖的国内数据源。

用途有两个：
  1. 在 GitHub Actions runner（境外出口）上跑一次，决定能否把整条链路托管到 CI；
  2. 在候选 VPS 上跑一次，确认那个机房的出口对申万/新浪友好。

只读，不写 L0、不动仓库。

    python engine/probe_remote.py
"""
from __future__ import annotations

import sys
import time
import warnings

warnings.filterwarnings("ignore")

SW_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")


def hr(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def main() -> int:
    import requests
    requests.packages.urllib3.disable_warnings()

    hr("出口 IP")
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        print("  ", r.json(), flush=True)
    except Exception as e:
        print("   取不到:", type(e).__name__, e, flush=True)

    hr("1) 申万官方日线原始接口")
    ok_sw = False
    t = time.time()
    try:
        r = requests.get(SW_URL, params={"swindexcode": "801010", "period": "DAY"},
                         headers={"User-Agent": UA}, timeout=60, verify=False)
        el = time.time() - t
        print(f"  HTTP {r.status_code}  耗时 {el:.1f}s  bytes={len(r.content)}", flush=True)
        d = (r.json() or {}).get("data") or []
        print(f"  行数={len(d)}  末行={d[-1].get('bargaindate') if d else None}", flush=True)
        print(f"  末行收盘={d[-1].get('closeindex') if d else None}", flush=True)
        ok_sw = bool(d)
    except Exception as e:
        print(f"  ✗ 失败 {type(e).__name__}: {e}", flush=True)

    hr("2) 新浪（实时 + 主站）")
    for u, name in [("https://hq.sinajs.cn/list=sh510300", "hq.sinajs 实时"),
                    ("https://finance.sina.com.cn", "sina 主站")]:
        t = time.time()
        try:
            r = requests.get(u, headers={"Referer": "https://finance.sina.com.cn",
                                         "User-Agent": UA}, timeout=30)
            body = r.text[:80].replace("\n", " ")
            print(f"  {name}: HTTP {r.status_code} 耗时 {time.time()-t:.1f}s  {body}", flush=True)
        except Exception as e:
            print(f"  {name}: ✗ {type(e).__name__}: {e}", flush=True)

    hr("3) akshare 封装")
    try:
        import akshare as ak
        t = time.time()
        df = ak.index_hist_sw(symbol="801010", period="day")
        print(f"  index_hist_sw: {len(df)} 行, 末行 {df['日期'].iloc[-1]}, 耗时 {time.time()-t:.1f}s", flush=True)
        t = time.time()
        df2 = ak.fund_etf_hist_sina(symbol="sh510300")
        print(f"  fund_etf_hist_sina: {len(df2)} 行, 末行 {df2['date'].iloc[-1]}, 耗时 {time.time()-t:.1f}s", flush=True)
        t = time.time()
        df3 = ak.index_realtime_sw(symbol="一级行业")
        print(f"  index_realtime_sw: {len(df3)} 行, 耗时 {time.time()-t:.1f}s", flush=True)
    except Exception as e:
        print(f"  ✗ akshare 失败 {type(e).__name__}: {e}", flush=True)

    hr("4) 单指数抓取耗时（估 120 个的总时长）")
    try:
        import akshare as ak
        codes = ["801010", "801030", "801040", "801050", "801080"]
        t0 = time.time()
        for c in codes:
            t = time.time()
            try:
                df = ak.index_hist_sw(symbol=c, period="day")
                print(f"  {c}: {time.time()-t:.1f}s  ({len(df)} 行)", flush=True)
            except Exception as e:
                print(f"  {c}: ✗ {type(e).__name__}", flush=True)
        el = time.time() - t0
        print(f"\n  5 个共 {el:.1f}s → 单个均 {el/5:.1f}s → 120 个约 {el/5*120/60:.0f} 分钟", flush=True)
    except Exception as e:
        print(f"  跳过：{type(e).__name__}: {e}", flush=True)

    hr("结论")
    if ok_sw:
        print("  ✓ 申万原始接口可达 —— 这台机器具备跑完整链路的条件", flush=True)
        return 0
    print("  ✗ 申万不可达 —— 这台机器无法作为数据抓取节点", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
