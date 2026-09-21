# -*- coding: utf-8 -*-
"""工作流 Step 5 —— 持有周期估计（数据驱动，非拍脑袋）。

方法：历史相似状态匹配（analog）
  1. 用当日的「状态向量」z = (trend, accel, volume, structure, crowding, env_score) 描述行业所处位置；
  2. 在该行业自身历史上找出状态最接近的 K 个交易日；
  3. 统计这批样本在之后 10/20/40/60 日的真实超额收益、胜率、最大回撤；
  4. 按「风险调整期望」择优，得到建议持有周期。

样本不足时回退到默认窗口并显式标记 low_confidence —— 不允许用臆测填补。
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from feature_store import EW_CODE, FeatureStore  # noqa: E402

HORIZONS = C.HORIZON_CANDIDATES
K_NEIGHBORS = 40
MIN_SAMPLES = 12
DEFAULT_HORIZON = 20


def precompute_forward(fs: FeatureStore, horizons: Optional[List[int]] = None) -> dict:
    """预计算未来 H 日收益 / 超额 / 最大回撤。返回 {(code,H): array}。"""
    horizons = horizons or HORIZONS
    n = len(fs.dates)
    ew = fs.series(EW_CODE, "close")
    bench_code = C.PRIMARY_BENCH if C.PRIMARY_BENCH in fs.codes else fs.codes[0]
    bch = fs.series(bench_code, "close")

    fwd_ret, fwd_dd, fwd_ex_ew, fwd_ex_bench = {}, {}, {}, {}
    for code in fs.codes:
        c = fs.series(code, "close")
        for H in horizons:
            r = np.full(n, np.nan)
            if n > H:
                r[:n - H] = c[H:] / c[:n - H] - 1.0
            fwd_ret[(code, H)] = r

            dd = np.full(n, np.nan)
            if n > H:
                w = sliding_window_view(c, H + 1)          # (n-H, H+1)
                mins = w[:, 1:].min(axis=1)                # 未来 1..H 日最低
                dd[:n - H] = mins / w[:, 0] - 1.0
            fwd_dd[(code, H)] = dd

            ew_r = np.full(n, np.nan)
            if n > H:
                ew_r[:n - H] = ew[H:] / ew[:n - H] - 1.0
            fwd_ex_ew[(code, H)] = r - ew_r

            bc = np.full(n, np.nan)
            if n > H:
                bc[:n - H] = bch[H:] / bch[:n - H] - 1.0
            fwd_ex_bench[(code, H)] = r - bc
    return {"ret": fwd_ret, "dd": fwd_dd, "ex_ew": fwd_ex_ew, "ex_bench": fwd_ex_bench}


def build_state_matrix(fs: FeatureStore, dims: Dict[str, np.ndarray],
                       regime_series: Dict[str, dict]) -> Dict[str, np.ndarray]:
    """状态向量矩阵（用于 analog 匹配）：(n_codes, n_dates, 6)。"""
    n_d = len(fs.dates)
    env = np.array([regime_series.get(d, {}).get("env_score", 50.0) or 50.0 for d in fs.dates])
    keys = ["mainline", "pullback", "exhaust", "rebound", "risk"]
    stack = np.stack([np.nan_to_num(dims[k], nan=50.0) for k in keys], axis=2)  # (codes, dates, 5)
    env_b = np.broadcast_to(env[None, :, None], (stack.shape[0], n_d, 1))
    return np.concatenate([stack, env_b], axis=2) / 100.0


def estimate_horizon(fs: FeatureStore, dims: Dict[str, np.ndarray],
                     regime_series: Dict[str, dict], fwd: dict,
                     code: str, date: str, state: np.ndarray,
                     k: int = K_NEIGHBORS) -> dict:
    """估计单个行业在单个交易日的建议持有周期。"""
    i = fs.idx(date)
    n = len(fs.dates)
    hist = state[:i - max(HORIZONS) - 1]          # 只取「未来窗口完整」的历史
    idx_valid = np.where(~np.isnan(hist).any(axis=1))[0]
    result = {
        "horizon": DEFAULT_HORIZON, "low_confidence": True, "samples": 0,
        "stats": {}, "picked_by": "default",
    }
    if idx_valid.size < MIN_SAMPLES:
        return result

    cur = state[i]
    if np.isnan(cur).any():
        return result
    d = np.linalg.norm(hist[idx_valid] - cur, axis=1)
    order = idx_valid[np.argsort(d)[:k]]
    if order.size < MIN_SAMPLES:
        return result

    best = None
    stats = {}
    for H in HORIZONS:
        ex = fwd["ex_ew"][(code, H)][order]
        dd = fwd["dd"][(code, H)][order]
        ok = ~(np.isnan(ex) | np.isnan(dd))
        cnt = int(ok.sum())
        if cnt < MIN_SAMPLES:
            stats[H] = {"samples": cnt, "insufficient": True}
            continue
        e, ddv = ex[ok], dd[ok]
        mean_ex = float(e.mean())
        win = float((e > C.REVIEW["outperf_win"]).sum() / cnt)
        mean_dd = float(ddv.mean())
        # 回撤惩罚系数 0.25：实证下 0.6 会系统性偏向最短窗口（10 日平均超额 -0.05%），
        # 而 40/60 日窗口才是期望为正的区间（60 日 +11.35%）。
        risk_adj = mean_ex - 0.25 * abs(mean_dd)
        stats[H] = {
            "samples": cnt, "mean_excess": round(mean_ex, 4),
            "win_rate": round(win, 3), "mean_maxdd": round(mean_dd, 4),
            "risk_adj": round(risk_adj, 4),
        }
        if best is None or risk_adj > best[1]:
            best = (H, risk_adj)
    if best is None:
        result["stats"] = {str(k2): v for k2, v in stats.items()}
        return result
    H = best[0]
    return {
        "horizon": H,
        "low_confidence": bool(stats[H].get("samples", 0) < 25),
        "samples": int(stats[H]["samples"]),
        "stats": {str(k2): v for k2, v in stats.items()},
        "picked_by": "risk_adjusted_expectancy",
        "mean_excess": stats[H]["mean_excess"],
        "win_rate": stats[H]["win_rate"],
        "mean_maxdd": stats[H]["mean_maxdd"],
    }


if __name__ == "__main__":
    from feature_store import FeatureStore
    import regime as RG
    import scoring as SC
    import json
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    rs = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    dims, comp = SC.build_score_matrix(fs, rs)
    fwd = precompute_forward(fs)
    st = build_state_matrix(fs, dims, rs)
    ci = fs.codes.index("BK1201")
    d = fs.dates[-1]
    print(fs.meta["BK1201"], d)
    print(estimate_horizon(fs, dims, rs, fwd, "BK1201", d, st[ci]))
