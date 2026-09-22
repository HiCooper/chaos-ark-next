# -*- coding: utf-8 -*-
"""ETF 份额折算（拆分 / 合并）的检测与复权修正。

## 为什么需要这个

`cache/etf_panel.json` 里存的是**不复权**价格（与新浪源一致，已用东财
`fund_etf_hist_em(adjust='')` 交叉验证逐值相同）。ETF 发生「份额折算」时，
单位价格会按整数倍跳变，而持有人价值不变：

    煤炭ETF国泰 1.515220   2024-04-11 2.612 -> 2024-04-12 1.297  (-50.3%)
    当日申万「煤炭开采」指数                          -0.48%
    房地产ETF南方 1.512200 2024-08-09 0.441 -> 2024-08-12 1.191  (+170.1%)
    当日申万「房地产服务」指数                        -4.47%

不复权价直接拿来算收益，就会把这种**价值中性事件**记成 ±50%~+220% 的
虚假盈亏。实测生产基线有 4 笔已结算交易被污染（原始记 −58.2% / −46.7% /
−17.7% / +14.4%，其中 −17.7% 那笔真实约 +65%）。

## 判定与还原

折算日无法用「跌得多」判定——创业板/科创 ETF 单日 ±20% 是真实的。
本模块用**参照收益**（同标的当日的真实涨跌）来区分：

    |ETF 当日收益 − 参照收益| > THRESH  →  判定为折算断点

参照优先取该 ETF 映射的申万行业指数（行业指数不存在折算事件）；映射缺失时
退化为「当日全池 ETF 收益的中位数」（ETF 之间高度共振，50% 的偏离不可能是行情）。

还原：折算价值中性 ⇒ 断点日 ETF 的真实收益就等于参照收益。于是

    k = (1 + 参照收益) / (1 + 断点日原始收益)

把断点日**及其之后**的价格全部乘 k（沿日期顺序累积），序列即连续，
且断点日以外的所有收益逐点不变。

修正后的序列是「前复权」意义上的连续价格，只用于计算收益/比值，
不改变任何真实历史价格用于展示。L0 原始层不动（write-once）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

THRESH = 0.25          # 判定阈值：与参照收益偏离超过此值即视为折算
PRICE_FIELDS = ("open", "high", "low", "close")


def _nav_series(etf_panel: dict) -> Dict[str, List[Optional[float]]]:
    dates = etf_panel["dates"]
    out = {}
    for sid, s in etf_panel["series"].items():
        out[sid] = [((s.get(d) or {}).get("close")) for d in dates]
    return out


def _ref_map(etf_panel: dict, panel: Optional[dict]) -> Dict[str, str]:
    """ETF sid -> 行业名（正查面板里存在该行业指数序列的才有用）。"""
    m = {}
    for sw, sid in (etf_panel.get("sw_to_etf") or {}).items():
        m[sid] = sw
    return m


def find_breaks(etf_panel: dict, panel: Optional[dict] = None,
                thresh: float = THRESH) -> Dict[str, List[dict]]:
    """返回 {sid: [{"date","prev","raw_ret","ref_ret","ref_src","factor"}]}。"""
    dates = etf_panel["dates"]
    nav = _nav_series(etf_panel)
    sw_of = _ref_map(etf_panel, panel)

    name2code = {}
    pser = {}
    if panel:
        name2code = {v["name"]: k for k, v in (panel.get("meta") or {}).items()}
        pser = panel.get("series") or {}

    # 当日全池收益（用于退化参照）
    med: List[Optional[float]] = []
    for i in range(len(dates)):
        rs = []
        for sid, arr in nav.items():
            a = arr[i - 1] if i > 0 else None
            b = arr[i]
            if a and b and a > 0:
                rs.append(b / a - 1.0)
        med.append(float(np.median(rs)) if len(rs) >= 8 else None)

    out: Dict[str, List[dict]] = {}
    for sid, arr in nav.items():
        sw = sw_of.get(sid)
        code = name2code.get(sw) if sw else None
        sref = pser.get(code) if code else None
        # 上一根**有数据**的 bar（不能用 dates[i-1]：标的常在断点前停牌/缺 bar，
        # 逐日历相邻比较会静默跳过这些断点——实测漏掉 9 处）
        prev_i, prev_c = None, None
        for i in range(len(dates)):
            b = arr[i]
            if not b:
                continue
            if prev_c and prev_c > 0:
                raw = b / prev_c - 1.0
                ref, src = None, None
                if sref:
                    ra = (sref.get(dates[prev_i]) or {}).get("close")
                    rb = (sref.get(dates[i]) or {}).get("close")
                    if ra and rb and ra > 0:
                        ref, src = rb / ra - 1.0, "index"
                if ref is None and med[i] is not None:
                    ref, src = med[i], "median"
                if ref is not None and abs(raw - ref) > thresh:
                    out.setdefault(sid, []).append({
                        "date": dates[i], "prev": dates[prev_i],
                        "raw_ret": round(raw, 6), "ref_ret": round(ref, 6),
                        "ref_src": src, "ratio": round(b / prev_c, 6),
                    })
            prev_i, prev_c = i, b
    return out


def apply_qfq(etf_panel: dict, panel: Optional[dict] = None,
              thresh: float = THRESH, verbose: bool = True) -> Tuple[dict, dict]:
    """原地返回修正后的 panel 副本 + 报告。"""
    breaks = find_breaks(etf_panel, panel, thresh)
    dates = etf_panel["dates"]
    idx = {d: i for i, d in enumerate(dates)}

    out = dict(etf_panel)
    out["series"] = {}
    report: Dict[str, list] = {}
    for sid, s in etf_panel["series"].items():
        bl = breaks.get(sid)
        if not bl:
            out["series"][sid] = s
            continue
        # 每个断点的累计系数（按日期顺序）
        cuts = []
        cum = 1.0
        for b in sorted(bl, key=lambda x: x["date"]):
            k = (1.0 + b["ref_ret"]) / (1.0 + b["raw_ret"])
            cum *= k
            cuts.append((idx[b["date"]], k, cum, b))
        ns = {}
        for d, row in s.items():
            i = idx.get(d)
            if i is None:
                ns[d] = row
                continue
            f = 1.0
            for (bi, _k, c, _b) in cuts:
                if i >= bi:
                    f = c
            if f == 1.0:
                ns[d] = row
            else:
                r2 = dict(row)
                for fld in PRICE_FIELDS:
                    v = r2.get(fld)
                    if v:
                        r2[fld] = round(v * f, 6)
                ns[d] = r2
        out["series"][sid] = ns
        report[sid] = [{"date": b["date"], "prev": b["prev"],
                        "raw_ret": b["raw_ret"], "ref_ret": b["ref_ret"],
                        "factor": round((1.0 + b["ref_ret"]) / (1.0 + b["raw_ret"]), 6),
                        "ref_src": b["ref_src"]} for (_i, _k, _c, b) in cuts]

    if verbose:
        n = sum(len(v) for v in report.values())
        print(f"[qfq] 检出折算断点 {n} 处 / {len(report)} 只 ETF")
        nm = (etf_panel.get("listing") or {})
        for sid, lst in sorted(report.items(), key=lambda x: -len(x[1])):
            for b in lst:
                print(f"      {sid} {nm.get(sid,{}).get('name','')[:12]:<14} {b['date']} "
                      f"原始 {b['raw_ret']*100:+7.1f}% 参照 {b['ref_ret']*100:+6.2f}% "
                      f"({b['ref_src']}) 复权因子 {b['factor']:.4f}")
    return out, report


def main() -> None:
    ep_path = f"{C.CACHE_DIR}/etf_panel.json"
    etf_panel = json.load(open(ep_path))
    panel = None
    if os.path.exists(f"{C.CACHE_DIR}/panel.json"):
        panel = json.load(open(f"{C.CACHE_DIR}/panel.json"))
    _out, report = apply_qfq(etf_panel, panel)

    # 顺带量化：有多少已结算交易的持有窗口跨越折算日
    rv = json.load(open(f"{C.OUT_DIR}/review.json"))
    ev = rv["settlement"]["events"]
    rd = rv["settlement"]["curve"]["dates"]
    tot = hit = 0
    for e in ev:
        if e["k"] != "S" or not e.get("bd"):
            continue
        tot += 1
        if e["i"] >= len(rd):
            continue
        sd = rd[e["i"]]
        for b in report.get(e["sid"], []):
            if e["bd"] < b["date"] <= sd:
                hit += 1
                true = (1 + e["ret"]) * b["factor"] - 1.0
                print(f"      污染: {e['bd']}→{sd} {e['sid']} {e['sw']} "
                      f"记账 {e['ret']*100:+.1f}% → 修正后约 {true*100:+.1f}%")
                break
    print(f"[qfq] 生产已结算交易 {tot} 笔，受折算污染 {hit} 笔")
    json.dump(report, open(f"{C.CACHE_DIR}/qfq_report.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"已写 {C.CACHE_DIR}/qfq_report.json")


if __name__ == "__main__":
    main()
