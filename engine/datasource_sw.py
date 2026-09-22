# -*- coding: utf-8 -*-
"""数据层（申万官方口径）：用 akshare 拉申万指数 + 新浪 ETF/基准，替代被限流的东财。

来源与理由：
    - 申万指数日K：ak.index_hist_sw（申万宏源官方），比东财 BK 板块更权威，
      覆盖 2004 年至今，含 L1/L2/L3 全层级目录（index_*_info）。
    - ETF 日K：ak.fund_etf_hist_sina（新浪），前复权，含成交额（元）。
    - 基准指数日K：ak.stock_zh_index_daily（新浪），只有量没有额（amount 置 0，
      特征里 amount 相关维度只做相对比值，且基准不参与量能打分）。

产出与东财版 datasource.build_panel 完全同构的 cache/panel.json：
    {generated_at, dates, meta: {code: {name, level}}, series: {code: {date: row}}}
代码命名空间从 90.BKxxxx 换成申万官方代码（801010 等）；下游 feature_store /
scoring / classify 只按 name/level 工作，对代码前缀无假设。

L0 语义：所有抓取先落 data/raw/（rawstore，write-once + merge-append），
再由本模块从 L0 重组面板；刷新重跑不直接依赖网络可达。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import rawstore as RS  # noqa: E402

SOURCE_SW = "akshare.index_hist_sw(申万宏源官方)"
SOURCE_ETF = "akshare.fund_etf_hist_sina(新浪)"
SOURCE_BENCH = "akshare.stock_zh_index_daily(新浪)"
SOURCE_CATALOG = "akshare.sw_index_*_info(申万宏源官方)"

BENCH_SINA = {  # config.BENCHMARKS 的 market.code -> 新浪符号
    "1.000001": "sh000001", "1.000300": "sh000300", "0.399006": "sz399006",
    "1.000852": "sh000852", "1.000905": "sh000905",
}

# EM 白名单名 -> 申万2021官方名的确定性映射（None = 无对应官方指数，跳过）。
# 绝大多数差异只是 Ⅱ/Ⅲ 后缀（自动剥除即可），这里只列真正改名的。
SUB_SECTOR_RENAME = {
    "汽车整车": "乘用车",      # 2021版拆分为乘用车/商用车，取主口径
    "光模块": None,            # 东财概念板块，申万官方无此指数
    "房屋建设": None,          # 2021版并入「基建市政工程」，口径不再可比
    "工程咨询服务": None,      # 同上
    "港口航运": "航运港口",    # 2021版合并名
}


def _ak():
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    return ak


def _df_records(df) -> List[dict]:
    """申万官方日K DataFrame -> [{date,open,close,high,low,volume,amount}, ...]。"""
    out = []
    for _, r in df.iterrows():
        try:
            out.append({
                "date": str(r["日期"]),
                "open": float(r["开盘"]), "close": float(r["收盘"]),
                "high": float(r["最高"]), "low": float(r["最低"]),
                "volume": float(r["成交量"]), "amount": float(r["成交额"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _sina_records(df, has_amount: bool) -> List[dict]:
    out = []
    for _, r in df.iterrows():
        try:
            out.append({
                "date": str(r["date"]),
                "open": float(r["open"]), "close": float(r["close"]),
                "high": float(r["high"]), "low": float(r["low"]),
                "volume": float(r["volume"]),
                "amount": float(r["amount"]) if has_amount else 0.0,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------- 目录快照
def fetch_catalog(refresh: bool = False) -> Dict[str, Dict[str, str]]:
    """申万 L1/L2/L3 层级目录 -> {code: name}。快照落 L0 catalog/。"""
    cached = RS.get("catalog", "sw_all")
    if cached and not refresh:
        out: Dict[str, List[dict]] = {"L1": [], "L2": [], "L3": []}
        for e in cached:  # 扁平列表，tag 区分层级
            out.setdefault(e["tag"], []).append({"code": e["code"], "name": e["name"]})
        return {k: {e["code"]: e["name"] for e in v} for k, v in out.items()}
    ak = _ak()
    out: Dict[str, List[dict]] = {}
    for fn, tag in [(ak.sw_index_first_info, "L1"),
                    (ak.sw_index_second_info, "L2"),
                    (ak.sw_index_third_info, "L3")]:
        df = fn()
        out[tag] = [{"code": str(r["行业代码"]).split(".")[0], "name": str(r["行业名称"])}
                    for _, r in df.iterrows()]
        time.sleep(0.3)
    RS.put("catalog", "sw_all", [{"tag": t, **e} for t, es in out.items() for e in es],
           SOURCE_CATALOG, merge=False,
           note="申万层级目录快照；L1/L2/L3 合一存储，tag 区分层级")
    return {k: {e["code"]: e["name"] for e in v} for k, v in out.items()}


# ---------------------------------------------------------------- 指数日K
def fetch_sw_daily(code: str, refresh: bool = False) -> Optional[List[dict]]:
    cached = RS.get("sw_index", code)
    if cached and not refresh:
        return cached
    ak = _ak()
    for attempt in range(3):
        try:
            df = ak.index_hist_sw(symbol=code, period="day")
            recs = _df_records(df)
            if recs:
                RS.put("sw_index", code, recs, SOURCE_SW,
                       note="成交量单位:股/亿股口径,成交额:亿元;仅做相对比值使用")
                return recs
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return cached


def fetch_bench_daily(market_code: str, refresh: bool = False) -> Optional[List[dict]]:
    sym = BENCH_SINA[market_code]
    cached = RS.get("bench", market_code)
    if cached and not refresh:
        return cached
    ak = _ak()
    for attempt in range(3):
        try:
            df = ak.stock_zh_index_daily(symbol=sym)
            recs = _sina_records(df, has_amount=False)
            if recs:
                RS.put("bench", market_code, recs, SOURCE_BENCH,
                       note="新浪指数日K无成交额列, amount=0; 基准不参与量能打分")
                return recs
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return cached


def fetch_etf_daily(secid: str, refresh: bool = False) -> Optional[List[dict]]:
    """东财 secid（1.512400 / 0.159915）-> 新浪 ETF 日K。amount 单位: 元。"""
    cached = RS.get("etf", secid)
    if cached and not refresh:
        return cached
    sym = ("sh" if secid.startswith("1.") else "sz") + secid.split(".")[-1]
    ak = _ak()
    for attempt in range(3):
        try:
            df = ak.fund_etf_hist_sina(symbol=sym)
            recs = _sina_records(df, has_amount=True)
            if recs:
                RS.put("etf", secid, recs, SOURCE_ETF,
                       note="前复权;amount单位:元,与MIN_DAILY_AMOUNT口径一致")
                return recs
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return cached


# ---------------------------------------------------------------- 行业宇宙
def build_universe(catalog: Dict[str, Dict[str, str]]) -> Dict[str, dict]:
    """申万官方目录 -> ARK 行业宇宙。L1 全量（排除综合），L2/L3 按白名单。"""
    l1 = catalog["L1"]
    merged = {**catalog["L2"], **catalog["L3"]}
    by_name = {}
    for code, nm in merged.items():
        by_name.setdefault(nm, code)
    # 白名单名 -> 官方名的反查表（剥 Ⅱ/Ⅲ + 显式改名表）
    official_to_canon = {}
    for name in C.SUB_SECTOR_WHITELIST:
        if name in by_name:
            official_to_canon[name] = name
            continue
        hit = None
        for suf in ("Ⅱ", "Ⅲ"):
            if (name + suf) in by_name:
                hit = name + suf
                break
        if hit:
            official_to_canon[hit] = name
    for em_name, sw_name in SUB_SECTOR_RENAME.items():
        if sw_name and sw_name in by_name:
            official_to_canon.setdefault(sw_name, em_name)

    universe: Dict[str, dict] = {}
    for code, name in l1.items():
        if name in C.L1_EXCLUDE:
            continue
        universe[code] = {"code": code, "name": name, "level": "L1"}
    for code, official in merged.items():
        canon = official_to_canon.get(official)
        if canon and code not in universe:
            universe[code] = {"code": code, "name": canon, "level": "L2"}
    return universe


# ---------------------------------------------------------------- 面板
def build_panel(refresh: bool = False, verbose: bool = True) -> dict:
    """从 L0（必要时先抓）组装 panel.json，结构与东财版完全同构。"""
    catalog = fetch_catalog(refresh=refresh)
    universe = build_universe(catalog)
    if verbose:
        n_l1 = sum(1 for u in universe.values() if u["level"] == "L1")
        print(f"  行业宇宙：L1={n_l1} + L2/L3佐证={len(universe) - n_l1}")

    series: Dict[str, Dict[str, dict]] = {}
    meta: Dict[str, dict] = {}
    missing: List[str] = []
    for i, (code, u) in enumerate(universe.items()):
        recs = fetch_sw_daily(code, refresh=refresh)
        if not recs:
            missing.append(f"{code} {u['name']}")
            continue
        series[code] = {r["date"]: r for r in recs}
        meta[code] = {"name": u["name"], "level": u["level"]}
        if verbose and (i + 1) % 25 == 0:
            print(f"  指数进度 {i+1}/{len(universe)}")
        time.sleep(0.2)

    bench_series = {}
    for mc, name in C.BENCHMARKS.items():
        recs = fetch_bench_daily(mc, refresh=refresh)
        if recs:
            bench_series[mc] = {r["date"]: r for r in recs}
            meta[mc] = {"name": name, "level": "BENCH"}
    series.update(bench_series)
    if verbose and missing:
        print(f"  [warn] 抓取失败 {len(missing)} 个: {missing[:8]}")

    # 交易日轴：L1 全体指数日期的并集（申万官方同一日历，取并集防个别指数缺失）
    l1_codes = [c for c, m in meta.items() if m["level"] == "L1"]
    dates = sorted({d for c in l1_codes for d in series[c]})
    dset = set(dates)
    series = {c: {d: r for d, r in s.items() if d in dset} for c, s in series.items()}

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "sw_official+sina (akshare)",
        "dates": dates,
        "meta": meta,
        "series": series,
    }
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(f"{C.CACHE_DIR}/panel.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    if verbose:
        print(f"  面板完成：{len(series)} 个标的，{len(dates)} 个交易日 "
              f"({dates[0]} -> {dates[-1]})")
    return out


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    build_panel(refresh=refresh)
