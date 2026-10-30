# -*- coding: utf-8 -*-
"""构建 ETF 面板：为每个申万一级行业挑选「最早上市 + 流动性最好」的可交易 ETF。

产出 cache/etf_panel.json：
    {
      "dates": [...],                       # 交易日（与行业指数面板一致）
      "series": {etf_secid: {date: {close, amount, ...}}},
      "listing": {etf_secid: {"name":..., "start":..., "avg_amount":...}},
      "sw_to_etf": {sw_name: etf_secid},    # 每个行业的最终主 ETF
      "tradeable": {date: [sw_name, ...]},  # 每个交易日可交易的行业集合
    }

关键约束（不可绕过）：
    ETF 上市日之前，该行业在回测中被标记为「不可交易」，不参与收益统计。
    绝不用行业指数回填，否则收益就是不可执行的虚假数字。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import datasource as DS  # noqa: E402
import datasource_sw as DSW  # noqa: E402
import etf_map as EM  # noqa: E402

OUT_PATH = f"{C.CACHE_DIR}/etf_panel.json"
FUND_NAMES_PATH = f"{C.CACHE_DIR}/fund_names.json"


def load_fund_names() -> Dict[str, str]:
    """ETF 代码 -> 中文名。来源：东财全量基金代码表（离线缓存）。"""
    if os.path.exists(FUND_NAMES_PATH):
        with open(FUND_NAMES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    txt = DS._curl("https://fund.eastmoney.com/js/fundcode_search.js")
    m: Dict[str, str] = {}
    if txt:
        try:
            i = txt.find("[")
            arr = json.loads(txt[i:txt.rfind("]") + 1])
            for a in arr:
                m[a[0]] = a[2]
        except Exception:
            pass
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(FUND_NAMES_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return m


def scan_etfs(refresh: bool = False, verbose: bool = True,
              recent_bars: Optional[int] = None) -> Dict[str, dict]:
    """抓取全部候选 ETF，记录上市日与流动性。

    recent_bars 给定时走增量（东财只取最近 N 根），否则全量（新浪）。
    """
    info: Dict[str, dict] = {}
    names = load_fund_names()
    cands = EM.all_etf_secids()

    # 增量主路径：一次批量拿全部 ETF 的当日快照（含成交额），约 4 次请求覆盖全池，
    # 这是替代"每只 ETF 重下 20 年历史"的关键——体量从 ~170MB 降到 ~70KB。
    snaps: Dict[str, dict] = {}
    if refresh and recent_bars:
        t0 = time.time()
        snaps = DSW.fetch_etf_snapshot_batch([sid for sid, _ in cands])
        if verbose:
            dates = sorted({v["date"] for v in snaps.values()})
            print(f"  批量快照：{len(snaps)}/{len(cands)} 个标的  {time.time()-t0:.1f}s  "
                  f"日期={dates}")

    for k, (sid, name) in enumerate(cands):
        code = sid.split(".")[-1]
        name = names.get(code, name)
        s = DSW.fetch_etf_daily(sid, refresh=refresh, recent_bars=recent_bars,
                                snapshot=snaps.get(sid))
        if not s:
            if verbose:
                print(f"  [fail] {sid} {name}")
            time.sleep(0.5)
            continue
        dates = [d["date"] for d in s]
        # 流动性：用最近 60 个交易日的日均成交额
        tail = s[-60:]
        avg_amt = sum(d["amount"] for d in tail) / max(len(tail), 1)
        info[sid] = {
            "secid": sid, "name": name,
            "start": dates[0], "end": dates[-1], "n": len(s),
            "avg_amount_60": avg_amt,
        }
        if verbose:
            print(f"  {sid} {name}: {dates[0]} -> {dates[-1]} n={len(s)} "
                  f"日均额={avg_amt/1e8:.2f}亿")
        time.sleep(C.FETCH_SLEEP)
    return info


def pick_main_etf(info: Dict[str, dict], key: str) -> Optional[str]:
    """当前（最新）时点的主 ETF：先看匹配度，再看流动性。

    注意：这是「今天」的选择，用于展示与今日结论绑定；
    历史回测必须用 build_sw_choice 的逐日选择。
    """
    best = None
    for sid, _name, match in EM.resolve_candidates(key):
        it = info.get(sid)
        if not it or it["avg_amount_60"] < EM.MIN_DAILY_AMOUNT:
            continue
        score = (match, it["avg_amount_60"])
        if best is None or score > best[0]:
            best = (score, sid)
    return best[1] if best else None


def build_sw_choice(dates: List[str], series: Dict[str, dict],
                    verbose: bool = True) -> Dict[str, Dict[str, str]]:
    return _build_choice_for(EM.all_keys(), dates, series, verbose=verbose)


def _build_choice_for(keys: List[str], dates: List[str], series: Dict[str, dict],
                      verbose: bool = True) -> Dict[str, Dict[str, str]]:
    """逐日选择：在每个交易日，取该行业「已上市且滚动 20 日成交额最高」的 ETF。

    这是真实可执行的模拟——现实中你在 2016 年只能买当时存在的那只，
    在 2024 年会买当时流动性最好的那只。用固定的一只 ETF 回测全程会
    产生两种假象：早期前视（用了还没上市的 ETF）、后期失真（用了僵尸 ETF）。
    """
    import numpy as np
    choice: Dict[str, Dict[str, str]] = {}
    n = len(dates)
    win = EM.ROLL_AMOUNT_WIN
    for sw in keys:
        cands = EM.resolve_candidates(sw)
        usable = [(sid, name, m) for sid, name, m in cands if sid in series]
        if not usable:
            continue
        k = len(usable)
        amt = np.zeros((k, n))
        ok = np.zeros((k, n), dtype=bool)
        match = np.array([m for _s, _n_, m in usable], dtype=float)
        for ci, (sid, _nm, _m) in enumerate(usable):
            sd = series[sid]
            for di, d in enumerate(dates):
                r = sd.get(d)
                if r and r.get("close"):
                    amt[ci, di] = float(r.get("amount") or 0.0)
                    ok[ci, di] = True
        # 滚动 20 日日均成交额（不足窗口用已有数据）
        roll = np.zeros((k, n))
        csum = np.cumsum(np.where(ok, amt, 0.0), axis=1)
        for di in range(n):
            lo = max(0, di - win + 1)
            span = di - lo + 1
            prev = csum[:, lo - 1] if lo > 0 else 0.0
            roll[:, di] = (csum[:, di] - prev) / span
        # 匹配度优先、流动性次之：把 match 放大到远高于成交额的量级
        eff = np.where(ok & (roll >= EM.MIN_DAILY_AMOUNT),
                       match[:, None] * 1e15 + roll, -1.0)
        best = eff.argmax(axis=0)
        bestval = eff.max(axis=0)
        per_date: Dict[str, str] = {}
        for di, d in enumerate(dates):
            if bestval[di] > 0:
                per_date[d] = usable[int(best[di])][0]
        choice[sw] = per_date
        if verbose:
            got = len(per_date)
            first = next((d for d in dates if d in per_date), None)
            print(f"    {sw:<6} 可交易日 {got:>5}/{n}  起始 {first or '—'}")
    return choice


def build(refresh: bool = False, verbose: bool = True, full: bool = False) -> dict:
    # 日常增量：只回看最近 N 根 bar；--full 或首次建档才走全量
    recent_bars = None if full else DSW.ETF_INCREMENTAL_BARS
    info = scan_etfs(refresh=refresh, verbose=verbose, recent_bars=recent_bars)
    keys = EM.all_keys()
    sw_to_etf: Dict[str, str] = {}
    for sw in keys:
        m = pick_main_etf(info, sw)
        if m:
            sw_to_etf[sw] = m
    if verbose:
        print(f"  当前主 ETF 映射：{len(sw_to_etf)}/{len(keys)}"
              f"（一级行业 + 细分赛道）")
        for sw, sid in sorted(sw_to_etf.items(), key=lambda kv: -info[kv[1]]["avg_amount_60"]):
            print(f"    {sw:<6} -> {info[sid]['name']:<18} "
                  f"上市 {info[sid]['start']}  日均额 {info[sid]['avg_amount_60']/1e8:.2f}亿")

    # 交易日：与行业指数面板对齐（下面还会按 ETF 实际数据尾部截断）
    panel = DS.load_panel()
    dates = panel["dates"]
    dset = set(dates)

    series: Dict[str, dict] = {}
    for sid in info:
        s = DSW.fetch_etf_daily(sid)
        if not s:
            continue
        series[sid] = {d["date"]: d for d in s if d["date"] in dset}

    # 结算层日历绝不能推进到「ETF 还没有行情」的那天：T+1 开盘建仓要用当日开盘价，
    # 缺价会让建仓被静默跳过、盯市退化成上一收盘价。新浪 ETF 常比申万晚一晚出数，
    # 所以按 ETF 实际最后一天截断（判断层可以领先，结算层不能）。
    etf_last = max((d for s in series.values() for d in s), default=None)
    if etf_last and dates and dates[-1] > etf_last:
        dropped = [d for d in dates if d > etf_last]
        dates = [d for d in dates if d <= etf_last]
        dset = set(dates)
        for sid in list(series):
            series[sid] = {d: r for d, r in series[sid].items() if d in dset}
        if verbose:
            print(f"  [warn] ETF 日线只到 {etf_last}（判断层已到 {dropped[-1]}）→ "
                  f"结算层日历截断到 {etf_last}；{'、'.join(dropped)} 等新浪出数后重跑 daily")

    if verbose:
        print("  逐日 ETF 选择（已上市 + 流动性最优）：")
    sw_choice = build_sw_choice(dates, series, verbose=verbose)

    # 每个交易日的可交易行业
    tradeable: Dict[str, List[str]] = {}
    for d in dates:
        tradeable[d] = [sw for sw, pd_ in sw_choice.items() if d in pd_]

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dates": dates,
        "series": series,
        "listing": info,
        "sw_to_etf": sw_to_etf,
        "sw_choice": sw_choice,
        "tradeable": tradeable,
        "min_daily_amount": EM.MIN_DAILY_AMOUNT,
    }
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    full = "--full" in sys.argv          # 强制全量重建（首次建档 / 排查数据异常用）
    r = build(refresh=refresh, full=full)
    print("SAVED", OUT_PATH)
    print("行业覆盖:", len(r["sw_to_etf"]))
    # 覆盖度演进
    dates = r["dates"]
    for y in range(2016, 2027):
        ys = [d for d in dates if d.startswith(str(y))]
        if not ys:
            continue
        n = len(r["tradeable"][ys[-1]])
        print(f"  {y}-12-31 可交易行业数: {n}")
