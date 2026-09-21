# -*- coding: utf-8 -*-
"""指标层：把 OHLCV 序列转成每日特征向量（纯函数，可缓存、可重放）。

向量化实现（pandas），保证同一天 + 同一份数据 => 同一组特征。
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd


def compute_series_features(rows: List[dict]) -> Dict[str, dict]:
    """对单个标的计算全历史特征序列。

    rows: [{date, open, close, high, low, volume, amount}, ...] 按日期升序
    返回 {date: {...features}}
    """
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    a = df["amount"].astype(float)
    n = len(df)

    ret1 = c.pct_change()
    f = pd.DataFrame({"date": df["date"], "close": c, "amount": a})
    for k in (5, 10, 20, 60, 120, 250):
        f[f"ret_{k}"] = c.pct_change(k)
    f["ret_1"] = ret1
    for k in (5, 10, 20, 60, 120, 250):
        f[f"ma{k}"] = c.rolling(k).mean()

    ma20 = f["ma20"]
    f["ma20_slope"] = ma20 / ma20.shift(5) - 1.0
    f["dist_ma20"] = c / ma20 - 1.0
    f["dist_ma60"] = c / f["ma60"] - 1.0
    f["dist_ma120"] = c / f["ma120"] - 1.0
    f["dist_ma250"] = c / f["ma250"] - 1.0

    hi60 = c.rolling(60).max()
    lo60 = c.rolling(60).min()
    hi250 = c.rolling(250).max()
    lo250 = c.rolling(250).min()
    f["dd_from_high60"] = c / hi60 - 1.0
    f["pos60"] = (c - lo60) / (hi60 - lo60).replace(0, np.nan)
    f["pos250"] = (c - lo250) / (hi250 - lo250).replace(0, np.nan)

    vol20 = ret1.rolling(20).std() * math.sqrt(252)
    vol60 = ret1.rolling(60).std() * math.sqrt(252)
    f["vol20"] = vol20
    f["vol60"] = vol60

    # 滚动最大回撤（60 日窗口）
    dd = np.full(n, np.nan)
    cv = c.to_numpy()
    for i in range(n):
        if i < 59:
            continue
        w = cv[i - 59:i + 1]
        peak = np.maximum.accumulate(w)
        dd[i] = float((w / peak - 1.0).min())
    f["maxdd_60"] = dd

    vma5, vma20 = v.rolling(5).mean(), v.rolling(20).mean()
    ama5, ama20 = a.rolling(5).mean(), a.rolling(20).mean()
    f["vol_ratio"] = vma5 / vma20.replace(0, np.nan)
    f["amount_ratio"] = ama5 / ama20.replace(0, np.nan)

    up_v = (ret1 > 0).astype(float) * v
    dn_v = (ret1 < 0).astype(float) * v
    f["up_vol_share"] = (
        up_v.rolling(10).sum() / (up_v.rolling(10).sum() + dn_v.rolling(10).sum()).replace(0, np.nan)
    )

    # 连续上涨天数
    cup = np.zeros(n)
    run = 0
    cvl = cv.tolist()
    for i in range(1, n):
        run = run + 1 if cvl[i] > cvl[i - 1] else 0
        cup[i] = run
    f["consec_up"] = cup

    f["r5_over_r20"] = np.where(f["ret_20"].abs() > 1e-4, f["ret_5"] / f["ret_20"], np.nan)
    f["sharpe20"] = f["ret_20"] / vol20.replace(0, np.nan)

    out: Dict[str, dict] = {}
    cols = [c for c in f.columns if c != "date"]
    for row in f.itertuples(index=False):
        d = row._asdict() if hasattr(row, "_asdict") else dict(zip(f.columns, row))
        dt = d.pop("date")
        out[dt] = {k: (None if (isinstance(val, float) and val != val) else float(val))
                   for k, val in d.items()}
    return out


def percentile_rank(value: float, pool: Sequence[float]) -> float:
    """value 在 pool 中的分位（0-1）。"""
    if value is None or value != value:
        return 0.5
    arr = np.asarray([x for x in pool if x is not None and x == x], dtype=float)
    if arr.size == 0:
        return 0.5
    return float((arr < value).sum() / arr.size)


def clip01(x: float) -> float:
    if x is None or x != x:
        return 0.5
    return max(0.0, min(1.0, float(x)))


def norm_score(x: float, lo: float, hi: float) -> float:
    """线性归一化到 0-100，超出区间截断。"""
    if x is None or x != x:
        return 50.0
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))
