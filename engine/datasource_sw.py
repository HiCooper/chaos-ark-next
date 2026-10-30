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

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import rawstore as RS  # noqa: E402

SOURCE_SW = "akshare.index_hist_sw(申万宏源官方)"
SOURCE_ETF = "akshare.fund_etf_hist_sina(新浪)"
SOURCE_ETF_EM = "akshare.fund_etf_hist_em(东财,区间)"
SOURCE_ETF_TX = "tencent.qt.gtimg.cn(腾讯批量快照)"
SOURCE_BENCH = "akshare.stock_zh_index_daily(新浪)"
SOURCE_CATALOG = "akshare.sw_index_*_info(申万宏源官方)"
SOURCE_SW_RT = "akshare.index_realtime_sw(申万实时行情)"
SOURCE_SW_SNAP = "swsresearch.index_publish/current(申万批量快照)"
SOURCE_BENCH_SNAP = "hq.sinajs.cn(新浪实时行情)"
# 官方日线滞后时，最多用实时行情补最近几天（再往前的日子没有可用的昨收，补不了）
RT_FILL_MAX_DAYS = 2

# ETF 日常增量：只取最近 N 根 bar，而不是重下全历史。
# N=15：15 个交易日足以跨过春节/国庆 7~9 天连休，请求体量仍是几百字节。
# 用 lmt（根数）而非 beg/end：实测 beg 会被对端直接断连，lmt 稳定。
ETF_INCREMENTAL_BARS = 15


class _EmBreaker:
    """东财熔断器：连续失败达到阈值后，本轮直接跳过东财、整体回落新浪。

    东财对密集请求很敏感（实测短时间 20 余次即被断连，90s 未恢复）。若不做熔断，
    123 个 ETF 会各自重试 3 次才回退，反而比原来更慢。这里只记"连续失败"，
    任意一次成功即清零。
    """
    THRESHOLD = 3

    def __init__(self) -> None:
        self.streak = 0

    @property
    def open(self) -> bool:
        return self.streak >= self.THRESHOLD

    def hit(self) -> None:
        self.streak += 1

    def reset(self) -> None:
        self.streak = 0


EM_BREAKER = _EmBreaker()

# 东财日K：akshare 的 fund_etf_hist_em 不带 User-Agent，会被对端直接断连
# （实测 RemoteDisconnected），且还要多发一次市场识别请求。这里直连：
# 我们本来就持有 secid，一次请求即可，且带 UA 后稳定。
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 申万官方日线（全历史）：trend/ 不支持任何区间/分页参数，单指数必返 6481 行。
# 单请求 6~24s 且以服务端生成耗时为多（1.4MB 不是瓶颈），串行 120 只 = 20~40 分钟。
# 因此校准路径改为「并行取数 + 串行落盘」：压缩的是等待，不是"重复下载"这件事本身。
SW_TREND_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"
SW_FETCH_WORKERS = 8

BENCH_SINA = {  # config.BENCHMARKS 的 market.code -> 新浪符号
    "1.000001": "sh000001", "1.000300": "sh000300", "0.399006": "sz399006",
    "1.000852": "sh000852", "1.000905": "sh000905",
}

# ---------------------------------------------------------------- 申万批量快照
# 官方日线接口 index_publish/trend/ 不支持任何区间/分页参数（page/page_size、
# startdate/enddate/begin_date/type 实测全部无效），恒返 6481 行 / 1.47MB，
# 120 个指数 = 约 365MB / 40 分钟。而同一套 API 的 current/ 是批量快照：
# 2 次请求（一级行业 31 + 二级行业 124）、约 24KB，拿到全部指数当日 OHLC+量额。
#
# 字段口径（对照 akshare.index_realtime_sw 的列映射，并用官方 L0 反推验证）：
#   l3=昨收  l4=今开  l5=成交额  l6=最高  l7=最低  l8=最新价  l11=成交量
#   l5 与 l11 都恰好是官方口径的 100 倍 —— 即 l5 单位百万元(=亿元×100)、
#   l11 单位百万股(=亿股×100)。115 个指数实测：l5/(100×官方amount) 中位 1.07、
#   l11/(100×官方volume) 中位 1.11、两者与「当日放量」互相自洽。
#
# 关键限制：接口不带日期，且只给「当日」。所以快照只能用于补**最新的一个交易日**，
# 无法重建历史。补出来的行一律带 est 标记，官方全量抓取时 overwrite_est 覆盖。
SW_SNAP_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/current/"
SW_SNAP_LEVELS = ("一级行业", "二级行业")
SW_SNAP_AMT_DIV = 100.0    # -> 亿元，与官方 L0 amount 同口径
SW_SNAP_VOL_DIV = 100.0    # -> 亿股，与官方 L0 volume 同口径
# 昨收与 L0 某日收盘的一致率门槛：低于它说明快照与本地历史对不上（源改版/口径变了），
# 不做增量、退回官方全量，宁可慢也不写错。
SW_SNAP_MIN_AGREE = 0.90
# 单日涨跌幅超过该值视为异常值，剔除（防止停牌复牌/指数重构日把 panel 带偏）
SW_SNAP_MAX_MOVE = 0.25

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
def _sw_records(data: List[dict]) -> List[dict]:
    """官方 trend/ 原始 JSON -> 标准记录。字段口径与 akshare.index_hist_sw 一致。"""
    out: List[dict] = []
    for x in data:
        try:
            out.append({
                "date": str(x["bargaindate"])[:10],
                "open": float(x["openindex"]), "close": float(x["closeindex"]),
                "high": float(x["maxindex"]), "low": float(x["minindex"]),
                "volume": float(x["bargainamount"]),   # 亿股
                "amount": float(x["bargainsum"]),      # 亿元
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_sw_official(code: str, timeout: int = 90) -> Optional[List[dict]]:
    """直连官方 trend/ 取单指数全历史（纯网络，不落盘）。"""
    for attempt in range(2):
        try:
            r = requests.get(SW_TREND_URL, params={"swindexcode": code, "period": "DAY"},
                             headers={"User-Agent": _UA}, timeout=timeout, verify=False)
            recs = _sw_records((r.json() or {}).get("data") or [])
            if recs:
                return recs
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def prefetch_sw_official(codes: List[str], workers: int = SW_FETCH_WORKERS,
                         verbose: bool = False, deadline: float = 0.0) -> Dict[str, List[dict]]:
    """并行拉一批指数的官方全历史，返回 {code: records}。**只走网络、不写 L0。**

    调用方负责串行落盘：rawstore.put 是「读整份 manifest → 改 → 写回」，
    并发写会互相覆盖丢条目，因此写入必须单线程。

    deadline（epoch 秒）用于让校准有时间上界：超时未返回的标的留待下次，
    已有的 est 行保持原样（不会被写坏），verify 会继续提示它们未校准。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: Dict[str, List[dict]] = {}
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_sw_official, c): c for c in codes}
        for f in as_completed(futs):
            code = futs[f]
            try:
                recs = f.result()
            except Exception:
                recs = None
            if recs:
                out[code] = recs
            done += 1
            if verbose and done % 25 == 0:
                print(f"    并发取数 {done}/{len(codes)}（{time.time()-t0:.0f}s，成功 {len(out)}）")
            if deadline and time.time() > deadline:
                for g in futs:
                    g.cancel()
                if verbose:
                    print(f"    [warn] 触及时间上限，仅完成 {done}/{len(codes)}，其余留待下次")
                break
    if verbose:
        print(f"    并发取数完成：成功 {len(out)}/{len(codes)}，用时 {time.time()-t0:.0f}s")
    return out


def fetch_sw_daily(code: str, refresh: bool = False,
                   records: Optional[List[dict]] = None) -> Optional[List[dict]]:
    """取单指数日K。records 给定时跳过网络（并发预取的产物），只做落盘。"""
    cached = RS.get("sw_index", code)
    if cached and not refresh:
        return cached
    if records is None:
        ak = _ak()
        for attempt in range(3):
            try:
                df = ak.index_hist_sw(symbol=code, period="day")
                records = _df_records(df)
                if records:
                    break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
    if records:
        # overwrite_est=True：官方值可以覆盖"实时兜底"留下的估算行（est 标记），
        # 一手数据彼此之间仍然先到先得；这是官方出数后自动修复的唯一通道。
        RS.put("sw_index", code, records, SOURCE_SW,
               overwrite_est=True,
               note="成交量单位:股/亿股口径,成交额:亿元;仅做相对比值使用")
        return records
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
                # overwrite_est=True：基准的实时快照行也带 est，官方日线出数后必须能覆盖，
                # 否则 est 标记会永久留在 L0（verify 会一直报"未校准"）。
                RS.put("bench", market_code, recs, SOURCE_BENCH, overwrite_est=True,
                       note="新浪指数日K无成交额列, amount=0; 基准不参与量能打分")
                return recs
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return cached


# ---------------------------------------------------------------- 增量抓取
def fetch_sw_snapshot(verbose: bool = False) -> Dict[str, dict]:
    """申万 current/ 批量快照 -> {code: 原始行}。2 次请求拿全部一级+二级行业。"""
    try:
        requests.packages.urllib3.disable_warnings()
    except Exception:
        pass
    out: Dict[str, dict] = {}
    for lvl in SW_SNAP_LEVELS:
        got = 0
        for attempt in range(3):
            try:
                r = requests.get(SW_SNAP_URL,
                                 params={"page": "1", "page_size": "1000",
                                         "indextype": lvl},
                                 headers={"User-Agent": _UA}, verify=False, timeout=60)
                rows = (r.json().get("data") or {}).get("results") or []
                for x in rows:
                    if x.get("swindexcode"):
                        out[x["swindexcode"]] = x
                got = len(rows)
                break
            except Exception:
                time.sleep(1.2 * (attempt + 1))
        if verbose:
            print(f"    快照 {lvl}: {got} 条")
    return out


def _snap_bars(raw: Dict[str, dict]) -> Dict[str, dict]:
    """快照原始行 -> {code: {prev_close,open,high,low,close,volume,amount}}，已换算单位。"""
    out: Dict[str, dict] = {}
    for code, x in raw.items():
        try:
            bar = {
                "prev_close": float(x["l3"]),
                "open": float(x["l4"]),
                "high": float(x["l6"]),
                "low": float(x["l7"]),
                "close": float(x["l8"]),
                "amount": float(x["l5"]) / SW_SNAP_AMT_DIV,
                "volume": float(x["l11"]) / SW_SNAP_VOL_DIV,
            }
        except (KeyError, TypeError, ValueError):
            continue
        if min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0:
            continue
        if bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > min(bar["open"], bar["close"]):
            continue          # OHLC 自相矛盾，丢弃
        if bar["prev_close"] > 0 and abs(bar["close"] / bar["prev_close"] - 1.0) > SW_SNAP_MAX_MOVE:
            continue          # 单日异动（复牌/重构），交给官方全量
        out[code] = bar
    return out


def _market_date() -> tuple:
    """从新浪实时行情取「最近一个已收盘交易日」-> (date, closed)。

    快照接口本身不带日期，必须由外部给：否则无从知道这批数字属于哪一天。
    取 5 个基准的行情戳，全部一致才采用（任一不一致说明正在切换日界）。
    """
    codes = ",".join(BENCH_SINA[mc] for mc in C.BENCHMARKS)
    try:
        r = requests.get("https://hq.sinajs.cn/list=" + codes,
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
        r.encoding = "gbk"
    except Exception:
        return None, False
    stamps = set()
    for line in r.text.strip().split("\n"):
        if '"' not in line:
            continue
        b = line.split('"')[1].split(",")
        if len(b) < 32 or not b[30]:
            continue
        d, hhmm = b[30], b[31]
        try:
            if (int(hhmm[0:2]), int(hhmm[3:5])) < (15, 0):
                return d, False      # 未收盘，当日 bar 还不成立
        except (ValueError, IndexError):
            return None, False
        stamps.add(d)
    if len(stamps) != 1:
        return None, False
    return stamps.pop(), True


def fetch_bench_snapshot() -> Dict[str, dict]:
    """新浪实时行情 -> {market_code: bar}，只给已收盘且日期有效的那批。"""
    codes = ",".join(BENCH_SINA[mc] for mc in C.BENCHMARKS)
    try:
        r = requests.get("https://hq.sinajs.cn/list=" + codes,
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
        r.encoding = "gbk"
    except Exception:
        return {}
    sym2mc = {v: k for k, v in BENCH_SINA.items()}
    out: Dict[str, dict] = {}
    for line in r.text.strip().split("\n"):
        if '"' not in line:
            continue
        sym = line.split("=")[0].split("_")[-1].strip()
        b = line.split('"')[1].split(",")
        mc = sym2mc.get(sym)
        if not mc or len(b) < 32:
            continue
        try:
            o, c, hi, lo = float(b[1]), float(b[3]), float(b[4]), float(b[5])
            if min(o, c, hi, lo) <= 0:
                continue
            out[mc] = {"date": b[30], "open": o, "close": c, "high": hi, "low": lo,
                       # 新浪实时第 8 字段是「手」，日线 volume 是「股」——统一乘 100，
                       # 否则 est 行与官方行的量纲差 100 倍（实测 801010 系基准全中）。
                       "volume": float(b[8] or 0.0) * 100.0, "amount": 0.0, "est": True,
                       "note": "新浪实时行情快照；官方日线出数后由 overwrite_est 覆盖"}
        except (ValueError, IndexError):
            continue
    return out


def _tail_hist(code: str, n: int = 6) -> Dict[str, dict]:
    rows = RS.get("sw_index", code)
    if not rows:
        return {}
    return {r["date"]: r for r in rows[-n:]}


def apply_incremental(verbose: bool = True) -> dict:
    """把当日快照写进 L0（est 标记），让 panel 不必为追加 1 天而重下 365MB。

    返回 {"ok": bool, "reason": str, "date": ..., "written": n, ...}。
    ok=False 时调用方应退回官方全量抓取（慢但绝对可靠）。
    """
    rep = {"ok": False, "reason": "", "date": None, "written": 0,
           "skipped": 0, "agree": 0.0, "bench": 0}

    mdate, closed = _market_date()
    if not closed:
        rep["reason"] = f"未收盘或无有效行情戳（date={mdate}）"
        return rep

    raw = fetch_sw_snapshot(verbose=verbose)
    bars = _snap_bars(raw)
    if len(bars) < 50:
        rep["reason"] = f"快照条目过少（{len(bars)}），源可能异常"
        return rep

    hist = {c: _tail_hist(c) for c in bars}
    hist = {c: h for c, h in hist.items() if h}
    if len(hist) < 50:
        rep["reason"] = f"L0 已有历史过少（{len(hist)}），首建应走全量"
        return rep

    # 候选日 = 本地历史里最近的几个日期；用「快照昨收 == 该日收盘」的一致率定位快照属于哪天
    cand: Dict[str, int] = {}
    for c, h in hist.items():
        for d in sorted(h)[-3:]:
            cand[d] = cand.get(d, 0) + 1
    if not cand:
        rep["reason"] = "无候选日期"
        return rep
    scored = []
    for d in cand:
        agree = n = 0
        for c, h in hist.items():
            if d not in h:
                continue
            n += 1
            if abs(h[d]["close"] - bars[c]["prev_close"]) <= 1e-6:
                agree += 1
        if n >= 50:
            scored.append((agree / n, d, agree, n))
    if not scored:
        rep["reason"] = "候选日期可用样本不足"
        return rep
    ratio, prev, agree, n = max(scored)
    rep["agree"] = round(ratio, 4)
    if ratio < SW_SNAP_MIN_AGREE:
        rep["reason"] = f"快照昨收与本地历史对上率仅 {ratio:.1%}（<{SW_SNAP_MIN_AGREE:.0%}）"
        return rep
    if mdate <= prev:
        rep.update(ok=True, reason=f"无新交易日（快照对应 {prev}，本地已到 {mdate}）",
                   date=prev)
        return rep
    # 幂等：已经写过这一天（例如同日重跑）就不再重复落盘
    have = sum(1 for h in hist.values() if mdate in h)
    if have >= 0.9 * len(hist):
        rep.update(ok=True, date=mdate,
                   reason=f"{mdate} 已在 L0（{have}/{len(hist)} 个标的），无需增量")
        return rep

    written = skipped = 0
    for c, bar in bars.items():
        h = hist.get(c)
        if not h or prev not in h:
            skipped += 1        # 该指数本地没有 prev 这天，无法确认连续性 -> 交给全量
            continue
        row = {"date": mdate, "open": bar["open"], "close": bar["close"],
               "high": bar["high"], "low": bar["low"],
               "volume": round(bar["volume"], 8), "amount": round(bar["amount"], 8),
               "est": True,
               "note": "申万 current/ 批量快照；官方日线出数后由 overwrite_est 覆盖"}
        RS.put("sw_index", c, [row], SOURCE_SW_SNAP, overwrite_est=True,
               note="官方日线 T+1 未出时的当日快照增量（est，可被官方覆盖）")
        written += 1

    # 基准同步推进：否则环境档位缺当日收益，结论会被截回前一天
    bn = 0
    for mc, b in fetch_bench_snapshot().items():
        rows = RS.get("bench", mc) or []
        if not rows:
            continue
        if b["date"] <= rows[-1]["date"]:
            continue
        RS.put("bench", mc, [{k: b[k] for k in
                             ("date", "open", "close", "high", "low", "volume", "amount")}
                            | {"est": True}], SOURCE_BENCH_SNAP, overwrite_est=True,
               note="新浪实时行情快照（est），官方日线出数后覆盖")
        bn += 1

    rep.update(ok=written > 0, written=written, skipped=skipped, date=mdate,
               prev=prev, bench=bn,
               reason=f"{prev} -> {mdate}，写入 {written} 个指数（{skipped} 个跳过）+ {bn} 个基准")
    return rep


def _fetch_etf_range(secid: str, bars: int) -> Optional[List[dict]]:
    """东财日K -> 最近 bars 根 bar。

    注意：东财对密集请求非常敏感（实测短时间 20 余次即被断连，13 分钟未恢复），
    因此它只是**兜底**路径，默认走腾讯批量快照（fetch_etf_snapshot_batch）。
    """
    if EM_BREAKER.open:
        return None
    params = {
        "secid": secid, "klt": "101", "fqt": "0",   # 日线、不复权
        "lmt": str(bars), "end": "20500101", "ut": EM_UT,
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",    # 日期,开,收,高,低,量,额
    }
    # 只重试 2 次：失败通常是对端限流断连，重试不会自愈，快速交给熔断更划算
    for attempt in range(2):
        try:
            r = requests.get(EM_KLINE_URL, params=params, timeout=20,
                             headers={"User-Agent": _UA})
            d = (r.json() or {}).get("data") or {}
            out: List[dict] = []
            for k in (d.get("klines") or []):
                p = k.split(",")
                if len(p) < 7:
                    continue
                out.append({
                    "date": p[0][:10],
                    "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "volume": float(p[5]) * 100,  # 东财「手」-> 新浪「股」口径
                    "amount": float(p[6]),
                })
            if out:
                EM_BREAKER.reset()
                return out
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    EM_BREAKER.hit()
    return None


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_BATCH = 40      # 单次批量请求的代码数（实测 40 个约 22KB）


def fetch_etf_snapshot_batch(secids: List[str]) -> Dict[str, dict]:
    """腾讯批量实时行情 -> {secid: 当日完整 bar}。这是 ETF 日常增量的主路径。

    为什么用它：一次请求覆盖 40 个标的、约 22KB，直接给出完整 OHLC + 成交量
    （手）+ 成交额（万元），不依赖任何需要重下全历史的接口。实测连续请求稳定，
    不像东财那样一密集就断连。

    口径：成交量 ×100 对齐新浪「股」；成交额 ×10000 转「元」。两者已与东财/新浪
    同日数据核对一致。
    只对**收盘后**的调用有意义——盘中调用拿到的是实时价，会把当日 bar 写错，
    所以这里用日期 + 时间戳双重校验，未收盘（时间戳早于 15:00）的标的直接丢弃。
    """
    out: Dict[str, dict] = {}
    for i in range(0, len(secids), TENCENT_BATCH):
        chunk = secids[i:i + TENCENT_BATCH]
        qs = ",".join(("sh" if s.startswith("1.") else "sz") + s.split(".")[-1]
                      for s in chunk)
        try:
            r = requests.get(TENCENT_QUOTE_URL + qs, timeout=20,
                             headers={"User-Agent": _UA,
                                      "Referer": "https://gu.qq.com/"})
            r.encoding = "gbk"
            for line in r.text.split(";"):
                if "=" not in line or '"' not in line:
                    continue
                body = line.split('"')[1].split("~")
                if len(body) < 38:
                    continue
                # 行首形如 v_sh510300=...；body[2] 是 6 位代码（510300），
                # 市场前缀必须从行首取，不能靠代码首位猜。
                head = line.split("=")[0].strip()
                sym6 = head.split("_")[-1]              # sh510300 / sz159915
                code, stamp = body[2], body[30]         # 代码、yyyymmddHHMMSS
                if len(stamp) < 14 or not sym6.startswith(("sh", "sz")):
                    continue
                d = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
                hh, mm = int(stamp[8:10]), int(stamp[10:12])
                if (hh, mm) < (15, 0):                 # 未收盘，当日 bar 还不成立
                    continue
                sid = ("1." if sym6.startswith("sh") else "0.") + code
                try:
                    out[sid] = {
                        "date": d,
                        "open": float(body[5]), "close": float(body[3]),
                        "high": float(body[33]), "low": float(body[34]),
                        "volume": float(body[6]) * 100,     # 手 -> 股
                        "amount": float(body[37]) * 10000,  # 万元 -> 元
                    }
                except (ValueError, IndexError):
                    continue
        except Exception:
            pass
        time.sleep(0.25)
    return out


def fetch_etf_daily(secid: str, refresh: bool = False,
                    recent_bars: Optional[int] = None,
                    snapshot: Optional[dict] = None) -> Optional[List[dict]]:
    """东财 secid（1.512400 / 0.159915）-> ETF 日K，不复权，amount 单位: 元。

    增量优先级（都是"只补新交易日"，不重下历史）：
      1. snapshot：腾讯批量快照，当日完整 bar（含成交额），一次请求覆盖 40 个标的；
      2. recent_bars：东财取最近 N 根（兜底，仅当快照不可用且缺口较大时）；
      3. 新浪全历史：首次建档 / 上面都不通时的最终回退，行为与改造前一致。

    所有路径都走 RS.put 的 merge-append：已存在日期不会被改动（write-once 不变）。
    """
    cached = RS.get("etf", secid)
    if cached and not refresh:
        return cached

    # 1) 批量快照：只有当快照日期确实比 L0 末日期新，且中间没有断档时才采用
    if cached and refresh and snapshot:
        last, d = cached[-1]["date"], snapshot.get("date")
        if d and d > last:
            from datetime import date as _date
            try:
                gap = (_date.fromisoformat(d) - _date.fromisoformat(last)).days
            except Exception:
                gap = 999
            if gap <= 7:
                RS.put("etf", secid, [snapshot], SOURCE_ETF_TX, overwrite_est=True,
                       note="不复权;amount单位:元;volume已对齐新浪口径(股);"
                            "腾讯批量快照增量，merge-append 不改历史")
                return RS.get("etf", secid) or [snapshot]
            # gap > 7：漏跑多日，单根快照会造成断档，往下走多根/全量路径

    # 2) 东财最近 N 根（熔断打开时直接返回 None，零开销跳过）
    if cached and refresh and recent_bars:
        inc = _fetch_etf_range(secid, recent_bars)
        if inc:
            RS.put("etf", secid, inc, SOURCE_ETF_EM, overwrite_est=True,
                   note=f"不复权;amount单位:元;volume已对齐新浪口径(股);"
                        f"东财增量 lmt={recent_bars}，merge-append 不改历史")
            return RS.get("etf", secid) or inc

    # 3) 新浪全历史（首次建档 / 回退）
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


def fill_interior_holes(series: Dict[str, Dict[str, dict]],
                        bench_series: Dict[str, Dict[str, dict]],
                        meta: Dict[str, dict], verbose: bool = True) -> Optional[dict]:
    """补齐判断层的**整日空洞**（与「尾部滞后」不同的一类缺陷）。

    判据：基准（新浪，最全）该日是交易日，而**没有任何一个 L1 指数**有 bar。
    这类洞不会自愈——官方出完后面的日期后 `official_last` 已经越过它，
    `fill_realtime_tail` 只看尾部，永远不会回头补这一根。于是系统整天跳过：
    没有 day 文件、没有结论、交易日历上一格空白，结算层的到期日还少算一天。

    补法与尾部兜底同一口径（`_fill_by_etf_proxy`）：**只补收益、不发明点位**——
    行业指数当日收盘 = 该指数上一个官方收盘 × (1 + 对应 ETF 当日收益)；
    没有对应 ETF 的行业用当日被代理行业的横截面中位收益。
    逐行打 `est`，L0 用 overwrite_est=True 写入，官方哪天补发就自动被真值覆盖。

    对齐体检（逐日）：被代理行业当日中位涨跌 vs 基准当日涨跌，相差 > 5% 视为口径不对，
    整天放弃——与尾部兜底同一道闸。
    """
    l1 = [c for c, m in meta.items() if m.get("level") == "L1" and c in series]
    if not l1:
        return None
    all_dates = sorted({d for c in l1 for d in series[c]})
    if len(all_dates) < 2:
        return None
    lo, hi = all_dates[0], all_dates[-1]
    bench_dates = sorted({d for s in bench_series.values() for d in s})
    covered = set(all_dates)
    holes = [d for d in bench_dates if lo <= d <= hi and d not in covered]
    # 历史补出来的日子（L0 里带 src=etf_proxy_gap 的行）——即便这一轮没有新洞，
    # 也要继续往面板里报，否则前端那条「数据含代理日」的提示会在下一次 daily 后消失，
    # 而那一天其实仍是估算值。官方真值覆盖后这些行不带 src，自然就消失了。
    already = sorted({d for c in l1 + [c for c, m in meta.items() if m.get("level") == "L2"]
                      for d, r in (series.get(c) or {}).items()
                      if r.get("src") == "etf_proxy_gap"})
    # 只补**已授权**的日期（config.GAP_FILL_DATES）：补一根历史 bar 会改变回测，
    # 必须逐个批准。未授权的缺口继续按「已登记空洞」处理，系统整天跳过。
    allowed = set(getattr(C, "GAP_FILL_DATES", ()) or ())
    skipped = [d for d in holes if d not in allowed]
    holes = [d for d in holes if d in allowed]
    if verbose and skipped:
        print(f"  [gapfill] 未授权，保持跳过：{skipped}（授权补：config.GAP_FILL_DATES）")
    if not holes and not already:
        return None
    if not holes:      # 没有新洞，但历史代理日仍在：只回报，不重复写 L0
        rep = {"method": "etf_proxy_gap",
               "days": {d: {"close": "etf_proxy_gap(ETF 日收益前推)",
                            "volume": "est(沪深300 量比)", "ohlc": "est(=收盘)",
                            "method": "etf_proxy_gap"} for d in already},
               "codes": 0, "rows": 0, "rows_etf": 0, "rows_xs": 0,
               "source": "新浪 ETF 日线（data/raw/etf）"}
        if verbose:
            print(f"  [gapfill] 沿用已补的代理日：{'、'.join(already)}（L0 里已带 est 标记）")
        return rep
    try:
        import json as _json
        ep = _json.load(open(f"{C.CACHE_DIR}/etf_panel.json", encoding="utf-8"))
        sw_to_etf = ep.get("sw_to_etf") or {}
    except Exception:
        sw_to_etf = {}
    if not sw_to_etf:
        if verbose:
            print("  [gapfill] 缺 etf_panel 的行业→ETF 映射，放弃补整日缺口")
        return None

    filled: Dict[str, List[dict]] = {}
    n_etf = n_xs = 0
    for day in holes:
        # 当天各行业代理收益（先算有对应 ETF 的）
        prox: Dict[str, float] = {}
        for code, s in series.items():
            if meta.get(code, {}).get("level") not in ("L1", "L2") or day in s:
                continue
            prev = max((d for d in s if d < day), default=None)
            if prev is None:
                continue
            r = _etf_proxy_returns(sw_to_etf, meta[code]["name"], [day], verbose=False)
            if r and day in r:
                prox[code] = r[day][0]
        if not prox:
            if verbose:
                print(f"  [gapfill] {day} 无任何行业可取到 ETF 代理收益，跳过该日")
            continue
        med = sorted(prox.values())[len(prox) // 2]

        # 对齐体检（先做，避免污染 filled）
        st = bench_series.get("1.000300") or bench_series.get("1.000001") or {}
        bd = sorted(st)
        bprev = max((d for d in bd if d < day), default=None)
        if bprev and st.get(day):
            bench_ret = float(st[day]["close"]) / float(st[bprev]["close"]) - 1.0
            if abs(med - bench_ret) > 0.05:
                if verbose:
                    print(f"  [gapfill] {day} 对齐体检不通过：代理中位 {med:+.2%} vs "
                          f"基准 {bench_ret:+.2%} → 放弃该日")
                continue

        for code, s in series.items():
            if meta.get(code, {}).get("level") not in ("L1", "L2") or day in s:
                continue
            prev = max((d for d in s if d < day), default=None)
            if prev is None:
                continue
            ret = prox.get(code, med)
            clamped = abs(ret) > 0.11          # 份额折算/数据错 → 夹住并标记
            ret = max(-0.11, min(0.11, ret))
            base = s[prev]
            b0 = float(base["close"])
            cur = b0 * (1.0 + ret)
            v0, a0 = float(base.get("volume") or 0.0), float(base.get("amount") or 0.0)
            ratio = _bench_vol_ratio(bench_series, prev, day) or 1.0
            est = ["gap_close_proxy" if code in prox else "gap_close_xs_median",
                   "open", "high", "low", "volume", "amount"]
            if clamped:
                est.append("clamped_11pct")
            filled.setdefault(code, []).append(
                {"date": day, "open": cur, "high": cur, "low": cur, "close": cur,
                 "volume": v0 * ratio, "amount": a0 * ratio,
                 "est": est, "src": "etf_proxy_gap"})
            if code in prox:
                n_etf += 1
            else:
                n_xs += 1

    if not filled:
        return None
    for code, rows in filled.items():
        RS.put("sw_index", code, rows, SOURCE_SW_RT, merge=True, overwrite_est=True,
               note="申万整日缺数据时用 ETF 收益代理补缺（见 docs/数据缺口-判断层整日缺失.md）")
        for r in rows:
            series[code][r["date"]] = r
        series[code] = dict(sorted(series[code].items()))
    report = {
        "method": "etf_proxy_gap",
        "days": {d: {"close": "etf_proxy_gap(ETF 日收益前推)", "volume": "est(沪深300 量比)",
                     "ohlc": "est(=收盘)", "method": "etf_proxy_gap"}
                 for d in sorted(set(holes) | set(already))},
        "codes": len(filled), "rows": sum(len(v) for v in filled.values()),
        "rows_etf": n_etf, "rows_xs": n_xs,
        "source": "新浪 ETF 日线（data/raw/etf）",
    }
    if verbose:
        print(f"  [gapfill] 判断层整日缺口 {len(holes)} 天：{'、'.join(holes)}；"
              f"用 {len(filled)} 个行业补齐（ETF 代理 {n_etf} 行 / 中位 {n_xs} 行）")
        print("           只补收益不发明点位，逐行标 est；官方补发后 daily 自动用真值覆盖")
    return report


# ---------------------------------------------------------------- 面板
def build_panel(refresh: bool = False, full: bool = False, verbose: bool = True) -> dict:
    """从 L0（必要时先抓）组装 panel.json，结构与东财版完全同构。

    refresh=True 且 full=False：先走「批量快照增量」（2~3 次请求、秒级），把当日
    bar 以 est 标记写进 L0，再离线组装 panel —— 这是日常路径。
    full=True：官方日线全量重下（每只必返 20 年全历史），用 overwrite_est 把此前的
    估算行换成官方真值 —— 这是校准路径。取数改为 8 路并发（串行时约 20~40 分钟），
    落盘仍串行。增量失败（源改版、对不上历史等）时自动退回全量，不会静默少一天。
    """
    incr = None
    if refresh and not full:
        if verbose:
            print("  增量抓取：申万批量快照（current/）...")
        incr = apply_incremental(verbose=verbose)
        if verbose:
            print(f"    {'✓' if incr['ok'] else '✗'} {incr['reason']}")
        if not incr["ok"] and "无新交易日" not in incr["reason"]:
            print("  [warn] 增量不可用，退回官方全量重下（慢）...")
            full = True

    catalog = fetch_catalog(refresh=refresh)
    universe = build_universe(catalog)
    if verbose:
        n_l1 = sum(1 for u in universe.values() if u["level"] == "L1")
        print(f"  行业宇宙：L1={n_l1} + L2/L3佐证={len(universe) - n_l1}")

    series: Dict[str, Dict[str, dict]] = {}
    meta: Dict[str, dict] = {}
    missing: List[str] = []
    # 校准路径（full=True）：官方 trend/ 只能整段取，120 只串行是 20~40 分钟。
    # 先并行把全历史取回内存（只走网络），再按 universe 顺序串行落盘 —— 落盘必须
    # 单线程，因为 rawstore.put 是「读整份 manifest → 改 → 写回」。
    prefetched: Dict[str, List[dict]] = {}
    if full and universe:
        if verbose:
            print(f"  官方全量：并行 {SW_FETCH_WORKERS} 路拉取 {len(universe)} 个指数全历史...")
        prefetched = prefetch_sw_official(list(universe.keys()), verbose=verbose)
    # 增量只覆盖一级/二级行业；三级行业（85xxxx）快照里没有，会落后一天。
    # 对这类「落后于增量日」的标的做定向官方补抓（单只约 10s，通常 3~5 只），
    # 而不是为了它们把 120 只全部重下。
    target = incr["date"] if (incr and incr.get("ok") and incr.get("date")) else None
    patched: List[str] = []
    lagging: List[str] = []      # 快照未覆盖且官方当日也没出（通常是三级行业）
    official_dead = False        # 试过一次官方补抓仍不达标，就不再为每个标的各等 10s
    for i, (code, u) in enumerate(universe.items()):
        recs = fetch_sw_daily(code, refresh=full, records=prefetched.get(code))
        if target and recs and recs[-1]["date"] < target:
            if official_dead:
                lagging.append(code)
            else:
                fresh = fetch_sw_daily(code, refresh=True)
                if fresh and fresh[-1]["date"] >= target:
                    recs = fresh
                    patched.append(code)
                else:
                    official_dead = True
                    lagging.append(code)
        if not recs:
            missing.append(f"{code} {u['name']}")
            continue
        series[code] = {r["date"]: r for r in recs}
        meta[code] = {"name": u["name"], "level": u["level"]}
        if verbose and (i + 1) % 25 == 0:
            print(f"  指数进度 {i+1}/{len(universe)}")
        # 全量路径的网络请求已在 prefetch 里并行完成，这里只剩本地落盘，无需节流

    bench_series = {}
    for mc, name in C.BENCHMARKS.items():
        recs = fetch_bench_daily(mc, refresh=full)
        if recs:
            bench_series[mc] = {r["date"]: r for r in recs}
            meta[mc] = {"name": name, "level": "BENCH"}
    series.update(bench_series)
    if verbose and patched:
        print(f"  [info] 定向补抓 {len(patched)} 个快照未覆盖的标的: {patched[:6]}")
    if verbose and lagging:
        print(f"  [info] {len(lagging)} 个标的当日无数据（快照未覆盖且官方未出，"
              f"多为三级行业）: {lagging[:6]}")
    if verbose and missing:
        print(f"  [warn] 抓取失败 {len(missing)} 个: {missing[:8]}")

    # 官方申万日线滞后于基准（新浪）时补尾部：≤2 天用实时行情、更长用 ETF 收益代理
    # （估算字段逐行带 est 标记，官方出数后 L0 overwrite_est 自动覆盖）
    rt_fill = fill_realtime_tail(series, bench_series, meta, verbose=verbose)
    # 另一类缺陷：官方**整日**缺数据（当天 120 个指数全无 bar，而 ETF/基准正常交易）。
    # 它不会被上面那条尾部兜底捡到（official_last 已经越过它），必须单独补，且要在
    # 下面的交易日轴之前补——否则这一天根本进不了日历。
    gap_fill = fill_interior_holes(series, bench_series, meta, verbose=verbose)

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
        "gap_fill": gap_fill,
        "incremental": incr,
    }
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(f"{C.CACHE_DIR}/panel.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    if verbose:
        print(f"  面板完成：{len(series)} 个标的，{len(dates)} 个交易日 "
              f"({dates[0]} -> {dates[-1]})")
    return out


if __name__ == "__main__":
    build_panel(refresh="--refresh" in sys.argv, full="--full" in sys.argv)
