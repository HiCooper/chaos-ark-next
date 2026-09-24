# -*- coding: utf-8 -*-
"""数据层（申万官方口径）：用 akshare 拉申万指数 + 新浪 ETF/基准，替代被限流的东财。

来源与理由：
    - 申万指数日K：ak.index_hist_sw（申万宏源官方），比东财 BK 板块更权威，
      覆盖 2004 年至今，含 L1/L2/L3 全层级目录（index_*_info）。
    - ETF 日K：ak.fund_etf_hist_sina（新浪），不复权，含成交额（元）。
      注意：是**不复权**口径（已用东财 fund_etf_hist_em(adjust='') 交叉验证
      逐值一致）。ETF 份额折算（拆分/合并）造成的价格跳变不在数据层处理，
      由 engine/qfq.py 在加载 ETF 账簿时按参照收益检测并还原——不要在本层
      擅自改用前复权源，否则 qfq 的断点检测会失去作用对象。
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
SOURCE_SW_RT = "akshare.index_realtime_sw(申万实时行情)"
# 官方日线滞后时，最多用实时行情补最近几天（再往前的日子没有可用的昨收，补不了）
RT_FILL_MAX_DAYS = 2

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
                # overwrite_est=True：官方值可以覆盖"实时兜底"留下的估算行（est 标记），
                # 一手数据彼此之间仍然先到先得；这是官方出数后自动修复的唯一通道。
                RS.put("sw_index", code, recs, SOURCE_SW,
                       overwrite_est=True,
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
                       note="不复权;amount单位:元,与MIN_DAILY_AMOUNT口径一致;折算跳变由qfq.py处理")
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


# ---------------------------------------------------------------- 实时兜底（官方滞后时）
def fetch_sw_realtime() -> Dict[str, dict]:
    """申万实时行情快照 -> {code: {prev_close, open, high, low, close, volume, amount}}。

    注意**量纲**：该接口的成交量/成交额与官方日线 index_hist_sw 不是同一量纲
    （实测同一指数同比例偏大 ~80 倍，且 09-22 缺量额），因此本函数只把
    `prev_close / close / open / high / low`（点数，口径与官方一致）用于兜底，
    成交量与成交额一律不取（由 fill_realtime_tail 按基准量比估算）。
    """
    ak = _ak()
    out: Dict[str, dict] = {}
    for sym in ("一级行业", "二级行业", "三级行业"):
        try:
            df = ak.index_realtime_sw(symbol=sym)
        except Exception:
            continue
        for _, r in df.iterrows():
            try:
                out[str(r["指数代码"])] = {
                    "prev_close": float(r["昨收盘"]), "open": float(r["今开盘"]),
                    "high": float(r["最高价"]), "low": float(r["最低价"]),
                    "close": float(r["最新价"]),
                }
            except (KeyError, ValueError, TypeError):
                continue
        time.sleep(0.3)
    return out


def _bench_vol_ratio(bench_series: Dict[str, Dict[str, dict]], d_from: str,
                     d_to: str) -> Optional[float]:
    """基准指数（优先沪深300）成交量环比 —— 只用比值，量纲无关。"""
    for mc in ("1.000300", "1.000001", "1.000905", "1.000852", "0.399006"):
        s = bench_series.get(mc) or {}
        a, b = s.get(d_from), s.get(d_to)
        if a and b and a.get("volume") and b.get("volume"):
            return float(b["volume"]) / float(a["volume"])
    return None


def fill_realtime_tail(series: Dict[str, Dict[str, dict]],
                       bench_series: Dict[str, Dict[str, dict]],
                       meta: Dict[str, dict], verbose: bool = True,
                       now: Optional[time.struct_time] = None) -> Optional[dict]:
    """官方日线滞后时，用申万**实时行情**补最近 1~2 个交易日，并标明估算字段。

    规则（无前视、可追溯）：
      · 待补日期 = 基准（新浪，更新及时）交易日 > 官方申万最后一天，最多 RT_FILL_MAX_DAYS 天；
      · 收盘价：若该日就是当前会话（本地日期相同且已过 15:05）→ 实时"最新价"（完整 bar）；
                否则只有"昨收盘"可用 → 只补收盘价（open/high/low 置为收盘价并标记 est）；
      · 成交量/成交额：**一律估算** = 上一官方日 × 沪深300 成交量环比。
        不能直接用实时接口的量额——实测其量纲与官方日线不同（同比例偏大 ~80 倍）；
      · 实时缺失的个别指数（如 801961）：收盘价按当日全体中位涨跌幅推算；
      · 每条落地记录带 `est`（被估算/来源非官方的字段）；L0 写入 overwrite_est=True，
        官方出数后重跑 daily 会自动用真值覆盖估算值。
    """
    now = now or time.localtime()
    l1 = [c for c, m in meta.items() if m.get("level") == "L1" and c in series]
    if not l1:
        return None
    official_last = max(max(series[c]) for c in l1)
    bench_dates = sorted({d for s in bench_series.values() for d in s})
    tail = [d for d in bench_dates if d > official_last]
    if not tail:
        return None
    if len(tail) > RT_FILL_MAX_DAYS:
        # 实时行情只有「昨收 + 当日 bar」，覆盖不了多日缺口 → 交给 ETF 收益代理路径
        if verbose:
            print(f"  [realtime] 官方滞后 {len(tail)} 天 > {RT_FILL_MAX_DAYS}，"
                  f"实时行情不够用，改走 ETF 收益代理")
        return _fill_by_etf_proxy(series, bench_series, meta, official_last, tail, verbose=verbose)
    rt = fetch_sw_realtime()
    if not rt:
        if verbose:
            print("  [realtime] 实时行情不可用，改走 ETF 收益代理")
        return _fill_by_etf_proxy(series, bench_series, meta, official_last, tail, verbose=verbose)
    today = time.strftime("%Y-%m-%d", now)
    after_close = (now.tm_hour, now.tm_min) >= (15, 5)
    day_plan = [{"date": d, "is_current": (d == today and after_close),
                 "ratio": _bench_vol_ratio(bench_series, official_last, d)} for d in tail]
    for plan in day_plan:
        if plan["ratio"] is None:
            plan["ratio"] = 1.0
    # 横截面中位涨跌幅：实时缺失的指数用它兜底（相对官方最后一天）
    med_ret: Dict[str, float] = {}
    for plan in day_plan:
        rr = []
        for c, s in series.items():
            if c not in rt or max(s) != official_last:
                continue
            prev = float(s[official_last]["close"])
            cur = rt[c]["close"] if plan["is_current"] else rt[c]["prev_close"]
            if prev:
                rr.append(cur / prev - 1.0)
        if rr:
            rr.sort()
            med_ret[plan["date"]] = rr[len(rr) // 2]
    filled_rows: Dict[str, List[dict]] = {}
    n_est = 0
    for code, s in series.items():
        if meta.get(code, {}).get("level") not in ("L1", "L2"):
            continue
        if max(s) != official_last:      # 官方本来就缺更早日子的指数：不补，避免制造新缺口
            continue
        base = s[official_last]
        b0 = float(base["close"])
        v0, a0 = float(base.get("volume") or 0.0), float(base.get("amount") or 0.0)
        rows = []
        for plan in day_plan:
            d, r = plan["date"], rt.get(code)
            est = ["volume", "amount"]
            if r is None:                                  # 实时缺失 → 横截面中位推算
                close = b0 * (1.0 + med_ret.get(d, 0.0))
                o = h = l = close
                est = ["close", "open", "high", "low"] + est
                src, n_est = "realtime_xs_median", n_est + 1
            elif plan["is_current"]:                        # 当前会话：完整 bar
                close, o, h, l = r["close"], r["open"], r["high"], r["low"]
                src = "realtime"
            else:                                           # 过去的那一天：只有昨收
                close = r["prev_close"]
                o = h = l = close
                est = ["close_source(昨收)", "open", "high", "low"] + est
                src, n_est = "realtime_prev_close", n_est + 1
            ratio = float(plan["ratio"] or 1.0)
            rows.append({"date": d, "open": o, "high": h, "low": l, "close": close,
                         "volume": v0 * ratio, "amount": a0 * ratio,
                         "est": est, "src": src})
        if rows:
            filled_rows[code] = rows
    if not filled_rows:
        return None
    # 对齐体检：补出的行业指数在整段窗口的中位涨跌 vs 基准同窗口（差太多说明日期对不上）
    last = day_plan[-1]["date"]
    sw_rets = []
    for code, rows in filled_rows.items():
        b = float(series[code][official_last]["close"])
        row = [x for x in rows if x["date"] == last][0]
        if b:
            sw_rets.append(row["close"] / b - 1.0)
    sw_rets.sort()
    sw_med = sw_rets[len(sw_rets) // 2] if sw_rets else 0.0
    st = bench_series.get("1.000300") or bench_series.get("1.000001") or {}
    b0, b1 = st.get(official_last), st.get(last)
    bench_win = (float(b1["close"]) / float(b0["close"]) - 1.0) if (b0 and b1) else None
    if bench_win is not None and abs(sw_med - bench_win) > 0.05:
        if verbose:
            print(f"  [realtime] 对齐体检不通过：补出的行业指数中位涨跌 {sw_med:+.2%} vs "
                  f"基准同窗口 {bench_win:+.2%}（差 {abs(sw_med - bench_win):.1%}）"
                  f"→ 实时口径不可信，改走 ETF 收益代理")
        return _fill_by_etf_proxy(series, bench_series, meta, official_last, tail, verbose=verbose)
    # 落 L0（overwrite_est：官方出数后自动覆盖）
    for code, rows in filled_rows.items():
        RS.put("sw_index", code, rows, SOURCE_SW_RT, merge=True, overwrite_est=True,
               note="官方日线滞后时的实时兜底；被估算的字段见每行 est")
        for r in rows:
            series[code][r["date"]] = r
        series[code] = dict(sorted(series[code].items()))
    report = {
        "method": "realtime",
        "days": {p["date"]: {
            "close": "realtime(完整bar)" if p["is_current"] else "realtime(昨收)",
            "volume": "est(沪深300量比)" if p["ratio"] != 1.0 else "est(沿用前值)",
            "ohlc": "realtime" if p["is_current"] else "est(=收盘)",
            "method": "realtime",
        } for p in day_plan},
        "codes": len(filled_rows),
        "codes_estimated": n_est,
        "official_last": official_last,
        "align_check": {"sw_median_ret": round(sw_med, 5),
                        "bench_ret": round(bench_win, 5) if bench_win is not None else None},
        "source": SOURCE_SW_RT,
    }
    if verbose:
        days_txt = "、".join(f"{p['date']}（{'实时完整bar' if p['is_current'] else '实时昨收'}）"
                            for p in day_plan)
        print(f"  [realtime] 官方停在 {official_last}，已用实时行情兜底 {len(tail)} 天：{days_txt}")
        print(f"             量额按沪深300量比估算；对齐体检 行业中位 {sw_med:+.2%} vs "
              f"基准 {bench_win:+.2%} ✓" if bench_win is not None else "             量额按沪深300量比估算")
        print("             官方出数后重跑 daily 会自动用真值覆盖（L0 overwrite_est=True）")
    return report


def _raw_etf_bars(sid: str) -> Dict[str, dict]:
    """原始 ETF 日线（data/raw/etf/<sid>.json）——L0 是及时的（新浪），且早于申万官方。"""
    recs = RS.get("etf", sid) or []
    return {r["date"]: r for r in recs if r.get("close")}


def _etf_proxy_returns(sw_to_etf: Dict[str, str], name: str, dates: List[str],
                       verbose: bool = True) -> Optional[Dict[str, tuple]]:
    """用该行业对应 ETF 的日收益，代理行业指数在缺口期的日收益。

    只补**收益**、不编造点位：把 ETF 的日收益率链式乘到「最后一个官方收盘价」上。
    返回 {date: (ret, vol_ratio, clamped)}；拿不到 ETF 返回 None。
    """
    sid = sw_to_etf.get(name)
    if not sid:
        return None
    bars = _raw_etf_bars(sid)
    if not bars:
        return None
    ks = sorted(bars)
    out: Dict[str, tuple] = {}
    for d in dates:
        prev = [x for x in ks if x < d]
        if d not in bars or not prev:
            return None
        p0, p1 = bars[prev[-1]]["close"], bars[d]["close"]
        if not p0 or not p1:
            return None
        ret = p1 / p0 - 1.0
        clamped = abs(ret) > 0.11          # 单日 ±11% 之外视为异常（份额折算/数据错），夹住并标记
        ret = max(-0.11, min(0.11, ret))
        v0, v1 = bars[prev[-1]].get("volume"), bars[d].get("volume")
        vr = (float(v1) / float(v0)) if (v0 and v1) else None
        out[d] = (ret, vr, clamped)
    return out


def _fill_by_etf_proxy(series: Dict[str, Dict[str, dict]],
                       bench_series: Dict[str, Dict[str, dict]],
                       meta: Dict[str, dict], official_last: str, tail: List[str],
                       verbose: bool = True) -> Optional[dict]:
    """缺口 > 实时行情能覆盖的天数时，用各行业 ETF 的日收益前推补齐（任意长度缺口）。

    与实时路径同一约定：逐行打 `est`、L0 写入 overwrite_est=True（官方出数后自动覆盖）、
    量额估算、末尾做对齐体检。ETF 数据本身是及时的（新浪），因此这条链路不受申万滞后影响；
    代价是：ETF 有折溢价/跟踪误差/份额折算，代理期的**行业点位是推算的**（收益口径仍真实）。
    """
    try:
        import json as _json
        ep = _json.load(open(f"{C.CACHE_DIR}/etf_panel.json", encoding="utf-8"))
        sw_to_etf = ep.get("sw_to_etf") or {}
    except Exception:
        sw_to_etf = {}
    if not sw_to_etf:
        return None
    filled: Dict[str, List[dict]] = {}
    n_est = n_xs = 0
    for code, s in series.items():
        if meta.get(code, {}).get("level") not in ("L1", "L2"):
            continue
        if max(s) != official_last:
            continue
        name = meta[code]["name"]
        proxy = _etf_proxy_returns(sw_to_etf, name, tail, verbose=verbose)
        base = s[official_last]
        b0 = float(base["close"])
        v0, a0 = float(base.get("volume") or 0.0), float(base.get("amount") or 0.0)
        rows: List[dict] = []
        if proxy:
            cur = b0
            for i, d in enumerate(tail):
                ret, vr, clamped = proxy[d]
                cur = cur * (1.0 + ret)
                ratio = _bench_vol_ratio(bench_series, official_last, d) or 1.0
                est = ["close_proxy", "open", "high", "low", "volume", "amount"]
                if clamped:
                    est.append("clamped_11pct")
                rows.append({"date": d, "open": cur, "high": cur, "low": cur, "close": cur,
                             "volume": v0 * (vr or ratio), "amount": a0 * (vr or ratio),
                             "est": est, "src": "etf_proxy"})
                n_est += 1
        else:
            # 无对应 ETF 的行业：用当日全体被代理行业的横截面中位收益推算
            rows = []
        if rows:
            filled[code] = rows
    if not filled:
        return None
    # 无 ETF 的行业 → 横截面中位收益
    for code, s in series.items():
        if meta.get(code, {}).get("level") not in ("L1", "L2") or code in filled:
            continue
        if max(s) != official_last:
            continue
        base = s[official_last]
        b0 = float(base["close"])
        v0, a0 = float(base.get("volume") or 0.0), float(base.get("amount") or 0.0)
        rows = []
        cur = b0
        for d in tail:
            rr = sorted((1.0 + filled[c][i]["close"] / float(series[c][official_last]["close"]) - 1.0)
                        for c in filled for i, x in enumerate(filled[c]) if x["date"] == d)
            med = rr[len(rr) // 2] if rr else 1.0
            day_ret = cur * med / cur - 1.0 if cur else 0.0
            cur = cur * (1.0 + day_ret)
            ratio = _bench_vol_ratio(bench_series, official_last, d) or 1.0
            rows.append({"date": d, "open": cur, "high": cur, "low": cur, "close": cur,
                         "volume": v0 * ratio, "amount": a0 * ratio,
                         "est": ["close_xs_median", "open", "high", "low", "volume", "amount"],
                         "src": "etf_proxy_xs"})
            n_xs += 1
        if rows:
            filled[code] = rows
    # 对齐体检：补出的行业中位涨跌 vs 基准同窗口
    last = tail[-1]
    sw_rets = sorted(filled[c][-1]["close"] / float(series[c][official_last]["close"]) - 1.0
                     for c in filled)
    sw_med = sw_rets[len(sw_rets) // 2] if sw_rets else 0.0
    st = bench_series.get("1.000300") or bench_series.get("1.000001") or {}
    b0, b1 = st.get(official_last), st.get(last)
    bench_win = (float(b1["close"]) / float(b0["close"]) - 1.0) if (b0 and b1) else None
    if bench_win is not None and abs(sw_med - bench_win) > 0.05:
        if verbose:
            print(f"  [etf_proxy] 对齐体检不通过：代理行业中位涨跌 {sw_med:+.2%} vs 基准 {bench_win:+.2%}"
                  f"（差 {abs(sw_med - bench_win):.1%}）→ 放弃兜底")
        return None
    for code, rows in filled.items():
        RS.put("sw_index", code, rows, SOURCE_SW_RT, merge=True, overwrite_est=True,
               note="申万官方滞后时的 ETF 收益代理兜底（见 docs/数据兜底-实时行情补尾部.md）")
        for r in rows:
            series[code][r["date"]] = r
        series[code] = dict(sorted(series[code].items()))
    report = {
        "method": "etf_proxy",
        "days": {d: {"close": "etf_proxy(ETF 日收益前推)", "volume": "est(该 ETF 量比/沪深300量比)",
                     "ohlc": "est(=收盘)", "method": "etf_proxy"} for d in tail},
        "codes": len(filled), "codes_estimated": n_est + n_xs,
        "official_last": official_last,
        "align_check": {"sw_median_ret": round(sw_med, 5),
                        "bench_ret": round(bench_win, 5) if bench_win is not None else None},
        "source": "新浪 ETF 日线（data/raw/etf）",
    }
    if verbose:
        print(f"  [etf_proxy] 官方停在 {official_last}，缺口 {len(tail)} 天，用 {len(filled)} 个行业的 ETF "
              f"日收益前推补齐：{'、'.join(tail)}")
        _bt = f"{bench_win:+.2%}" if bench_win is not None else "—"
        print(f"             其中 {n_xs} 行（无 ETF 行业）用横截面中位收益；"
              f"对齐体检 行业中位 {sw_med:+.2%} vs 基准 {_bt} ✓")
        print("             官方出数后重跑 daily 会自动用真值覆盖（L0 overwrite_est=True）")
    return report


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

    # 官方申万日线滞后于基准（新浪）时补尾部：≤2 天用实时行情、更长用 ETF 收益代理
    # （估算字段逐行带 est 标记，官方出数后 L0 overwrite_est 自动覆盖）
    rt_fill = fill_realtime_tail(series, bench_series, meta, verbose=verbose)

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
        "tail_fill": rt_fill,
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
