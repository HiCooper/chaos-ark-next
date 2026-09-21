# -*- coding: utf-8 -*-
"""第三轮诊断：确认「长期主线动量 + 短期回调反转」的双层结构。

关键问题：
  Q1 120/250 日动量的 IC 是否分年度稳定？（避免单一行情驱动的假象）
  Q2 分组收益是否单调？（IC 高不等于可交易）
  Q3 短期反转是否只在「长期上升趋势内」有效？（主线回调 vs 接飞刀）
  Q4 组合因子的效果是否优于单因子？
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
from diag2 import build_candidates, zscore  # noqa: E402


def long_momentum(fs: FeatureStore, H: int) -> np.ndarray:
    codes = fs.codes
    n = len(fs.dates)
    m = np.vstack([fs.series(c, "close") for c in codes])
    r = np.full_like(m, np.nan)
    if n > H:
        r[:, :-H] = m[:, H:] / m[:, :-H] - 1.0
    return r


def decile_analysis(fs: FeatureStore, factor: np.ndarray, fwd: dict,
                    start_i: int, H: int = 40, ngrp: int = 5) -> dict:
    """按因子值分 5 组，统计各组未来 H 日相对 EW 的平均超额收益。"""
    codes = fs.codes
    grp_ret: Dict[int, list] = {}
    for i in range(start_i, len(fs.dates) - H - 1):
        cis = [ci for ci, c in enumerate(codes)
               if fs.meta.get(c, {}).get("level") in ("L1", "L2")
               and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]
        if len(cis) < 15:
            continue
        xs = factor[cis, i]
        ok = ~np.isnan(xs)
        cis = [cis[k] for k in range(len(cis)) if ok[k]]
        xs = xs[ok]
        if len(cis) < 15:
            continue
        ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
        order = np.argsort(xs)
        size = len(order) // ngrp
        for g in range(ngrp):
            sl = order[g * size:(g + 1) * size] if g < ngrp - 1 else order[g * size:]
            grp_ret.setdefault(g, []).append(float(np.mean(ys[sl])))
    return {f"G{g+1}": {"avg_excess": round(float(np.mean(v)), 4), "n_days": len(v)}
            for g, v in sorted(grp_ret.items())}


def conditional_ic(fs: FeatureStore, cond: np.ndarray, factor: np.ndarray,
                   fwd: dict, start_i: int, H: int = 40,
                   cond_bands=(-np.inf, -0.10, 0.05, np.inf)) -> dict:
    """在「长期动量高/中/低」三个条件下，分别检验短期反转因子的 IC。"""
    codes = fs.codes
    buckets: Dict[str, list] = {"长期弱势(< -10%)": [], "长期震荡(-10%~+5%)": [], "长期强势(> +5%)": []}
    names = list(buckets.keys())
    for i in range(start_i, len(fs.dates) - H - 1):
        cis = [ci for ci, c in enumerate(codes)
               if fs.meta.get(c, {}).get("level") in ("L1", "L2")
               and fs.series(c, "ret_60")[i] == fs.series(c, "ret_60")[i]]
        if len(cis) < 15:
            continue
        cv = cond[cis, i]
        fv = factor[cis, i]
        ys = np.array([fwd["ex_ew"][(codes[ci], H)][i] for ci in cis])
        for bi in range(3):
            lo, hi = cond_bands[bi], cond_bands[bi + 1]
            m = (cv >= lo) & (cv < hi) & (~np.isnan(cv)) & (~np.isnan(fv))
            if m.sum() < 8:
                continue
            ic = spearman(fv[m], ys[m])
            if ic == ic:
                buckets[names[bi]].append(ic)
    return {k: {"ic_mean": round(float(np.mean(v)), 4), "n_days": len(v)}
            for k, v in buckets.items() if len(v) >= 30}


if __name__ == "__main__":
    import regime as RG
    fs = FeatureStore.load(f"{C.CACHE_DIR}/features.npz")
    rs = json.load(open(f"{C.CACHE_DIR}/regime.json"))
    fwd = HZ.precompute_forward(fs)
    cands = build_candidates(fs)
    start_i = max(260, len(fs.dates) - 900)

    m120 = long_momentum(fs, 120)
    m250 = long_momentum(fs, 250)
    cands["ret_120"] = m120
    cands["ret_250"] = m250

    # Q1 年度稳定性
    print("=== Q1 长期动量的分年度 IC（H=40） ===")
    from diag2 import yearly_stability
    ys = yearly_stability(fs, cands, fwd, start_i, ["ret_120", "ret_250", "rev_60", "rev_20"], H=40)
    print(f"{'year':<8}{'ret_120':>10}{'ret_250':>10}{'rev_60':>10}{'rev_20':>10}")
    for yr, d in ys.items():
        print(f"{yr:<8}{d.get('ret_120', float('nan')):>10.4f}{d.get('ret_250', float('nan')):>10.4f}"
              f"{d.get('rev_60', float('nan')):>10.4f}{d.get('rev_20', float('nan')):>10.4f}")

    # Q2 分组单调性
    print("\n=== Q2 分组未来超额收益（H=40，G1=因子值最低组） ===")
    for nm, fac in (("ret_120", m120), ("ret_250", m250), ("rev_60", cands["rev_60"])):
        d = decile_analysis(fs, fac, fwd, start_i, H=40)
        line = "  ".join(f"{k}:{v['avg_excess']*100:+.2f}%" for k, v in d.items())
        print(f"{nm:<10} {line}")

    # Q3 条件 IC：短期反转在长期上升/下降趋势中是否不同
    print("\n=== Q3 短期反转因子的条件 IC（H=40，按长期 250 日动量分组） ===")
    for nm in ("rev_20", "rev_60", "exhaust", "bottom_rebound"):
        r = conditional_ic(fs, m250, cands[nm], fwd, start_i, H=40)
        print(f"{nm:<16}" + "  ".join(f"{k}={v['ic_mean']:+.4f}({v['n_days']})" for k, v in r.items()))

    # Q4 组合因子
    print("\n=== Q4 组合因子 IC（H=20/40/60） ===")
    combos = {
        "combo_mainline_pullback": zscore(m120) + zscore(cands["rev_20"]),
        "combo_mainline_pullback60": zscore(m120) + zscore(cands["rev_60"]),
        "combo_mainline_exhaust": zscore(m120) + zscore(cands["exhaust"]),
        "combo_mainline_only": zscore(m120),
        "combo_ml250_pullback": zscore(m250) + zscore(cands["rev_20"]),
        "combo_ml_dd": zscore(m120) + zscore(cands["dd_deep"]),
        "combo_ml_quiet": zscore(m120) + zscore(cands["quiet_vol"]),
        "combo_triple": zscore(m120) + 0.7 * zscore(cands["rev_20"]) + 0.5 * zscore(cands["exhaust"]),
    }
    allc = dict(cands)
    allc.update(combos)
    from diag2 import ic_table
    res = ic_table(fs, allc, fwd, start_i, horizons=(20, 40, 60))
    print(f"{'factor':<26}{'H=20':>9}{'H=40':>9}{'H=60':>9}")
    for nm in list(combos.keys()) + ["ret_120", "rev_20", "exhaust"]:
        row = f"{nm:<26}"
        for H in (20, 40, 60):
            r = res.get(f"{nm}|{H}")
            row += f"{r['ic_mean']:>9.4f}" if r else f"{'--':>9}"
        print(row)

    # 组合因子的分组收益
    print("\n=== 组合因子分组收益（H=40） ===")
    for nm, fac in combos.items():
        d = decile_analysis(fs, fac, fwd, start_i, H=40)
        line = "  ".join(f"{k}:{v['avg_excess']*100:+.2f}%" for k, v in d.items())
        print(f"{nm:<26}{line}")

    json.dump(res, open(f"{C.CACHE_DIR}/ic_diag3.json", "w"), ensure_ascii=False, indent=1)
