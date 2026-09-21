# -*- coding: utf-8 -*-
"""第二轮诊断：检验「反转 / 超跌反弹 / 量能枯竭」类因子，以及动量的环境条件。

第一轮 IC 显示行业层面动量全线为负（t 约 -10），说明 A 股行业以均值回归为主。
本轮目的：找到真正有正向预测力、且分年度稳定的因子组合，用来重建打分体系。
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
from diag import spearman  # noqa: E402


def zscore(x: np.ndarray) -> np.ndarray:
    m = np.nanmean(x)
    s = np.nanstd(x)
    return (x - m) / s if s and s > 1e-12 else x * 0.0


def build_candidates(fs: FeatureStore) -> Dict[str, np.ndarray]:
    """构造候选因子矩阵：统一约定「值越大 = 越看多」。
    返回 {name: (n_codes, n_dates)}"""
    codes = fs.codes
    n = len(fs.dates)
    out: Dict[str, np.ndarray] = {}

    def stack(field: str, sign: float = 1.0) -> np.ndarray:
        return sign * np.vstack([fs.series(c, field) for c in codes])

    # 长周期动量（检验是否存在中长期动量）
    for H in (120, 250):
        m = np.vstack([fs.series(c, "close") for c in codes])
        r = np.full_like(m, np.nan)
        if n > H:
            r[:, :-H] = m[:, H:] / m[:, :-H] - 1.0
        out[f"ret_{H}"] = r
        out[f"rev_{H}"] = -r

    out["rev_20"] = stack("ret_20", -1.0)
    out["rev_60"] = stack("ret_60", -1.0)
    out["rev_rs20"] = stack("rs_20", -1.0)
    out["rev_rs60"] = stack("rs_60", -1.0)
    out["far_ma20"] = stack("dist_ma20", -1.0)
    out["far_ma60"] = stack("dist_ma60", -1.0)
    out["dd_deep"] = stack("dd_from_high60", -1.0)     # 越大 = 跌得越深
    out["maxdd_deep"] = stack("maxdd_60", -1.0)
    out["low_pos60"] = stack("pos60", -1.0)
    out["low_pos250"] = stack("pos250", -1.0)
    out["quiet_vol"] = stack("vol_ratio", -1.0)
    out["quiet_amt"] = stack("amount_ratio", -1.0)
    out["low_sharpe"] = stack("sharpe20", -1.0)
    out["few_consec"] = stack("consec_up", -1.0)
    out["low_uvs"] = stack("up_vol_share", -1.0)

    # 触底反弹：中期弱 + 近期强
    r5 = stack("ret_5")
    r20 = stack("ret_20")
    out["bottom_rebound"] = zscore(r5) - zscore(r20)
    # 跌势减缓：近期跌幅小于中期线性外推
    out["decel"] = r5 - r20 * 0.25
    # 深跌 + 缩量（抛压衰竭）
    out["exhaust"] = zscore(stack("dd_from_high60", -1.0)) + zscore(stack("vol_ratio", -1.0))
    # 超跌反转组合
    out["rev_combo"] = (zscore(stack("ret_60", -1.0)) + zscore(stack("ret_20", -1.0))
                        + zscore(stack("rs_60", -1.0)))
    # 反转 + 触底确认
    out["rev_confirm"] = zscore(stack("ret_60", -1.0)) + zscore(r5 - r20 * 0.25)
    # 高质量反转：超跌 + 波动收敛 + 缩量
    out["rev_quiet"] = (zscore(stack("ret_60", -1.0)) + zscore(stack("vol20", -1.0))
                        + zscore(stack("vol_ratio", -1.0)))
    # 低波动
    out["low_vol"] = stack("vol20", -1.0)
    return out


def ic_table(fs: FeatureStore, cands: Dict[str, np.ndarray], fwd: dict,
             start_i: int, horizons=(10, 20, 40, 60)) -> dict:
    codes = fs.codes
    res = {}
    for H in horizons:
        for name, mat in cands.items():
            ics = []
            for i in range(start_i, len(fs.dates) - H - 1):
                cis = [ci for ci, c in enumerate(codes)
                       if fs.meta.get(c, {}).get("level") in ("L1", "L2")
                       and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]
                if len(cis) < 15:
                    continue
                xs = mat[cis, i]
                ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
                ic = spearman(xs, ys)
                if ic == ic:
                    ics.append(ic)
            if len(ics) < 30:
                continue
            a = np.array(ics)
            m, s = float(a.mean()), float(a.std(ddof=1))
            res[f"{name}|{H}"] = {"metric": name, "horizon": H, "ic_mean": round(m, 4),
                                  "ic_std": round(s, 4), "t": round(m / s * np.sqrt(len(a)), 2) if s else 0,
                                  "pos_ratio": round(float((a > 0).mean()), 3), "n": len(a)}
    return res


def yearly_stability(fs: FeatureStore, cands: Dict[str, np.ndarray], fwd: dict,
                     start_i: int, names: List[str], H: int = 20) -> dict:
    codes = fs.codes
    by_year: Dict[str, Dict[str, list]] = {}
    for i in range(start_i, len(fs.dates) - H - 1):
        yr = fs.dates[i][:4]
        cis = [ci for ci, c in enumerate(codes)
               if fs.meta.get(c, {}).get("level") in ("L1", "L2")
               and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]
        if len(cis) < 15:
            continue
        ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
        for name in names:
            ic = spearman(cands[name][cis, i], ys)
            if ic == ic:
                by_year.setdefault(yr, {}).setdefault(name, []).append(ic)
    out = {}
    for yr in sorted(by_year):
        out[yr] = {nm: round(float(np.mean(v)), 4) for nm, v in by_year[yr].items()
                   if len(v) >= 20}
    return out


def regime_split(fs: FeatureStore, cands: Dict[str, np.ndarray], fwd: dict,
                 regime_series: Dict[str, dict], start_i: int,
                 names: List[str], H: int = 20) -> dict:
    codes = fs.codes
    buckets: Dict[str, Dict[str, list]] = {}
    for i in range(start_i, len(fs.dates) - H - 1):
        rg = regime_series.get(fs.dates[i], {}).get("regime")
        if not rg:
            continue
        cis = [ci for ci, c in enumerate(codes)
               if fs.meta.get(c, {}).get("level") in ("L1", "L2")
               and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]
        if len(cis) < 15:
            continue
        ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
        for name in names:
            ic = spearman(cands[name][cis, i], ys)
            if ic == ic:
                buckets.setdefault(rg, {}).setdefault(name, []).append(ic)
    return {rg: {nm: round(float(np.mean(v)), 4) for nm, v in d.items() if len(v) >= 15}
            for rg, d in buckets.items()}


if __name__ == "__main__":
    import regime as RG
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    rs = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    fwd = HZ.precompute_forward(fs)
    cands = build_candidates(fs)
    start_i = max(C.WARMUP, len(fs.dates) - 900)
    res = ic_table(fs, cands, fwd, start_i)

    print("=== 候选因子 IC（20 日），按 IC 降序 ===")
    rows = sorted([r for r in res.values() if r["horizon"] == 20], key=lambda r: -r["ic_mean"])
    print(f"{'factor':<16}{'IC':>9}{'t':>8}{'正比例':>8}")
    for r in rows:
        print(f"{r['metric']:<16}{r['ic_mean']:>9.4f}{r['t']:>8.2f}{r['pos_ratio']:>8.3f}")

    print("\n=== 关键因子在各周期的表现 ===")
    key = ["rev_60", "rev_20", "rev_rs60", "far_ma60", "dd_deep", "bottom_rebound",
           "exhaust", "rev_confirm", "rev_quiet", "quiet_vol", "low_vol",
           "ret_120", "ret_250", "rev_250"]
    for nm in key:
        line = f"{nm:<16}"
        for H in (10, 20, 40, 60):
            r = res.get(f"{nm}|{H}")
            line += f"{r['ic_mean']:>9.4f}" if r else f"{'--':>9}"
        print(line)

    print("\n=== 分年度稳定性（H=20） ===")
    ys = yearly_stability(fs, cands, fwd, start_i, ["rev_60", "rev_confirm", "exhaust", "bottom_rebound", "rev_quiet"])
    hdr = "year     " + "".join(f"{k:>14}" for k in ["rev_60", "rev_confirm", "exhaust", "bottom_rebound", "rev_quiet"])
    print(hdr)
    for yr, d in ys.items():
        print(f"{yr:<9}" + "".join(f"{d.get(k, float('nan')):>14.4f}" for k in
                                   ["rev_60", "rev_confirm", "exhaust", "bottom_rebound", "rev_quiet"]))

    print("\n=== 按环境分组（H=20） ===")
    rg = regime_split(fs, cands, fwd, rs, start_i, ["rev_60", "rev_confirm", "exhaust", "bottom_rebound"])
    for k, d in rg.items():
        print(k, d)

    json.dump(res, open(f"{C.CACHE_DIR}/ic_diag2.json", "w"), ensure_ascii=False, indent=1)
