# -*- coding: utf-8 -*-
"""趋势相关因子的「新增信息」诊断（只读：不写任何生产产物、不改 config）。

为什么只测这几类
--------------
`docs/行业选错-三方向实验结论-2026-09-24.md` 已证明：对现有分数做**逐日单调变换**
（减当日中位 / 分位化 / 与分位的任意加权混合）**= 0 新增信息** —— 因为 Rank IC 对
逐日单调变换不变。所以唯一可能有增量的候选，必须**改变横截面排序**：

  把「绝对幅度」换成「单位风险的幅度」或「路径形状」——即引入除法 / 交互：
    · 风险调整趋势  ret_120 / vol_60（文献：volatility-managed / risk-managed momentum）
    · 趋势效率      ret_120 / (1 + |maxdd_60|)（同样涨幅，路径回撤浅的更优？）
    · 量价确认      up_vol_share（上涨日的成交量占比，是「价」之外的另一个信息源）
    · 多窗一致性    短中长三窗同号才算「真趋势」
    · 加速度        r5/r20（二阶：趋势在加速还是减速）

口径
----
前瞻超额 ex_ew 与 `review.fwd_stats` 逐位同口径（ret − 行业等权 ret），只是向量化重写；
Rank IC 与 `lab_sector_fix.run_ic` 同口径（逐日横截面 spearman，WARMUP 起，H=80/100）。

输出三张表：
  A 全样本 Rank IC（含 t 值）—— 单因子有没有信息
  B 控制 mainline 后的**组内 IC**（按当日 mainline 三分位分组、组内 spearman 再平均）
    —— 这才是「在现有打分之上有没有**增量**信息」的关键检验
  C 分环境分层 IC —— 趋势因子是否在某些环境下失效

用法：python engine/lab_trend_quality.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
from feature_store import EW_CODE  # noqa: E402

HZS = (80, 100)
MIN_N = 8          # 单日横截面最少样本


def spearman(x: np.ndarray, y: np.ndarray):
    m = ~np.isnan(x) & ~np.isnan(y)
    if m.sum() < MIN_N:
        return None
    rx = pd.Series(x[m]).rank().to_numpy()
    ry = pd.Series(y[m]).rank().to_numpy()
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    d = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    if d == 0:
        return None
    return float((rx * ry).sum() / d)


def ic_table(score: np.ndarray, fwd: np.ndarray, mask_i: np.ndarray | None = None):
    """逐日横截面 Rank IC 的均值 / IR / t / 为正比例。"""
    ics = []
    for i in range(C.WARMUP, fwd.shape[1]):
        if mask_i is not None and not mask_i[i]:
            continue
        v = spearman(score[:, i], fwd[:, i])
        if v is not None:
            ics.append(v)
    if len(ics) < 30:
        return None
    a = np.asarray(ics)
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    ir = mean / sd if sd > 0 else 0.0
    return {"mean": mean, "ir": ir, "t": ir * np.sqrt(len(a)),
            "pos": float((a > 0).mean()), "n": len(a)}


def group_ic(score: np.ndarray, fwd: np.ndarray, ctrl: np.ndarray, n_grp: int = 3,
             mask_i: np.ndarray | None = None):
    """控制 ctrl（mainline）后的组内 Rank IC：每日按 ctrl 三分位分组，组内算 spearman 再等权平均。"""
    ics = []
    for i in range(C.WARMUP, fwd.shape[1]):
        if mask_i is not None and not mask_i[i]:
            continue
        c_i, s_i, f_i = ctrl[:, i], score[:, i], fwd[:, i]
        m = ~np.isnan(c_i) & ~np.isnan(s_i) & ~np.isnan(f_i)
        if m.sum() < n_grp * MIN_N:
            continue
        cv = c_i[m]
        q = np.nanquantile(cv, [1.0 / n_grp, 2.0 / n_grp])
        parts = [m & (c_i <= q[0]), m & (c_i > q[0]) & (c_i <= q[1]), m & (c_i > q[1])]
        vals = []
        for p in parts:
            if p.sum() < MIN_N:
                continue
            v = spearman(np.where(p, s_i, np.nan), np.where(p, f_i, np.nan))
            if v is not None:
                vals.append(v)
        if vals:
            ics.append(float(np.mean(vals)))
    if len(ics) < 30:
        return None
    a = np.asarray(ics)
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    ir = mean / sd if sd > 0 else 0.0
    return {"mean": mean, "t": ir * np.sqrt(len(a)), "n": len(a)}


def main() -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    dims = ctx["dims"]
    regime = ctx["regime"]
    codes = [c for c in fs.codes if c != "EW"]
    n_c, n_d = len(codes), len(fs.dates)
    print(f"样本：{n_c} 个行业/赛道 × {n_d} 个交易日（{fs.dates[0]} → {fs.dates[-1]}）")
    print(f"环境窗口：{min(regime)} → {max(regime)}\n")

    # ---------- 前瞻超额矩阵（与 review.fwd_stats 同口径，向量化）----------
    close = np.vstack([fs.series(c, "close") for c in codes]).astype(float)
    ew = fs.series(EW_CODE, "close").astype(float)
    fwd = {}
    for H in HZS:
        r = close[:, H:] / close[:, :n_d - H] - 1.0
        ewr = ew[H:] / ew[:n_d - H] - 1.0
        M = np.full((n_c, n_d), np.nan)
        M[:, :n_d - H] = r - ewr[None, :]
        fwd[H] = M

    # ---------- 因子矩阵 ----------
    def S(f):
        return np.vstack([fs.series(c, f) for c in codes]).astype(float)

    r5, r20, r60 = S("ret_5"), S("ret_20"), S("ret_60")
    r120, r250 = S("ret_120"), S("ret_250")
    v20, v60 = S("vol20"), S("vol60")
    mdd = S("maxdd_60")
    uvs = S("up_vol_share")
    accel = S("r5_over_r20")
    ddh = S("dd_from_high60")
    pos250 = S("pos250")

    def safe_div(a, b):
        return np.where((b > 1e-9) & ~np.isnan(b), a / b, np.nan)

    minabs = np.minimum(np.abs(r60), np.minimum(np.abs(r120), np.abs(r250)))
    sgn3 = np.sign(r60) * np.sign(r120) * np.sign(r250)
    consistency = np.where(sgn3 > 0, minabs, -minabs)

    F = {
        # --- 基线：现行口径 ---
        "mainline（现行）": dims["mainline"],
        "ret_120（现行分量）": r120,
        "ret_250": r250,
        "ret_60": r60,
        "ret_20（短期，已知反转）": r20,
        # --- 候选：风险调整（破坏单调性）---
        "IR = ret120 / vol60": safe_div(r120, v60),
        "IR = ret120 / vol20": safe_div(r120, v20),
        "IR = ret250 / vol60": safe_div(r250, v60),
        # --- 候选：路径质量 ---
        "效率 = ret120 / (1+|maxdd60|)": safe_div(r120, 1.0 + np.abs(mdd)),
        "maxdd_60（路径回撤）": mdd,
        "贴近60日高点 dd_from_high60": ddh,
        "pos250（250日区间位置）": pos250,
        # --- 候选：量价 / 结构 ---
        "量价确认 up_vol_share": uvs,
        "三窗一致性 consistency": consistency,
        "加速度 r5/r20": accel,
    }

    # ---------- A. 全样本 Rank IC ----------
    print("=" * 96)
    print("A. 全样本横截面 Rank IC（逐日 spearman；ex_ew 前瞻超额；WARMUP 起）")
    print(f"{'因子':<32}{'IC@80':>9}{'t@80':>8}{'+|@80':>8}{'IC@100':>9}{'t@100':>8}{'+|@100':>8}")
    print("-" * 96)
    A = {}
    for name, mat in F.items():
        cells = f"{name:<32}"
        row = {}
        for H in HZS:
            r = ic_table(mat, fwd[H])
            A[(name, H)] = r
            if r:
                cells += f"{r['mean']:>9.3f}{r['t']:>8.1f}{r['pos']:>8.1%}"
                row[H] = r
            else:
                cells += f"{'—':>9}{'—':>8}{'—':>8}"
        print(cells)

    # ---------- B. 控制 mainline 的组内 IC（增量检验）----------
    print()
    print("=" * 96)
    print("B. 控制 mainline 后的组内 IC（每日按 mainline 三分位分组、组内 spearman 再平均）")
    print("   —— 显著为正 = 该因子在「同样强的趋势」内部还能再分好坏（真增量）")
    print(f"{'因子':<32}{'组内IC@80':>11}{'t@80':>8}{'组内IC@100':>11}{'t@100':>8}")
    print("-" * 96)
    ml = dims["mainline"]
    for name, mat in F.items():
        if name.startswith("mainline"):
            continue
        cells = f"{name:<32}"
        for H in HZS:
            g = group_ic(mat, fwd[H], ml)
            cells += (f"{g['mean']:>11.3f}{g['t']:>8.1f}" if g else f"{'—':>11}{'—':>8}")
        print(cells)

    # ---------- C. 分环境分层 IC（只看 mainline 与最强候选）----------
    print()
    print("=" * 96)
    print("C. 分环境分层 IC（按当日 regime；用于判断「趋势因子在高波动/防守市是否失效」）")
    reg_of = np.array([(regime.get(d) or {}).get("regime") for d in fs.dates], dtype=object)
    sel = ["mainline（现行）", "ret_120（现行分量）", "IR = ret120 / vol60",
           "量价确认 up_vol_share", "三窗一致性 consistency"]
    print(f"{'因子':<32}" + "".join(f"{r:>12}" for r in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC")))
    print("-" * 96)
    for name in sel:
        mat = F[name]
        cells = f"{name:<32}"
        for rg in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC"):
            mask = reg_of == rg
            mask = np.array([bool(m) for m in mask])
            r = ic_table(mat, fwd[100], mask)
            cells += (f"{r['mean']:>9.3f}({r['n']//10:>2})" if r else f"{'—':>12}")
        print(cells)

    # ---------- D. 候选与 mainline 的横截面秩相关（判断是否只是同一信息的换装）----------
    print()
    print("=" * 96)
    print("D. 与 mainline 的逐日横截面秩相关（过高 = 只是 mainline 的换装，无增量空间）")
    for name, mat in F.items():
        if name.startswith("mainline"):
            continue
        cs = []
        for i in range(C.WARMUP, n_d):
            v = spearman(mat[:, i], ml[:, i])
            if v is not None:
                cs.append(v)
        if cs:
            print(f"  {name:<34} 平均秩相关 {np.mean(cs):+.3f}")

    # ---------- E. 逐年稳定性（防「单年耀眼」：组内增量必须跨年普遍）----------
    print()
    print("=" * 96)
    print("E. 逐年 IC / 组内 IC（H=100）—— 看「增量」是跨年普遍还是单年驱动")
    key = ["mainline（现行）", "ret_120（现行分量）", "pos250（250日区间位置）", "IR = ret120 / vol60"]
    years = sorted({d[:4] for d in fs.dates[C.WARMUP:-1] if d >= "2018"})
    print(f"{'因子':<30}" + "".join(f"{y[2:]:>9}" for y in years))
    print("-" * 96)
    for nm in key:
        mat = F[nm]
        cells = f"{nm:<30}"
        for y in years:
            mk = np.array([d[:4] == y for d in fs.dates])
            r = ic_table(mat, fwd[100], mk)
            cells += (f"{r['mean']:>9.3f}" if r else f"{'—':>9}")
        print(cells)
    print(f"{'（组内，控制 mainline）':<28}" + "".join(f"{y[2:]:>9}" for y in years))
    for nm in key[2:]:
        cells = f"{nm:<30}"
        for y in years:
            mk = np.array([d[:4] == y for d in fs.dates])
            g = group_ic(F[nm], fwd[100], ml, mask_i=mk)
            cells += (f"{g['mean']:>9.3f}" if g else f"{'—':>9}")
        print(cells)

    # ---------- F. 组合因子：mainline + pos250 + IR（横截面 z 标准化后线性组合）----------
    def zs(mat):
        m = np.nanmean(mat, axis=0, keepdims=True)
        s = np.nanstd(mat, axis=0, keepdims=True)
        return np.where(s > 1e-12, (mat - m) / s, np.nan)

    print()
    print("=" * 96)
    print("F. 组合因子（同日横截面 z 标准化后加权；多因子组合不是任何单因子的单调变换）")
    combos = {
        "现行 mainline（对照）": ml,
        "0.7·ml + 0.3·pos250": 0.7 * zs(ml) + 0.3 * zs(pos250),
        "0.7·ml + 0.3·IR": 0.7 * zs(ml) + 0.3 * zs(F["IR = ret120 / vol60"]),
        "0.6·ml + 0.2·pos250 + 0.2·IR": 0.6 * zs(ml) + 0.2 * zs(pos250) + 0.2 * zs(F["IR = ret120 / vol60"]),
    }
    print(f"{'组合':<34}{'IC@80':>9}{'t@80':>7}{'IC@100':>9}{'t@100':>7}")
    print("-" * 96)
    for nm, mat in combos.items():
        cells = f"{nm:<34}"
        for H in HZS:
            r = ic_table(mat, fwd[H])
            cells += (f"{r['mean']:>9.3f}{r['t']:>7.1f}" if r else f"{'—':>9}{'—':>7}")
        print(cells)
    print()


if __name__ == "__main__":
    main()
