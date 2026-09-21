# -*- coding: utf-8 -*-
"""诊断：各维度 / 原始指标 与未来超额收益的 Rank IC。

用途：引擎调参不能靠直觉。这里用横截面 Rank IC 直接回答
「哪个维度真的有预测力、方向是正是负、稳不稳定」。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from feature_store import FeatureStore  # noqa: E402
import horizon as HZ  # noqa: E402
import scoring as SC  # noqa: E402


def rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(np.argsort(x))
    return order.astype(float)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 10:
        return np.nan
    ra, rb = rank(a[m]), rank(b[m])
    ra -= ra.mean()
    rb -= rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom > 1e-9 else np.nan


def run(fs: FeatureStore, dims: Dict[str, np.ndarray], fwd: dict,
        start_i: int, horizons=(10, 20, 40, 60)) -> dict:
    codes = fs.codes
    invest_mask = {}
    for i in range(start_i, len(fs.dates)):
        invest_mask[i] = [ci for ci, c in enumerate(codes)
                          if fs.meta.get(c, {}).get("level") in ("L1", "L2")
                          and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]

    metrics = ["trend", "accel", "volume", "structure", "crowding", "regime_fit", "composite"]
    raws = ["ret_5", "ret_10", "ret_20", "ret_60", "rs_20", "rs_60", "dist_ma20", "dist_ma60",
            "ma20_slope", "dd_from_high60", "pos60", "pos250", "vol20", "maxdd_60",
            "vol_ratio", "amount_ratio", "up_vol_share", "consec_up", "r5_over_r20", "sharpe20"]

    out: Dict[str, dict] = {}
    for H in horizons:
        for name in metrics + raws:
            ics = []
            for i in range(start_i, len(fs.dates) - H - 1):
                cis = invest_mask.get(i)
                if not cis or len(cis) < 15:
                    continue
                if name in metrics:
                    xs = dims[name][cis, i]
                else:
                    xs = np.array([fs.series(codes[ci], name)[i] for ci in cis])
                ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
                ic = spearman(xs, ys)
                if ic == ic:
                    ics.append(ic)
            if len(ics) < 30:
                continue
            a = np.array(ics)
            ic_mean = float(a.mean())
            ic_std = float(a.std(ddof=1))
            t = ic_mean / ic_std * np.sqrt(len(a)) if ic_std > 1e-9 else 0.0
            out[f"{name}|{H}"] = {
                "metric": name, "horizon": H, "ic_mean": round(ic_mean, 4),
                "ic_std": round(ic_std, 4), "t": round(float(t), 2),
                "ir": round(ic_mean / ic_std, 3) if ic_std > 1e-9 else 0.0,
                "n_days": len(a), "pos_ratio": round(float((a > 0).mean()), 3),
            }
    return out


def report(res: dict):
    rows = sorted(res.values(), key=lambda r: (r["metric"], r["horizon"]))
    print(f"{'metric':<14}{'H':>4}{'IC均值':>10}{'IC标准差':>10}{'IR':>8}{'t':>8}{'正比例':>8}")
    print("-" * 62)
    for r in rows:
        print(f"{r['metric']:<14}{r['horizon']:>4}{r['ic_mean']:>10.4f}"
              f"{r['ic_std']:>10.4f}{r['ir']:>8.3f}{r['t']:>8.2f}{r['pos_ratio']:>8.3f}")
    print("\n=== 按 20 日 IC 绝对值排序（真正有用的维度） ===")
    h20 = sorted([r for r in res.values() if r["horizon"] == 20],
                 key=lambda r: -abs(r["ic_mean"]))
    for r in h20:
        flag = "正向" if r["ic_mean"] > 0 else "反向"
        print(f"  {r['metric']:<14} IC={r['ic_mean']:+.4f} t={r['t']:+6.2f}  "
              f"正比例={r['pos_ratio']:.2f}  -> {flag}")


if __name__ == "__main__":
    import regime as RG
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    rs = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    dims, comp = SC.build_score_matrix(fs, rs)
    fwd = HZ.precompute_forward(fs)
    res = run(fs, dims, fwd, start_i=max(C.WARMUP, len(fs.dates) - 900))
    report(res)
    json.dump(res, open(f"{C.CACHE_DIR}/ic_diag.json", "w"), ensure_ascii=False, indent=1)
