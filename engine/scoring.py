# -*- coding: utf-8 -*-
"""工作流 Step 2-3 —— 行业六维打分。

六个维度各自 0-100，全部是「当日横截面可比较」的标准化量，不含未来信息。
维度：趋势动量 / 加速度 / 量能确认 / 结构质量 / 拥挤度 / 环境适配。

实现为「全历史批量预计算」，因此：
  - 跑单日 与 回测 400 天 走的是同一个矩阵，不存在两条代码路径；
  - 结论可逐日重放。
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from feature_store import FeatureStore  # noqa: E402

DIMS = ["mainline", "pullback", "exhaust", "rebound", "risk", "regime_fit"]


def _norm(arr: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """线性归一化到 0-100，NaN -> 50（中性）。"""
    out = (arr - lo) / (hi - lo) * 100.0
    out = np.clip(out, 0.0, 100.0)
    return np.where(np.isnan(arr), 50.0, out)


def _safe(arr: np.ndarray) -> np.ndarray:
    return np.nan_to_num(arr, nan=0.0)


def compute_dimension_scores(fs: FeatureStore) -> Dict[str, np.ndarray]:
    """计算 5 个与 regime 无关的维度。

    方向由 engine/diag*.py 的 Rank IC 实证确定，不是直觉：
      - 长期（120/250 日）动量强正向：IC≈0.55@40日，分年度稳定 0.52-0.58，分组单调；
      - 短期（20/60 日）为反转，且**仅在长期上升趋势内有效**（条件 IC +0.12 vs +0.002），
        因此 pullback / rebound 两个维度按主线强度做条件加权，避免在长期弱势行业里「接飞刀」。
    """
    codes = fs.codes
    n_d = len(fs.dates)
    out = {d: np.full((len(codes), n_d), np.nan) for d in DIMS}

    for ci, code in enumerate(codes):
        ret5 = fs.series(code, "ret_5")
        ret20 = fs.series(code, "ret_20")
        ret60 = fs.series(code, "ret_60")
        ret120 = fs.series(code, "ret_120")
        ret250 = fs.series(code, "ret_250")
        rs120 = fs.series(code, "rs_120")
        slope = fs.series(code, "ma20_slope")
        dist20 = fs.series(code, "dist_ma20")
        dist120 = fs.series(code, "dist_ma120")
        dist250 = fs.series(code, "dist_ma250")
        volr = fs.series(code, "vol_ratio")
        amtr = fs.series(code, "amount_ratio")
        uvs = fs.series(code, "up_vol_share")
        vol20 = fs.series(code, "vol20")
        ddh = fs.series(code, "dd_from_high60")

        # ---- 1. mainline：长期主线强度（最强因子，权重最高）
        mainline = (0.40 * _norm(ret120, -0.10, 0.50)
                    + 0.25 * _norm(ret250, -0.15, 0.80)
                    + 0.20 * _norm(dist120, -0.10, 0.30)
                    + 0.15 * _norm(dist250, -0.15, 0.40))
        ml_gate = 0.45 + 0.55 * np.nan_to_num(mainline, nan=50.0) / 100.0   # 主线强度闸门

        # ---- 2. pullback：短期回调深度（只有在主线里才成立）
        pullback_raw = (0.40 * _norm(-ret20, -0.20, 0.15)
                        + 0.30 * _norm(-ddh, 0.0, 0.30)
                        + 0.30 * _norm(-dist20, -0.15, 0.10))
        pullback = pullback_raw * ml_gate

        # ---- 3. exhaust：量能枯竭 / 抛压衰竭
        exhaust = (0.50 * _norm(-volr, -1.80, -0.60)
                   + 0.30 * _norm(-amtr, -1.80, -0.60)
                   + 0.20 * _norm(0.55 - uvs, -0.25, 0.25))

        # ---- 4. rebound：触底确认（跌势减缓 + 短期转正 + MA20 拐头）
        rebound_raw = (0.50 * _norm(ret5 - ret20 * 0.25, -0.05, 0.08)
                       + 0.30 * _norm(ret5, -0.08, 0.10)
                       + 0.20 * _norm(slope, -0.04, 0.06))
        rebound = rebound_raw * (0.40 + 0.60 * np.nan_to_num(mainline, nan=50.0) / 100.0)

        # ---- 5. risk：波动与量能极值控制（弱预测力，仅用于排除极端）
        risk = (0.70 * _norm(0.45 - vol20, -0.25, 0.35)
                + 0.30 * _norm(1.60 - volr, -0.40, 1.00))

        out["mainline"][ci] = mainline
        out["pullback"][ci] = pullback
        out["exhaust"][ci] = exhaust
        out["rebound"][ci] = rebound
        out["risk"][ci] = risk
    return out


def compute_regime_fit(fs: FeatureStore, regime_series: Dict[str, dict],
                       dims: Dict[str, np.ndarray]) -> np.ndarray:
    """按当日环境合成 regime_fit，并给出 regime 调整后的综合分。"""
    codes = fs.codes
    dates = fs.dates
    fit = np.full((len(codes), len(dates)), np.nan)
    composite = np.full((len(codes), len(dates)), np.nan)

    for i, d in enumerate(dates):
        info = regime_series.get(d)
        if not info:
            continue
        r = info["regime"]
        ml = dims["mainline"][:, i]
        pb = dims["pullback"][:, i]
        ex = dims["exhaust"][:, i]
        rb = dims["rebound"][:, i]
        rk = dims["risk"][:, i]

        if r == "ATTACK":
            f = 0.60 * ml + 0.40 * rb
        elif r == "DEFEND":
            f = 0.50 * ml + 0.30 * rk + 0.20 * pb
        elif r == "PANIC":
            f = 0.60 * rk + 0.40 * ml
        else:
            f = 0.40 * ml + 0.30 * pb + 0.30 * rb
        fit[:, i] = f

        w = C.REGIME_WEIGHT_ADJ.get(r, C.REGIME_WEIGHT_ADJ["NEUTRAL"])
        raw_w = {k: C.BASE_WEIGHTS[k] * w.get(k, 1.0) for k in C.BASE_WEIGHTS}
        tot = sum(raw_w.values())
        comp = (raw_w["mainline"] * np.nan_to_num(ml, nan=50.0)
                + raw_w["pullback"] * np.nan_to_num(pb, nan=50.0)
                + raw_w["exhaust"] * np.nan_to_num(ex, nan=50.0)
                + raw_w["rebound"] * np.nan_to_num(rb, nan=50.0)
                + raw_w["risk"] * np.nan_to_num(rk, nan=50.0)
                + raw_w["regime_fit"] * np.nan_to_num(f, nan=50.0)) / tot
        composite[:, i] = comp
    dims["regime_fit"] = fit
    return composite


def build_score_matrix(fs: FeatureStore, regime_series: Dict[str, dict],
                       verbose: bool = True) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    dims = compute_dimension_scores(fs)
    composite = compute_regime_fit(fs, regime_series, dims)
    dims["composite"] = composite
    if verbose:
        print(f"  打分矩阵完成：{len(fs.codes)} x {len(fs.dates)}")
    return dims, composite


def rank_pct_of(matrix: np.ndarray, axis: int = 0) -> np.ndarray:
    """沿 axis 计算排名分位（0-1）。"""
    n = matrix.shape[axis]
    out = np.argsort(np.argsort(np.nan_to_num(matrix, nan=-1e9), axis=axis), axis=axis) / max(n - 1, 1)
    return out
