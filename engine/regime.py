# -*- coding: utf-8 -*-
"""工作流 Step 1 —— 环境识别（Regime）。

回答一个问题：今天的市场，是适合「进攻选强」，还是「防守埋伏」，还是「什么都不做」。

输出不是单一标签，而是 5 个可解释分项 + 综合热度分 + 滞回后的 regime + 置信度。
所有阈值集中于 config.REGIME_RULES，config 不变则结论可复现。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from indicators import norm_score  # noqa: E402
from feature_store import EW_CODE, FeatureStore  # noqa: E402

REGIME_ORDER = ["PANIC", "DEFEND", "NEUTRAL", "ATTACK"]

# 档位阈值 / 平滑 / 确认 —— 提成模块常量，便于页面与规格书引用同一份来源，
# 避免「文档写着 62、代码里改了 60」这类静默漂移。
REGIME_THRESHOLDS = {"attack": 62.0, "neutral": 46.0, "defend": 32.0}
EMA_ALPHA = 0.4              # 环境热度分指数平滑系数（等效约 3 日 EMA）
REGIME_CONFIRM_DAYS = 2      # 连续 N 日越界才切换档位（滞回）


def _pct_rank_of_series(arr: np.ndarray, i: int, window: int = 250) -> float:
    """当前值在过去 window 期中的分位（不足时按已有样本）。"""
    lo = max(0, i - window + 1)
    w = arr[lo:i + 1]
    w = w[~np.isnan(w)]
    if w.size < 20:
        return 0.5
    cur = arr[i]
    if cur != cur:
        return 0.5
    return float((w < cur).sum() / w.size)


def build_regime_series(fs: FeatureStore, verbose: bool = True) -> Dict[str, dict]:
    """逐日计算环境状态。返回 {date: regime_info}。"""
    dates = fs.dates
    n = len(dates)
    l1 = fs.l1_codes()
    bench = C.PRIMARY_BENCH if C.PRIMARY_BENCH in fs.codes else "1.000001"

    b_close = fs.series(bench, "close")
    b_vol20 = fs.series(bench, "vol20")
    b_dist20 = fs.series(bench, "dist_ma20")
    b_dd60 = fs.series(bench, "dd_from_high60")
    b_ret20 = fs.series(bench, "ret_20")
    b_ret60 = fs.series(bench, "ret_60")
    b_slope = fs.series(bench, "ma20_slope")
    ew_ret20 = fs.series(EW_CODE, "ret_20")
    ew_slope = fs.series(EW_CODE, "ma20_slope")

    # 市场总成交额（L1 之和）用于流动性分项
    amt_sum = np.zeros(n)
    for c in l1:
        amt_sum += np.nan_to_num(fs.series(c, "amount"))
    amt_ma5 = np.convolve(amt_sum, np.ones(5) / 5, mode="full")[:n]
    amt_ma20 = np.convolve(amt_sum, np.ones(20) / 20, mode="full")[:n]
    amt_ma5[:4] = np.nan
    amt_ma20[:19] = np.nan

    raw_scores = np.full(n, np.nan)
    parts: List[dict] = [{} for _ in range(n)]

    for i in range(n):
        r20 = np.array([fs.series(c, "ret_20")[i] for c in l1], dtype=float)
        r5 = np.array([fs.series(c, "ret_5")[i] for c in l1], dtype=float)
        valid = r20[~np.isnan(r20)]
        if valid.size < 10:
            continue
        breadth20 = float((valid > 0).sum() / valid.size)
        breadth5 = float((r5[~np.isnan(r5)] > 0).sum() / max((~np.isnan(r5)).sum(), 1))
        median20 = float(np.median(valid))
        disp = float(np.nanstd(valid))

        p_breadth = breadth20 * 100.0
        p_trend = 0.6 * norm_score(b_dist20[i], -0.05, 0.05) + 0.4 * norm_score(b_slope[i], -0.03, 0.03)
        p_momentum = norm_score(median20, -0.08, 0.12)
        vol_pct = _pct_rank_of_series(b_vol20, i)
        p_vol = (1.0 - vol_pct) * 100.0
        liq_ratio = (amt_ma5[i] / amt_ma20[i]) if amt_ma20[i] and amt_ma20[i] == amt_ma20[i] else 1.0
        p_liq = norm_score(liq_ratio, 0.70, 1.30)

        comps = {"breadth": p_breadth, "trend": p_trend, "momentum": p_momentum,
                 "vol": p_vol, "liquidity": p_liq}
        score = (0.32 * p_breadth + 0.24 * p_trend + 0.24 * p_momentum
                 + 0.10 * p_vol + 0.10 * p_liq)
        raw_scores[i] = score
        parts[i] = {
            "breadth20": breadth20, "breadth5": breadth5,
            "median_ret20": median20, "dispersion20": disp,
            "bench_ret20": float(b_ret20[i]), "bench_ret60": float(b_ret60[i]),
            "bench_dist_ma20": float(b_dist20[i]), "bench_dd60": float(b_dd60[i]),
            "bench_vol20": float(b_vol20[i]), "vol_pct": vol_pct,
            "liq_ratio": float(liq_ratio) if liq_ratio == liq_ratio else None,
            "components": {k: float(v) for k, v in comps.items()},
        }

    # 3 日 EMA 平滑，避免日频抖动
    smooth = np.full(n, np.nan)
    alpha = EMA_ALPHA
    prev = None
    for i in range(n):
        v = raw_scores[i]
        if v != v:
            smooth[i] = prev if prev is not None else np.nan
            continue
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        smooth[i] = prev

    # 滞回映射：需连续 N 日越过阈值才切换档位
    def to_regime(s: float) -> str:
        if s >= REGIME_THRESHOLDS["attack"]:
            return "ATTACK"
        if s >= REGIME_THRESHOLDS["neutral"]:
            return "NEUTRAL"
        if s >= REGIME_THRESHOLDS["defend"]:
            return "DEFEND"
        return "PANIC"

    out: Dict[str, dict] = {}
    cur = None
    pending = None
    pending_cnt = 0
    for i in range(n):
        s = smooth[i]
        if s != s:
            continue
        r = to_regime(s)
        if cur is None:
            cur = r
        elif r != cur:
            if pending == r:
                pending_cnt += 1
            else:
                pending, pending_cnt = r, 1
            if pending_cnt >= REGIME_CONFIRM_DAYS:
                cur, pending, pending_cnt = r, None, 0
        else:
            pending, pending_cnt = None, 0

        # 恐慌强制覆盖：极端回撤或极端波动直接降级
        forced = None
        if parts[i]:
            if parts[i]["bench_dd60"] == parts[i]["bench_dd60"] and parts[i]["bench_dd60"] < C.REGIME_RULES["bench_dd_panic"]:
                forced = "PANIC"
            elif parts[i]["vol_pct"] > C.REGIME_RULES["vol_pct_panic"] and cur in ("ATTACK", "NEUTRAL"):
                forced = "DEFEND"
        label = forced if forced else cur
        if forced and forced == "PANIC":
            cur = "PANIC"

        comps = parts[i].get("components", {})
        vals = [v for v in comps.values() if v is not None]
        spread = float(np.std(vals)) if len(vals) > 1 else 20.0
        conf = max(0.35, min(0.95, 1.0 - spread / 45.0))

        out[dates[i]] = {
            "date": dates[i],
            "regime": label,
            "env_score": float(s),
            "raw_score": float(raw_scores[i]) if raw_scores[i] == raw_scores[i] else None,
            "confidence": round(conf, 3),
            "forced": bool(forced),
            **{k: v for k, v in parts[i].items() if k != "components"},
            "components": {k: round(float(v), 2) for k, v in comps.items()},
        }
    if verbose:
        print(f"  环境序列完成：{len(out)} 天")
    return out


def regime_weights(regime: str) -> Dict[str, float]:
    """环境驱动的权重调整（归一化到和为 BASE 权重之和）。"""
    adj = C.REGIME_WEIGHT_ADJ.get(regime, C.REGIME_WEIGHT_ADJ["NEUTRAL"])
    w = {k: C.BASE_WEIGHTS[k] * adj.get(k, 1.0) for k in C.BASE_WEIGHTS}
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}


if __name__ == "__main__":
    from feature_store import FeatureStore
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    rs = build_regime_series(fs)
    json.dump(rs, open(f"{C.CACHE_DIR}/regime.json", "w"), ensure_ascii=False)
    ds = sorted(rs.keys())
    for d in ds[-8:]:
        r = rs[d]
        print(d, r["regime"], round(r["env_score"], 1), "breadth=%.2f" % r["breadth20"])
