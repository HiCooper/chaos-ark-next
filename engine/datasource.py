# -*- coding: utf-8 -*-
"""数据层：从东方财富抓取行业板块 / 细分赛道 / 基准指数的日线，落盘为本地快照。

设计原则：
1. 抓到的原始响应原样落盘（cache/raw），任何重跑都从同一份快照重建，保证可复现。
2. 只抓一次，之后复用缓存；需要刷新时显式 --refresh。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

# L0 原始层：外部 API 一手快照，落盘后不可变（见 rawstore.py / docs/DATA-LAYERS.md）
RAW_DIR = f"{C.ROOT}/data/raw/kline_em"


def _curl(url: str) -> Optional[str]:
    """用 curl 抓取（东财对 python-urllib 有风控，curl 稳定）。"""
    cmd = [
        "curl", "-s", "--max-time", "30", "--compressed",
        "-A", C.EASTMONEY_UA,
        "-H", "Referer: https://quote.eastmoney.com/",
        url,
    ]
    for attempt in range(C.MAX_RETRY):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout
        except Exception:
            pass
        time.sleep(0.8 * (attempt + 1))
    return None


def fetch_kline(secid: str, refresh: bool = False) -> Optional[List[dict]]:
    """抓取单个标的日线。返回 [{date,open,close,high,low,volume,amount}, ...]"""
    os.makedirs(RAW_DIR, exist_ok=True)
    path = f"{RAW_DIR}/kline_{secid.replace('.', '_')}.json"
    if os.path.exists(path) and not refresh:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            payload = None
    else:
        payload = None

    if payload is None:
        url = (
            f"{C.KLINE_URL}?secid={secid}&fields1=f1,f2,f3&"
            f"fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&"
            f"beg={C.HISTORY_BEG}&end=20500101&lmt=3000"
        )
        txt = _curl(url)
        if not txt:
            return None
        try:
            payload = json.loads(txt)
        except Exception:
            return None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        time.sleep(C.FETCH_SLEEP)

    data = (payload or {}).get("data")
    if not data or not data.get("klines"):
        return None
    out = []
    for line in data["klines"]:
        p = line.split(",")
        try:
            out.append({
                "date": p[0],
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
            })
        except (ValueError, IndexError):
            continue
    return out or None


def fetch_block_list(refresh: bool = False) -> List[dict]:
    """全量板块列表（东财 m:90 t:2）。"""
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    path = f"{C.CACHE_DIR}/all_blocks.json"
    if os.path.exists(path) and not refresh:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    rows: List[dict] = []
    for page in range(1, 6):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f20&fs=m:90+t:2&"
            "fields=f12,f14,f3,f20,f6"
        )
        txt = _curl(url)
        if not txt:
            continue
        try:
            diff = json.loads(txt)["data"]["diff"]
        except Exception:
            continue
        rows.extend(diff)
        time.sleep(C.FETCH_SLEEP)
    uniq = {}
    for r in rows:
        uniq.setdefault(r["f12"], r)
    result = list(uniq.values())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return result


def resolve_universe(refresh: bool = False) -> Dict[str, dict]:
    """解析出 ARK 的行业宇宙：31 个申万一级 + Top N 细分赛道。

    返回 {code: {code, name, level, market_cap}}
    """
    blocks = fetch_block_list(refresh=refresh)
    by_name = {b["f14"]: b for b in blocks}
    universe: Dict[str, dict] = {}

    for name in C.SW_L1:
        if name in C.L1_EXCLUDE:
            continue
        b = by_name.get(name)
        if not b:
            continue
        universe[b["f12"]] = {
            "code": b["f12"], "name": b["f14"], "level": "L1",
            "market_cap": float(b.get("f20") or 0),
        }

    l1_codes = set(universe.keys())
    subs = []
    for b in blocks:
        nm = b["f14"]
        if b["f12"] in l1_codes:
            continue
        # 细分赛道：名称命中白名单，且不是「XXⅡ/Ⅲ」这类同义层级
        if nm in C.SUB_SECTOR_WHITELIST and not nm.endswith(("Ⅱ", "Ⅲ")):
            subs.append(b)
    subs.sort(key=lambda x: -(float(x.get("f20") or 0)))
    for b in subs[:C.SUB_SECTOR_TOPN]:
        universe[b["f12"]] = {
            "code": b["f12"], "name": b["f14"], "level": "L2",
            "market_cap": float(b.get("f20") or 0),
        }
    return universe


def build_panel(refresh: bool = False, verbose: bool = True) -> dict:
    """抓取宇宙内全部标的 + 基准，返回面板数据。"""
    universe = resolve_universe(refresh=refresh)
    panel: Dict[str, List[dict]] = {}
    meta = {}

    targets = [(c, m["name"], m["level"]) for c, m in universe.items()]
    bench = [(k, v, "BENCH") for k, v in C.BENCHMARKS.items()]
    all_targets = targets + bench

    for i, (code, name, level) in enumerate(all_targets):
        series = fetch_kline(f"90.{code}" if level != "BENCH" else code, refresh=refresh)
        if not series:
            # 板块端失败时，基准 secid 直接带市场前缀
            series = fetch_kline(code, refresh=refresh)
        if not series:
            if verbose:
                print(f"  [warn] 无数据 {code} {name}")
            continue
        panel[code] = series
        meta[code] = {"name": name, "level": level}
        if verbose and (i + 1) % 20 == 0:
            print(f"  抓取进度 {i+1}/{len(all_targets)}")

    # 对齐交易日：以基准指数的交易日为准
    base_dates = [d["date"] for d in panel.get(C.PRIMARY_BENCH, [])]
    if not base_dates:
        raise RuntimeError("基准指数数据缺失，无法构建面板")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dates": base_dates,
        "meta": meta,
        "series": {},
    }
    for code, series in panel.items():
        out["series"][code] = {d["date"]: d for d in series if d["date"] in set(base_dates)}
    if verbose:
        print(f"  面板完成：{len(panel)} 个标的，{len(base_dates)} 个交易日")
    return out


def save_panel(panel: dict, path: Optional[str] = None):
    path = path or f"{C.CACHE_DIR}/panel.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(panel, f, ensure_ascii=False)
    return path


def load_panel(path: Optional[str] = None) -> dict:
    path = path or f"{C.CACHE_DIR}/panel.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    p = build_panel(refresh=refresh)
    print(save_panel(p))
