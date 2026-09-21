# -*- coding: utf-8 -*-
"""特征库：把面板数据预计算为「标的 x 日期 x 字段」的三维张量。

落盘为 npz（列式 + float32），避免 JSON 体积爆炸；
任何一天的分析都只是「查表 + 打分」，因此回测 400 天与跑 1 天用的是同一套代码路径。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Sequence

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from indicators import compute_series_features  # noqa: E402

EW_CODE = "__EW__"      # 申万一级等权市场指数（行业轮动的真正对手盘）
FIELDS = [
    "close", "amount",
    "ret_5", "ret_10", "ret_20", "ret_60", "ret_120", "ret_250",
    "dist_ma20", "dist_ma60", "dist_ma120", "dist_ma250", "ma20_slope",
    "dd_from_high60", "pos60", "pos250",
    "vol20", "vol60", "maxdd_60",
    "vol_ratio", "amount_ratio", "up_vol_share",
    "consec_up", "r5_over_r20", "sharpe20",
    "rs_20", "rs_60", "rs_120",
]


class FeatureStore:
    def __init__(self, dates: List[str], codes: List[str],
                 data: Dict[str, Dict[str, np.ndarray]], meta: Dict[str, dict]):
        self.dates = dates
        self.codes = codes
        self.data = data                 # code -> field -> np.array(len(dates))
        self.meta = meta                 # code -> {name, level}
        self._idx = {d: i for i, d in enumerate(dates)}

    # ------------------------------------------------------------ 存取
    def idx(self, date: str) -> int:
        return self._idx[date]

    def get(self, code: str, date: str, field: str) -> float:
        arr = self.data[code][field]
        v = arr[self._idx[date]]
        return float(v)

    def row(self, code: str, date: str) -> Dict[str, float]:
        i = self._idx[date]
        return {f: float(self.data[code][f][i]) for f in FIELDS}

    def col(self, field: str, date: str, codes: Optional[Sequence[str]] = None) -> List[float]:
        """某交易日、某字段在所有（或指定）标的上的横截面取值。"""
        i = self._idx[date]
        cs = codes if codes is not None else self.codes
        return [float(self.data[c][field][i]) for c in cs]

    def series(self, code: str, field: str) -> np.ndarray:
        return self.data[code][field]

    def l1_codes(self) -> List[str]:
        return [c for c in self.codes if self.meta.get(c, {}).get("level") == "L1"]

    def investable(self, date: str) -> List[str]:
        """当日可打分标的：数据充分且非基准/非等权指数。"""
        i = self._idx[date]
        out = []
        for c in self.codes:
            lv = self.meta.get(c, {}).get("level")
            if lv not in ("L1", "L2"):
                continue
            if self.data[c]["ret_60"][i] != self.data[c]["ret_60"][i]:
                continue
            out.append(c)
        return out

    def save(self, path: str):
        payload = {}
        for c in self.codes:
            for f in FIELDS:
                payload[f"{c}|{f}"] = self.data[c][f].astype(np.float32)
        np.savez_compressed(path, **payload)
        with open(path + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump({"dates": self.dates, "codes": self.codes,
                       "meta": self.meta, "fields": FIELDS}, fh, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> "FeatureStore":
        meta = json.load(open(path + ".meta.json", "r", encoding="utf-8"))
        z = np.load(path)
        data: Dict[str, Dict[str, np.ndarray]] = {c: {} for c in meta["codes"]}
        for c in meta["codes"]:
            for f in meta["fields"]:
                data[c][f] = z[f"{c}|{f}"].astype(np.float64)
        return FeatureStore(meta["dates"], meta["codes"], data, meta["meta"])


def _align(panel: dict, code: str, dates: List[str]) -> List[dict]:
    """把 {date: row} 对齐到统一日期轴；收盘价前向填充，量额缺失留空。"""
    s = panel["series"][code]
    out = []
    last_close = None
    for d in dates:
        r = s.get(d)
        if r is None:
            if last_close is None:
                out.append(None)
                continue
            out.append({"date": d, "open": last_close, "close": last_close,
                        "high": last_close, "low": last_close,
                        "volume": float("nan"), "amount": float("nan")})
            continue
        last_close = float(r["close"])
        out.append({"date": d, "open": float(r["open"]), "close": float(r["close"]),
                    "high": float(r["high"]), "low": float(r["low"]),
                    "volume": float(r["volume"]), "amount": float(r["amount"])})
    return out


def build(panel: dict, last_n: Optional[int] = None, verbose: bool = True) -> FeatureStore:
    """从面板构建特征库。last_n 限制只算最近 N 个交易日（省内存/提速）。"""
    dates = panel["dates"]
    if last_n:
        dates = dates[-last_n:]
    codes = [c for c in panel["series"].keys()]

    # 1) 逐个标的算序列特征
    raw: Dict[str, Dict[str, dict]] = {}
    for i, code in enumerate(codes):
        rows = [r for r in _align(panel, code, dates) if r is not None]
        if len(rows) < 30:
            if verbose:
                print(f"  [skip] {code} 数据过少 {len(rows)}")
            continue
        raw[code] = compute_series_features(rows)
        if verbose and (i + 1) % 25 == 0:
            print(f"  特征进度 {i+1}/{len(codes)}")

    # 2) 构造申万一级等权市场指数（只用 L1）
    l1 = [c for c, m in panel["meta"].items()
          if m.get("level") == "L1" and c in raw]
    ew_close = []
    prev = 1.0
    for di, d in enumerate(dates):
        vals = []
        for c in l1:
            f = raw[c].get(d)
            if not f:
                continue
            prev_c = raw[c].get(dates[di - 1]) if di > 0 else None
            if not prev_c or not prev_c["close"]:
                continue
            vals.append(f["close"] / prev_c["close"] - 1.0)
        if di == 0 or not vals:
            ew_close.append(1.0)
            prev = 1.0
        else:
            m = sum(vals) / len(vals)
            prev = prev * (1.0 + m)
            ew_close.append(prev)
    ew_rows = [{"date": d, "open": p, "close": p, "high": p, "low": p,
                "volume": 0.0, "amount": 0.0} for d, p in zip(dates, ew_close)]
    raw[EW_CODE] = compute_series_features(ew_rows)

    # 3) 转列式张量 + 计算相对强度 rs
    data: Dict[str, Dict[str, np.ndarray]] = {}
    final_codes = list(raw.keys())
    ew_r = {k: np.array([raw[EW_CODE].get(d, {}).get(f"ret_{k}", float("nan")) for d in dates], dtype=float)
            for k in (20, 60, 120)}

    for code in final_codes:
        feat = raw[code]
        data[code] = {}
        for f in FIELDS:
            if f in ("rs_20", "rs_60", "rs_120"):
                continue
            arr = np.array([(feat.get(d) or {}).get(f, float("nan")) for d in dates], dtype=np.float64)
            data[code][f] = arr
        for k in (20, 60, 120):
            rk = np.array([(feat.get(d) or {}).get(f"ret_{k}", float("nan")) for d in dates], dtype=np.float64)
            data[code][f"rs_{k}"] = rk - ew_r[k]

    meta = dict(panel["meta"])
    meta[EW_CODE] = {"name": "行业等权(自建)", "level": "EW"}
    return FeatureStore(dates, final_codes, data, meta)


if __name__ == "__main__":
    import datasource as DS
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1500
    p = DS.load_panel()
    fs = build(p, last_n=n)
    out = f"{C.CACHE_DIR}/features.npz"
    fs.save(out)
    print(f"saved {out}: {len(fs.codes)} codes x {len(fs.dates)} dates")
    print("date range:", fs.dates[0], "->", fs.dates[-1])
