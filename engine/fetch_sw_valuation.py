# -*- coding: utf-8 -*-
"""申万官方「指数分析」日度数据抓取 —— 价格之外的第一手信息源。

为什么单独建这个抓取器（而不是直接调 akshare）
--------------------------------------------
akshare 的 `index_analysis_daily_sw` 与本用途有两个硬冲突：
  1. `page_size` 硬编码为 50、`swindexcode` 硬编码为 "all"。全历史约
     16 年 × 155 个申万指数 ≈ 60 万行，按 50/页要 1.2 万次请求，不可行。
     本模块直接调 HTTP，把 page_size 调大（并自动探测服务端上限）。
  2. 需要「先探针、后抓取」：在花掉抓取预算之前，先确认历史深度、分页上限
     与**日期口径**（见下）。

数据语义（逐日 × 逐申万指数）
----------------------------
    收盘指数 closeindex      涨跌幅 markup        换手率 turnoverrate
    市盈率   pe              市净率 pb            股息率 dp
    均价     meanprice       成交额占比 bargainsumrate
    流通市值 negotiablessharesum1     平均流通市值 negotiablessharesum2

其中 **pe / pb / dp 是价格之外的信息**（盈利与净资产的定价），
turnoverrate / bargainsumrate / 流通市值 也不由收盘价单独决定。

探针要回答的三件事（不靠推断，实测）
----------------------------------
  Q1 历史深度：该接口最早能回溯到哪一天？
  Q2 分页上限：page_size 调到多大才会被服务端截断？
  Q3 日期口径：`bargaindate` 是「数据所属交易日」还是「报告发布日」？
     以及分析表的 `收盘指数` 是否等于 `index_hist_sw` 同日 `收盘`。
     —— 日期错位会直接变成前视污染，必须实测而非假定。

落盘（L0，write-once + merge-append）
------------------------------------
    data/raw/sw_valuation/<申万代码>.json
    [{date, close, markup, turnover, pe, pb, dp, meanprice, sumrate,
      float_mv, avg_mv}, ...]
新增 kind 对现有链路**完全惰性**：datasource_sw.build_panel 只读
sw_index / bench / etf / catalog，不认识 sw_valuation。

用法
----
    python engine/fetch_sw_valuation.py --probe                  # 只探针，不落盘
    python engine/fetch_sw_valuation.py --since 2010-01-01       # 全量抓取
    python engine/fetch_sw_valuation.py --since 2024-01-01 --index-type 二级行业
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import rawstore as RS  # noqa: E402

API = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
SOURCE = "swsresearch.institute-sw/api/index_analysis(申万宏源官方指数分析)"
NOTE = ("逐日×逐申万的估值/换手/市值面板；pe/pb/dp 取自申万官方口径，"
        "不作复权或平滑处理")

# index_type 可选值（与 akshare 文档一致）；三级行业不在此接口支持范围内
INDEX_TYPES = ("一级行业", "二级行业")

# 服务端字段 -> 本地字段
COLS = {
    "swindexcode": "code",
    "swindexname": "name",
    "bargaindate": "date",
    "closeindex": "close",
    "markup": "markup",
    "turnoverrate": "turnover",
    "pe": "pe",
    "pb": "pb",
    "dp": "dp",
    "meanprice": "meanprice",
    "bargainsumrate": "sumrate",
    "negotiablessharesum1": "float_mv",
    "negotiablessharesum2": "avg_mv",
}
NUM_FIELDS = ("close", "markup", "turnover", "pe", "pb", "dp",
              "meanprice", "sumrate", "float_mv", "avg_mv")

# 上一次 fetch() 里抓失败的页（{chunk, page, err}），供 main 写入报告
_LAST_FAILS: List[dict] = []

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False           # 与日更一致：NO_PROXY='*' 等效，直连国内站点
    s.headers.update({"User-Agent": UA, "Referer":
                      "https://www.swsresearch.com/institute_sw/allIndex/analysisIndex"})
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
    return s


def _get_page(sess: requests.Session, index_type: str, start: str, end: str,
              page: int, page_size: int, retries: int = 3,
              strict: bool = False) -> Tuple[int, List[dict]]:
    """取一页。`strict=True` 时重试耗尽会抛异常。

    为什么需要 strict：抓取循环把「返回 0 行」当作「这一段的页已经翻完」。
    若网络抖动被吞成 0 行，那一段后面的页会被**静默丢弃**、且看不出来。
    所以抓取路径必须用 strict，让失败显式暴露。
    """
    params = {
        "page": str(page), "page_size": str(page_size), "index_type": index_type,
        "start_date": start, "end_date": end, "type": "DAY", "swindexcode": "all",
    }
    last = None
    for k in range(retries):
        try:
            r = sess.get(API, params=params, timeout=40, verify=False)
            j = r.json()
            d = j.get("data") or {}
            return int(d.get("count") or 0), list(d.get("results") or [])
        except Exception as e:            # noqa: BLE001
            last = e
            time.sleep(1.5 * (k + 1))
    if strict:
        raise RuntimeError(f"请求失败 {start}~{end} page={page}: {last}")
    print(f"    [warn] 请求失败 page={page}: {last}")
    return 0, []


def _norm(row: dict) -> Optional[dict]:
    out = {}
    code = row.get("swindexcode")
    if not code:
        return None
    out["code"] = str(code)
    out["name"] = str(row.get("swindexname") or "")
    d = row.get("bargaindate")
    if not d:
        return None
    out["date"] = str(d)[:10]
    for src, dst in COLS.items():
        if dst in ("code", "name", "date"):
            continue
        v = row.get(src)
        try:
            out[dst] = float(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            out[dst] = None
    return out


def _chunks(since: str, until: str, days: int = 120) -> List[Tuple[str, str]]:
    """把日期区间切成若干段（默认 ~120 天），避免单次查询行数过大。"""
    a = dt.date.fromisoformat(since)
    b = dt.date.fromisoformat(until)
    out = []
    cur = a
    while cur <= b:
        nxt = min(cur + dt.timedelta(days=days - 1), b)
        out.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt + dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ 探针
def probe(sess: requests.Session, index_type: str = "一级行业",
          probe_from: str = "2005-01-01", probe_to: Optional[str] = None) -> dict:
    to = probe_to or dt.date.today().isoformat()
    rep: Dict[str, object] = {"index_type": index_type, "range": [probe_from, to]}

    print(f"[Q1/Q2] 单次大区间探针 {probe_from} → {to}（page_size 试探）")
    for ps in (50, 200, 1000, 5000, 20000):
        cnt, rows = _get_page(sess, index_type, probe_from, to, 1, ps)
        if not rows:
            print(f"        page_size={ps:<6} count={cnt:<8} 返回 0 行")
            continue
        print(f"        page_size={ps:<6} count={cnt:<8} 首页返回 {len(rows):>6} 行"
              f"  <- 有效页大小 = {min(ps, len(rows))}")
        if len(rows) < ps:
            rep["page_size_effective"] = len(rows)
            rep["count_full_range"] = cnt
            break
    else:
        rep["page_size_effective"] = 20000

    print("[Q1] 历史深度：逐年探测最早有数据的月份")
    first_date = None
    for y in range(2004, dt.date.today().year + 1):
        a, b = f"{y}-01-01", f"{y}-01-31"
        cnt, rows = _get_page(sess, index_type, a, b, 1, 200)
        if rows:
            dates = sorted(str(r.get("bargaindate"))[:10] for r in rows if r.get("bargaindate"))
            first_date = dates[0]
            print(f"        {y} 年 1 月：count={cnt}，返回 {len(rows)} 行，最早 {first_date}")
            break
    rep["first_available"] = first_date

    cnt, rows = _get_page(sess, index_type, to, to, 1, 200)
    last_rows = [n for n in (_norm(r) for r in rows) if n]
    last_rows.sort(key=lambda x: x["date"])
    rep["today_rows"] = len(last_rows)
    print(f"[Q1] 今天 {to}：返回 {len(last_rows)} 行"
          + ("" if last_rows else "（官方当日未出数，收盘后才有，属正常）"))
    if last_rows:
        s = last_rows[0]
        print(f"        样例 {s['code']} {s['name']} 收盘={s['close']} pe={s['pe']} "
              f"pb={s['pb']} dp={s['dp']} 换手={s['turnover']}")

    # Q3 日期口径：必须用**历史**窗口（今天的数往往还没出），而且要同时比
    # 「同日」与「前一日」——只有这样才能区分「同日对齐」与「分析表滞后一日」。
    print("[Q3] 日期口径校验：分析表 close vs index_hist_sw 同日 / 前一日 close")
    a, b = "2024-06-03", "2024-06-07"
    rows = []
    for page in range(1, 21):
        _c, more = _get_page(sess, index_type, a, b, page, 200)
        if not more:
            break
        rows += more
        if len(more) < 200:
            break
    recs_all = [x for x in (_norm(r) for r in rows) if x]
    rep["align_window"] = [a, b]
    rep["align_rows"] = len(recs_all)
    n0 = n1 = tot = 0
    bad, lag1 = [], []
    for n in recs_all:
        if n["close"] is None:
            continue
        hist = RS.get("sw_index", n["code"])
        if not hist:
            continue
        ds = [str(r["date"]) for r in hist]
        by_d = {str(r["date"]): float(r["close"]) for r in hist}
        c_same = by_d.get(n["date"])
        prev = ds[ds.index(n["date"]) - 1] if n["date"] in by_d and ds.index(n["date"]) > 0 else None
        c_prev = by_d.get(prev) if prev else None
        if c_same is not None:
            tot += 1
            if abs(c_same - n["close"]) < 1e-6:
                n0 += 1
            else:
                bad.append((n["code"], n["date"], round(n["close"], 4), round(c_same, 4)))
        if c_prev is not None and abs(c_prev - n["close"]) < 1e-6:
            n1 += 1
            lag1.append(n["code"])
    rep["date_align"] = {"n_pairs": tot, "n_exact_same_day": n0,
                         "n_match_prev_day": n1, "n_lag1_codes": len(set(lag1))}
    print(f"        窗口 {a}~{b}：可比 {tot} 个（行业×日）")
    print(f"        与**同日**逐值相同 {n0}/{tot}；与前一日相同 {n1}/{tot}")
    if bad:
        print(f"        同日不等的样例 (code, date, 分析表close, 日线同日close): {bad[:4]}")
    if tot:
        if n0 >= tot * 0.9:
            print("        → 结论：bargaindate = 交易日，收盘口径一致 ✓（无前视风险）")
        elif n1 >= tot * 0.5:
            print("        → 结论：分析表收盘 ≈ 日线**前一日** ⇒ 数据本身滞后一日，"
                  "按 T-1 使用即可，不是前视")
        else:
            print("        → 结论：既非同值也非滞后一日，需人工排查")
    return rep


# ------------------------------------------------------------------ 抓取
def fetch(sess: requests.Session, index_type: str, since: str, until: str,
          page_size: int, verbose: bool = True, flush_cb=None,
          flush_every: int = 6) -> Dict[str, List[dict]]:
    """按时间分段 + 分页抓取，返回 {code: [row, ...]}。

    `flush_cb` 每 `flush_every` 段被调用一次，收到「自上次 flush 以来新抓到的行」
    （仍按 {code: [rows]} 形式）—— 目的是让**任意时刻中断都不丢已抓到的数据**：
    本机在内存吃紧时会 fork 失败，长任务必须能断点续跑。
    """
    acc: Dict[str, Dict[str, dict]] = {}
    pending: Dict[str, Dict[str, dict]] = {}
    chunks = _chunks(since, until)
    n_req = 0
    fails: List[dict] = []
    for ci, (a, b) in enumerate(chunks):
        page = 1
        while True:
            try:
                cnt, rows = _get_page(sess, index_type, a, b, page, page_size,
                                      strict=True)
            except Exception as e:                    # noqa: BLE001
                fails.append({"chunk": [a, b], "page": page, "err": str(e)})
                print(f"    [err] {a}~{b} page={page} 重试耗尽 → 该页起放弃本段：{e}",
                      flush=True)
                break
            n_req += 1
            if not rows:
                break
            for raw in rows:
                n = _norm(raw)
                if n:
                    acc.setdefault(n["code"], {})[n["date"]] = n
                    pending.setdefault(n["code"], {})[n["date"]] = n
            if len(rows) < page_size:
                break
            page += 1
            if page > 400:
                print(f"    [warn] {a}~{b} 分页超过 400，强制停止")
                break
            time.sleep(0.1)
        if flush_cb and pending and (ci + 1) % flush_every == 0:
            flush_cb({c: sorted(v.values(), key=lambda r: r["date"])
                      for c, v in pending.items()})
            print(f"    [flush] {ci + 1}/{len(chunks)} 段，落 L0 "
                  f"{sum(len(v) for v in pending.values())} 行", flush=True)
            pending = {}
        if verbose and (ci + 1) % 10 == 0:
            print(f"    进度 {ci+1}/{len(chunks)} 段（{a}~{b}），"
                  f"累计 {len(acc)} 个指数 / {sum(len(v) for v in acc.values())} 行，"
                  f"已发 {n_req} 次请求", flush=True)
        time.sleep(0.1)
    if flush_cb and pending:
        flush_cb({c: sorted(v.values(), key=lambda r: r["date"])
                  for c, v in pending.items()})
        print(f"    [flush] 尾段落 L0 {sum(len(v) for v in pending.values())} 行", flush=True)
    if fails:
        print(f"    [warn] {index_type} 有 {len(fails)} 页抓取失败（已记入报告，需重跑补齐）",
              flush=True)
    print(f"    {index_type} 抓取完成：{len(acc)} 个指数，"
          f"{sum(len(v) for v in acc.values())} 行，共 {n_req} 次请求")
    global _LAST_FAILS
    _LAST_FAILS = fails
    return {c: sorted(v.values(), key=lambda r: r["date"]) for c, v in acc.items()}


def bench(sess: requests.Session, index_type: str, n: int = 10,
          window: Tuple[str, str] = ("2024-06-03", "2024-06-07"),
          page_size: int = 200) -> dict:
    """对同一窗口顺序打 N 次请求，测真实「秒/请求」——决定全量抓取是否可行。

    本机内存吃紧时会 fork 失败、进程被换出，网络本身不是瓶颈，
    所以必须先测「这台机器上」的吞吐，而不是照搬常规经验值。
    """
    print(f"[bench] {index_type}：{n} 次顺序请求，窗口 {window[0]}~{window[1]}，"
          f"page_size={page_size}", flush=True)
    ts: List[float] = []
    for k in range(n):
        t0 = time.time()
        cnt, rows = _get_page(sess, index_type, window[0], window[1], 1, page_size)
        dt_ = time.time() - t0
        ts.append(dt_)
        print(f"    #{k + 1:<3} {dt_:6.2f}s  count={cnt}  rows={len(rows)}", flush=True)
        time.sleep(0.2)
    avg = sum(ts) / len(ts)
    med = sorted(ts)[len(ts) // 2]
    rep = {"n": n, "avg_s": round(avg, 3), "median_s": round(med, 3),
           "min_s": round(min(ts), 3), "max_s": round(max(ts), 3)}
    print(f"    平均 {avg:.2f}s/请求，中位 {med:.2f}s，最快 {min(ts):.2f}s，"
          f"最慢 {max(ts):.2f}s")
    for label, total in (("全量 2012→今 一级+二级 ≈1750 次", 1750),
                         ("仅二级 ≈1400 次", 1400),
                         ("仅 2016→今 二级 ≈1200 次", 1200)):
        print(f"    若 {label} ⇒ 预计 {avg * total / 3600:.1f} 小时")
    return rep


def save_l0(acc: Dict[str, List[dict]], verbose: bool = True) -> dict:
    meta_out = {}
    for code, rows in acc.items():
        recs = []
        for r in rows:
            rec = {"date": r["date"]}
            for f in NUM_FIELDS:
                rec[f] = r.get(f)
            recs.append(rec)
        RS.put("sw_valuation", code, recs, SOURCE, merge=True, note=NOTE)
        meta_out[code] = {"name": rows[0].get("name", ""), "rows": len(recs),
                          "first": recs[0]["date"], "last": recs[-1]["date"]}
    if verbose and meta_out:
        firsts = [v["first"] for v in meta_out.values()]
        lasts = [v["last"] for v in meta_out.values()]
        print(f"  L0 落盘：{len(meta_out)} 个文件，覆盖 {min(firsts)} → {max(lasts)}")
    return meta_out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只探针，不抓取不落盘")
    ap.add_argument("--since", default="2012-01-01",
                    help="默认 2012 起：回测窗口锚定 2016，3 年分位需要再往前 750 个交易日")
    ap.add_argument("--until", default=None)
    ap.add_argument("--index-type", default=None, help="默认抓 一级行业 + 二级行业")
    ap.add_argument("--page-size", type=int, default=200,
                    help="实测服务端上限≈200（再大就读超时），勿轻易调高")
    ap.add_argument("--bench", type=int, default=0,
                    help="只对固定窗口打 N 次请求测「秒/请求」，不落盘")
    args = ap.parse_args()

    until = args.until or dt.date.today().isoformat()
    types = (args.index_type,) if args.index_type else INDEX_TYPES
    sess = _session()

    if args.bench:
        rep = {"benched_at": dt.datetime.now().isoformat(timespec="seconds"),
               "results": {}}
        for it in types:
            rep["results"][it] = bench(sess, it, args.bench, page_size=args.page_size)
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        p = f"{C.CACHE_DIR}/sw_valuation_bench.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"测速报告：{p}")
        return

    if args.probe:
        rep = {"probed_at": dt.datetime.now().isoformat(timespec="seconds"), "results": {}}
        for it in types:
            print("=" * 84)
            print(f"探针：{it}")
            try:
                rep["results"][it] = probe(sess, it, probe_to=until)
            except Exception as e:            # noqa: BLE001
                print(f"  探针失败：{e}")
                rep["results"][it] = {"error": str(e)}
            print()
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        p = f"{C.CACHE_DIR}/sw_valuation_probe.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"探针报告已落盘：{p}")
        return

    all_acc: Dict[str, List[dict]] = {}
    fails_all: Dict[str, List[dict]] = {}
    for it in types:
        print(f"抓取 {it}：{args.since} → {until}（page_size={args.page_size}）", flush=True)
        acc = fetch(sess, it, args.since, until, args.page_size,
                    flush_cb=lambda d: save_l0(d, verbose=False))
        fails_all[it] = list(_LAST_FAILS)
        for c, rows in acc.items():
            if c in all_acc:
                byd = {r["date"]: r for r in all_acc[c]}
                byd.update({r["date"]: r for r in rows})
                all_acc[c] = sorted(byd.values(), key=lambda r: r["date"])
            else:
                all_acc[c] = rows
        # 每个 index_type 收尾各落一次：即使下一个 type 中途崩溃，本 type 已完整
        save_l0({c: all_acc[c] for c in acc})
    meta_out = save_l0(all_acc)
    reps = {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "since": args.since, "until": until, "page_size": args.page_size,
            "codes": len(meta_out), "meta": meta_out, "failures": fails_all}
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    p = f"{C.CACHE_DIR}/sw_valuation_fetch.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(reps, f, ensure_ascii=False, indent=1)
    print(f"抓取报告：{p}")


if __name__ == "__main__":
    main()
