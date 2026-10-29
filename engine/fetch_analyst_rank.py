# -*- coding: utf-8 -*-
"""抓取巨潮「投资评级」流水 -> L0（价格之外的第 2 个信息源）。

数据源
    akshare `stock_rank_forecast_cninfo(date=YYYYMMDD)`
    = 巨潮资讯「投资评级」接口，返回**当日发布的**券商评级记录。
    实测历史到 2014 年可用（更早未测）；每日 60~390 条、覆盖 60~230 只个股。

为什么算「价格之外」
    评级方向（上调/下调）、是否首次覆盖、目标价，都不来自价格路径，
    是分析师对**基本面**的判断。与上一轮「估值」的关键区别：
    估值是**市场给的价**（PE/PB 由价格算出），评级是**人对基本面的预期**。

落盘口径（L0，write-once + merge-append）
    kind = "analyst_rank"，key = 交易日（YYYY-MM-DD），**一天一个文件**。
    为什么不用年/月做 key：`rawstore.put` 的 merge 是「按 date 去重」
    （它假设一天一条记录），一天几百条评级会被压成一条 → **静默丢数据**。
    按天做 key 的额外好处：断点续跑天然成立，单日失败不污染其他天。

并发
    抓取（网络 IO）用线程池；**落盘在主线程串行** —— `rawstore.put` 要读改写
    manifest.json，多线程并发 put 会互相覆盖、丢 manifest 条目。

日期源
    交易日列表直接取自特征库 `fs.dates`，不是「跳过周末」—— 零浪费请求，
    也不会漏掉调休交易日。

用法
    python engine/fetch_analyst_rank.py --dry                 # 只看计划
    python engine/fetch_analyst_rank.py --since 2026-09-01     # 小规模试抓
    python engine/fetch_analyst_rank.py --since 2016-01-01 --workers 4
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from typing import List, Optional

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import rawstore as RS  # noqa: E402
from feature_store import FeatureStore  # noqa: E402

KIND = "analyst_rank"
SOURCE = "cninfo:投资评级 (akshare stock_rank_forecast_cninfo)"
NOTE = ("券商个股评级流水，一天一文件；字段 code/name/date/org/analyst/"
        "rating/is_first/change/prev_rating/tp_lo/tp_hi")

FIELDS = ["date", "code", "name", "org", "analyst", "rating",
          "is_first", "change", "prev_rating", "tp_lo", "tp_hi"]


def _clean(v):
    """pandas 标量 -> JSON 安全值（NaN/inf/空串 -> None）。"""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def _code(v) -> Optional[str]:
    """证券代码保持 6 位字符串（000552 不能变成 552）。"""
    s = _clean(v)
    if s is None:
        return None
    s = s.split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _num(v) -> Optional[float]:
    s = _clean(v)
    if s is None:
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def rows_from_df(df: pd.DataFrame, date: str) -> List[dict]:
    out: List[dict] = []
    for _, r in df.iterrows():
        code = _code(r.get("证券代码"))
        if not code:
            continue
        pub = _clean(r.get("发布日期")) or date
        out.append({
            "date": str(pub)[:10],
            "code": code,
            "name": _clean(r.get("证券简称")),
            "org": _clean(r.get("研究机构简称")),
            "analyst": _clean(r.get("研究员名称")),
            "rating": _clean(r.get("投资评级")),
            "is_first": _clean(r.get("是否首次评级")),
            "change": _clean(r.get("评级变化")),
            "prev_rating": _clean(r.get("前一次投资评级")),
            "tp_lo": _num(r.get("目标价格-下限")),
            "tp_hi": _num(r.get("目标价格-上限")),
        })
    return out


_AK = None


def _fetch_rows(ak, date: str, retries: int = 3) -> List[dict]:
    """抓一个交易日的评级流水。重试耗尽抛异常（不允许静默变 0 行）。"""
    key = date.replace("-", "")
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            df = ak.stock_rank_forecast_cninfo(date=key)
            if df is None or df.empty:
                return []
            return rows_from_df(df, date)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{date}: {type(last).__name__}: {last}")


def _fetch_worker(date: str):
    """子进程入口：抓一天，返回 (date, rows|None, err|str|None)。

    **为什么必须用进程池而不是线程池**：akshare 的 cninfo 接口内部用
    mini_racer（V8）解密请求参数，而 V8 isolate 不是线程安全的——多线程并发
    调用会让 libmini_racer 直接 segfault（本机实测：线程池 3 并发即崩）。
    进程隔离是唯一干净的解法，每个子进程有自己的 V8。
    异常不向外抛，转成字符串回传，避免污染进程池。
    """
    global _AK
    if _AK is None:
        import akshare as ak
        _AK = ak
    try:
        return date, _fetch_rows(_AK, date), None
    except Exception as e:  # noqa: BLE001
        return date, None, f"{type(e).__name__}: {e}"


def run(since: str, until: str, workers: int, dry: bool = False):
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    dates = [d for d in fs.dates if since <= d <= until]
    todo = [d for d in dates if RS.get(KIND, d) is None]
    print(f"交易日 {len(dates)} 天（{dates[0]} ~ {dates[-1]}）；"
          f"已有 L0 {len(dates) - len(todo)} 天；待抓 {len(todo)} 天", flush=True)
    if dry:
        print("前 10 个待抓日:", todo[:10])
        return
    if not todo:
        print("无待抓日期，退出。")
        return

    fails: List[dict] = []
    done = 0
    n_empty = 0
    t0 = time.time()
    batch_size = max(workers * 2, 4)
    ex = ProcessPoolExecutor(max_workers=workers)
    try:
        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            futs = [ex.submit(_fetch_worker, d) for d in batch]
            rebuild = False
            for fut in futs:
                try:
                    d, rows, err = fut.result()
                except Exception as e:       # noqa: BLE001  子进程崩溃 -> 池失效
                    fails.append({"date": "?", "err": f"pool: {e}"})
                    print(f"  [err] 进程池异常，重建：{e}", flush=True)
                    rebuild = True
                    break
                if err:
                    fails.append({"date": d, "err": err})
                    print(f"  [err] {d} {err}", flush=True)
                    continue
                if not rows:
                    n_empty += 1
                # 落盘只在主进程做：rawstore.put 要读改写 manifest.json
                RS.put(KIND, d, rows, SOURCE, merge=True, note=NOTE)
                done += 1
            if rebuild:
                ex.shutdown(wait=False)
                ex = ProcessPoolExecutor(max_workers=workers)
            n = done + len(fails)
            if n % 200 < batch_size:
                el = time.time() - t0
                print(f"  进度 {n}/{len(todo)}  成功 {done}  空 {n_empty}  "
                      f"失败 {len(fails)}  {el:.0f}s "
                      f"({el / max(n, 1):.2f}s/天)", flush=True)
    finally:
        ex.shutdown(wait=True)

    el = time.time() - t0
    rep = {
        "kind": KIND, "since": since, "until": until, "workers": workers,
        "n_todo": len(todo), "n_ok": done, "n_empty": n_empty,
        "n_fail": len(fails), "elapsed_sec": round(el, 1),
        "sec_per_day": round(el / max(len(todo), 1), 3),
        "failures": fails,
    }
    p = f"{C.CACHE_DIR}/analyst_rank_fetch.json"
    json.dump(rep, open(p, "w"), ensure_ascii=False, indent=1)
    print(f"完成：成功 {done} 天（其中 {n_empty} 天无评级发布），失败 {len(fails)} 天，"
          f"耗时 {el:.0f}s（{rep['sec_per_day']}s/天）→ {p}")
    if fails:
        print(f"  ⚠️ {len(fails)} 天失败，重跑同一命令即可续抓（已有 L0 会自动跳过）")


if __name__ == "__main__":
    a = sys.argv[1:]

    def opt(name: str, default: str) -> str:
        return a[a.index(name) + 1] if name in a else default

    run(opt("--since", "2016-01-01"), opt("--until", "9999-12-31"),
        int(opt("--workers", "4")), dry="--dry" in a)
