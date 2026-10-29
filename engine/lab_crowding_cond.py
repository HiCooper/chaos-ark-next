# -*- coding: utf-8 -*-
"""条件化拥挤度（方向 1 收口）—— 判定「只在弱市启用拥挤度」是否值得落参。

背景与承接
--------------------------------------------------------------------------------
`lab_crowding.py` 表 7/8 已给出**环境内 IC** 的 Δ（同族合成 + T-1 regime：
仅 PANIC +0.0124 / DEFEND+PANIC +0.0123 / ATTACK+NEUTRAL −0.0010，886~1574 日）。
但落参决策还缺三块决定性证据：
    ① **全局净效果** —— 表 7/8 只报环境内，看不出对全样本的净影响；
    ② **参数档稳健性** —— 条件化只在 W=250 / w=0.2~0.3 上试过，未验准则②③；
    ③ **组合层兑现** —— IC 增量 ≠ cagr 增量（2024 那轮 G2 教训）。
本脚本补齐 ①②（含逐年），③ 由 `lab_crowding_replay.py` 负责。

两个方法学修正（相对表 7/8）
--------------------------------------------------------------------------------
1. **硬条件化**：环境外**完全不碰** composite，而不是「把拥挤度置中性 50」。
   置中性会改变环境外的**数值**（0.7·comp + 0.3·50 = 0.7·comp + 15）——排序虽不变，
   却会抬高 composite 的绝对水平、挤动 `classify` 的**绝对阈值**
   （`buy_now_min` / `ambush_min`），在环境外制造出一个看不见的副作用。
   硬条件化下环境外的 composite **逐字节等于基线**，副作用严格为 0（表 3 给断言）。

2. **保分布秩混合**：环境内按「横截面秩」混合，再把结果映射回当日 composite 的
   经验分布。理由：composite 是 0~100 的**窄分布打分，且绝对值带阈值语义**；
   直接线性混合（`lab_crowding` 表 2）会同时改变分布形状与均值，把「排序改善」
   和「阈值被挤动」混在一起、无法分辨。保分布混合让混合前后的分位结构完全一致，
   **只改排序**。

输出 7 张表（只读，不写任何生产产物）：
    1  条件化实现对比（硬 vs 置中性）—— 全局净效果
    2  窗口档 × 条件集合扫描（准则②：方向跨档一致）
    3  权重档扫描 + 环境外不变性断言（准则③：响应单调）
    4  逐年全局 IC（防单年耀眼）
    5  单指标 vs 同族合成（条件化下）
    6  分环境明细（环境内 IC 与分组差）
    7  T-1 regime 稳健性
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import lab_valuation as LV  # noqa: E402
import lab_crowding as LC  # noqa: E402
from feature_store import EW_CODE  # noqa: E402

HZS = (20, 80, 100)
MIN_N = 8
WINDOWS = (60, 120, 250, 500)
WEIGHTS = (0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.70, 1.00)
W_MAIN = 250
W_REF = 0.30
LINE = "=" * 112

ENV_SETS = (
    (("PANIC",), "仅PANIC"),
    (("DEFEND", "PANIC"), "DEFEND+PANIC"),
    (("DEFEND", "PANIC", "NEUTRAL"), "DEF+PAN+NEU"),
    (("ATTACK", "NEUTRAL"), "ATTACK+NEU(证伪)"),
)


def _f(d, key="mean", nd=3) -> str:
    if not d or d.get(key) is None:
        return "n/a"
    v = d[key]
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _pct(d, key="spread") -> str:
    v = (d or {}).get(key)
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def quantile_map(rank01: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """把 0~1 的秩映射到 ref 的经验分布上 —— 「保分布」的关键。

    线性插值而非取整，避免 120 个行业里出现大量并列值（并列会削弱排序信息）。
    """
    v = np.sort(ref[~np.isnan(ref)])
    m = len(v)
    if m < 2:
        return np.full(rank01.shape, np.nan)
    pos = np.clip(rank01, 0.0, 1.0) * (m - 1)
    lo = np.floor(pos).astype(int)
    hi = np.minimum(lo + 1, m - 1)
    frac = pos - lo
    return v[lo] * (1.0 - frac) + v[hi] * frac


def _rank01(col: np.ndarray) -> np.ndarray:
    """单列横截面秩分位（0~1）。"""
    out = np.full(col.shape, np.nan)
    mk = ~np.isnan(col)
    if mk.sum() < 2:
        return out
    out[mk] = pd.Series(col[mk]).rank(pct=True).to_numpy()
    return out


def build_cond_comp(comp: np.ndarray, comp_r: np.ndarray, crowd_r: np.ndarray,
                    env_mask: np.ndarray, l12_mask: np.ndarray, w: float,
                    hard: bool = True) -> np.ndarray:
    """条件化合成 composite。

    hard=True ：环境外逐字节保留 comp（推荐）
    hard=False：环境外把拥挤度置中性（复现 lab_crowding 表 7/8 的口径）

    ⚠️ 实现要点（踩过的坑）：混合后必须**重新秩化**再映射回 comp 的经验分布。
    否则两个均匀秩的加权和是「三角分布」（集中在中位），映射后值挤向中心、
    尾部变薄 → 过 64/56 阈值的候选变少，成交被人工压低（实测 345 → 127）。
    重新秩化保证输出恒为均匀分位 ⇒ 映射后的值分布与 comp 完全一致，只改排序。
    """
    out = comp.copy()
    for i in range(comp.shape[1]):
        cr = crowd_r[:, i]
        if not env_mask[i]:
            if hard:
                continue
            cr = np.full(comp.shape[0], 0.5)          # 环境外置中性秩
        else:
            cr = np.where(np.isnan(cr), 0.5, cr)
        mix_r = _rank01((1.0 - w) * comp_r[:, i] + w * cr)
        mk = ~np.isnan(mix_r)
        out[mk, i] = quantile_map(mix_r[mk], comp[l12_mask, i])
    return out


def env_mask_of(regime, dates, envs_on, lag: int = 0) -> np.ndarray:
    n = len(dates)
    return np.array([(regime.get(dates[max(0, i - lag)]) or {}).get("regime") in envs_on
                     for i in range(n)])


def main() -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    dims = ctx["dims"]
    regime = ctx["regime"]
    n_d = len(fs.dates)

    codes = [c for c in fs.codes
             if c != EW_CODE and fs.meta.get(c, {}).get("level") in ("L1", "L2")]
    l12_mask = np.array([c in set(codes) for c in fs.codes])
    row_idx = [fs.codes.index(c) for c in codes]
    n_c = len(codes)

    comp = dims["composite"]
    # comp_r 必须在 L1/L2 **内部**算秩：quantile_map 的参考分布也是 L1/L2，
    # 若秩在 126 行（含 __EW__ / L3）上算、参考分布却取 120 行，两者不同源。
    comp_r = np.full_like(comp, np.nan)
    comp_r[l12_mask, :] = LC.xs_rank(comp[l12_mask, :])

    print(LINE)
    print(f"样本：{n_c} 行业 × {n_d} 交易日（{fs.dates[0]} → {fs.dates[-1]}）  "
          f"引擎 {PL.ENGINE_VERSION}")
    print(f"composite 是唯一的注入点：classify_day 从 ctx['comp'] 取分数、"
          f"从 dims['mainline'] 取回避阈值 → 只改排序不改「谁被回避」")
    print(LINE)

    # ---- 前瞻超额（与 lab_crowding / review.fwd_stats 同构）
    close = np.vstack([fs.series(c, "close") for c in codes]).astype(float)
    ew = fs.series(EW_CODE, "close").astype(float)
    fwd = {}
    for H in HZS:
        r = close[:, H:] / close[:, :n_d - H] - 1.0
        ewr = ew[H:] / ew[:n_d - H] - 1.0
        M = np.full((n_c, n_d), np.nan)
        M[:, :n_d - H] = r - ewr[None, :]
        fwd[H] = M

    V, val_days, missing = LV.load_val_panel(codes, fs.dates)
    turnover, sumrate = V["turnover"], V["sumrate"]

    # ---- 拥挤度原语（与 lab_crowding 同口径）
    tp = {W: LV.trailing_pct(turnover, W, int(W * 0.6)) for W in WINDOWS}
    crowd = {W: 100.0 * (1.0 - tp[W]) for W in WINDOWS}
    log_t = np.log(np.where(turnover > 0, turnover, np.nan))
    tchg60 = LV.diff_n(log_t, 60)
    parts = [LC.xs_rank(-tp[W_MAIN]), LC.xs_rank(-tchg60), LC.xs_rank(-sumrate)]
    syn = 100.0 * np.nanmean(np.stack(parts), axis=0)

    def to_full(mat_l12: np.ndarray) -> np.ndarray:
        """把 L1/L2 池的矩阵嵌回全 codes 行序（非 L1/L2 行填 NaN）。"""
        full = np.full((len(fs.codes), n_d), np.nan)
        full[l12_mask, :] = mat_l12
        return full

    # 拥挤度秩（全 codes 形状，L1/L2 之外为 NaN —— 不会被写入，因为 classify 只看 L1/L2）
    crowd_r = {W: to_full(LC.xs_rank(crowd[W])) for W in WINDOWS}
    crowd_r["syn"] = to_full(LC.xs_rank(syn))

    base_r = LV.ic_table(comp[row_idx, :], fwd[100])
    base_sp100 = LV.group_spread(comp[row_idx, :], fwd[100])
    base_sp20 = LV.group_spread(comp[row_idx, :], fwd[20])
    print(f"\n基线 composite：IC@20 {_f(LV.ic_table(comp[row_idx, :], fwd[20]))} / "
          f"@80 {_f(LV.ic_table(comp[row_idx, :], fwd[80]))} / @100 {_f(base_r)}"
          f"（t={_f(base_r, 't')}）  分组差@20 {_pct(base_sp20)} / @100 {_pct(base_sp100)}")
    print(f"换手率面板覆盖率 {np.nanmean(~np.isnan(turnover)):.1%}；"
          f"有数据行业 {int((val_days > 0).sum())}/{n_c}；缺数据 {len(missing)} 个")

    def report(score_full: np.ndarray, tag: str) -> dict:
        """全局净效果评估（一行）。Δ 一律相对 composite 基线、同一样本。"""
        s = score_full[row_idx, :]
        ic20, ic80, ic100 = (LV.ic_table(s, fwd[h]) for h in HZS)
        gi = LV.group_ic(s, fwd[100], comp[row_idx, :])
        sp20, sp100 = LV.group_spread(s, fwd[20]), LV.group_spread(s, fwd[100])
        d_ic = (ic100["mean"] - base_r["mean"]) if (ic100 and base_r) else None
        d_sp = (sp100["spread"] - base_sp100["spread"]) if (sp100 and base_sp100) else None
        rec = {"tag": tag, "ic20": ic20, "ic80": ic80, "ic100": ic100, "gi": gi,
               "sp20": sp20, "sp100": sp100, "d_ic": d_ic, "d_sp": d_sp,
               "hi100": sp100["hi"] if sp100 else None}
        hit = f"{rec['hi100'] * 100:+.2f}%" if rec["hi100"] is not None else "n/a"
        dit = f"{d_ic:+.4f}" if d_ic is not None else "n/a"
        dsp = f"{d_sp * 100:+.2f}%" if d_sp is not None else "n/a"
        print(f"{tag:<34}{_f(ic20):>8}{_f(ic80):>8}{_f(ic100):>8}{dit:>10}"
              f"{_f(gi):>9}{_pct(sp20):>9}{_pct(sp100):>10}{dsp:>10}{hit:>11}")
        return rec

    def header() -> None:
        print(f"{'配置':<34}{'IC@20':>8}{'IC@80':>8}{'IC@100':>8}{'ΔIC@100':>10}"
              f"{'组内IC':>9}{'组差@20':>9}{'组差@100':>10}{'Δ组差':>10}{'高分组@100':>11}")

    # ================= 1. 条件化实现对比 =================
    m_dp = env_mask_of(regime, fs.dates, ("DEFEND", "PANIC"))
    m_panic = env_mask_of(regime, fs.dates, ("PANIC",))
    print()
    print(LINE)
    print("1. 条件化实现对比 —— 全局净效果（不是环境内）。硬条件化 vs 置中性")
    print(f"   DEFEND+PANIC 占 {m_dp.sum()} 日（{m_dp.sum() / n_d:.0%}）"
          f"；仅 PANIC 占 {m_panic.sum()} 日（{m_panic.sum() / n_d:.0%}）")
    print("-" * 112)
    header()
    report(comp, "composite（基线）")
    # 全局加（无条件下）
    cb_glob = build_cond_comp(comp, comp_r, crowd_r["syn"], np.ones(n_d, bool),
                              l12_mask, W_REF, hard=True)
    report(cb_glob, f"全局加·同族合成(w={W_REF})")
    # 软条件化（置中性）—— 复现表 7/8 口径
    cb_soft = build_cond_comp(comp, comp_r, crowd_r["syn"], m_dp, l12_mask, W_REF, hard=False)
    report(cb_soft, f"软条件化(置中性) DEF+PAN(w={W_REF})")
    # 硬条件化
    for envs, label in ((("PANIC",), "仅PANIC"), (("DEFEND", "PANIC"), "DEFEND+PANIC"),
                        (("DEFEND", "PANIC", "NEUTRAL"), "DEF+PAN+NEUTRAL")):
        m = env_mask_of(regime, fs.dates, envs)
        cc = build_cond_comp(comp, comp_r, crowd_r["syn"], m, l12_mask, W_REF, hard=True)
        report(cc, f"硬条件化 {label}(w={W_REF})")

    # ================= 2. 窗口档 × 条件集合 =================
    print()
    print(LINE)
    print("2. 窗口档扫描（硬条件化，w=0.30）—— 准则②：方向跨档是否一致")
    print("   ΔIC/Δ组差 均为「相对 composite 基线」的全样本差；日数=因子有值的交易日")
    print("-" * 112)
    print(f"{'条件集合':<14}{'W':>5}{'全局IC@100':>12}{'ΔIC':>10}{'组差@100':>11}"
          f"{'Δ组差':>10}{'环境内IC':>10}{'Δ环境内':>10}{'有效日':>8}")
    for envs, label in ENV_SETS:
        m = env_mask_of(regime, fs.dates, envs)
        for W in WINDOWS:
            cc = build_cond_comp(comp, comp_r, crowd_r[W], m, l12_mask, W_REF, hard=True)
            s = cc[row_idx, :]
            ic = LV.ic_table(s, fwd[100])
            sp = LV.group_spread(s, fwd[100])
            ic_on = LV.ic_table(s, fwd[100], m)
            ic_b_on = LV.ic_table(comp[row_idx, :], fwd[100], m)
            d_on = (ic_on["mean"] - ic_b_on["mean"]) if (ic_on and ic_b_on) else None
            d_ic = (ic["mean"] - base_r["mean"]) if (ic and base_r) else None
            d_sp = (sp["spread"] - base_sp100["spread"]) if (sp and base_sp100) else None
            n_ok = int((m & ((~np.isnan(tp[W])).sum(axis=0) >= MIN_N)).sum())
            print(f"{label:<14}{W:>5}{_f(ic):>12}"
                  f"{(f'{d_ic:+.4f}' if d_ic is not None else 'n/a'):>10}"
                  f"{_pct(sp):>11}"
                  f"{(f'{d_sp * 100:+.2f}%' if d_sp is not None else 'n/a'):>10}"
                  f"{_f(ic_on):>10}"
                  f"{(f'{d_on:+.4f}' if d_on is not None else 'n/a'):>10}{n_ok:>8}")

    # ================= 3. 权重档 + 环境外不变性断言 =================
    print()
    print(LINE)
    print(f"3. 权重档扫描（硬条件化，同族合成，W={W_MAIN}，DEFEND+PANIC）—— 准则③：响应单调")
    print("-" * 112)
    print(f"{'w':<8}{'全局IC@100':>12}{'ΔIC':>10}{'组差@100':>11}{'Δ组差':>10}"
          f"{'高分组@100':>11}{'环境内IC':>10}{'Δ环境内':>10}{'环境外变动':>12}")
    for w in WEIGHTS:
        cc = build_cond_comp(comp, comp_r, crowd_r["syn"], m_dp, l12_mask, w, hard=True)
        s = cc[row_idx, :]
        ic = LV.ic_table(s, fwd[100])
        sp = LV.group_spread(s, fwd[100])
        ic_on = LV.ic_table(s, fwd[100], m_dp)
        ic_b_on = LV.ic_table(comp[row_idx, :], fwd[100], m_dp)
        d_on = (ic_on["mean"] - ic_b_on["mean"]) if (ic_on and ic_b_on) else None
        d_ic = (ic["mean"] - base_r["mean"]) if (ic and base_r) else None
        d_sp = (sp["spread"] - base_sp100["spread"]) if (sp and base_sp100) else None
        off = ~m_dp
        a, b = cc[:, off], comp[:, off]
        chg = int(np.sum((a != b) & ~(np.isnan(a) & np.isnan(b))))
        tot = int(np.sum(~np.isnan(comp[:, off])))
        hit = f"{sp['hi'] * 100:+.2f}%" if sp else "n/a"
        print(f"{w:<8.2f}{_f(ic):>12}"
              f"{(f'{d_ic:+.4f}' if d_ic is not None else 'n/a'):>10}"
              f"{_pct(sp):>11}"
              f"{(f'{d_sp * 100:+.2f}%' if d_sp is not None else 'n/a'):>10}"
              f"{hit:>11}"
              f"{_f(ic_on):>10}"
              f"{(f'{d_on:+.4f}' if d_on is not None else 'n/a'):>10}"
              f"{f'{chg}/{tot}':>12}")

    # ================= 4. 逐年 =================
    print()
    print(LINE)
    print(f"4. 逐年全局 IC@100（硬条件化，同族合成，DEFEND+PANIC，w={W_REF}）")
    print("-" * 112)
    cc_dp = build_cond_comp(comp, comp_r, crowd_r["syn"], m_dp, l12_mask, W_REF, hard=True)
    yrs = [y for y in sorted(set(d[:4] for d in fs.dates)) if y >= "2016"]
    print(f"{'因子':<28}" + "".join(f"{y[2:]:>8}" for y in yrs) + f"{'正/负':>9}")
    for nm, mat in (("composite（基线）", comp),
                    (f"硬条件化 DEF+PAN w={W_REF}", cc_dp),
                    (f"硬条件化 仅PANIC w={W_REF}", None)):
        if mat is None:
            m = env_mask_of(regime, fs.dates, ("PANIC",))
            mat = build_cond_comp(comp, comp_r, crowd_r["syn"], m, l12_mask, W_REF, hard=True)
        vals = []
        for y in yrs:
            mi = np.array([d[:4] == y for d in fs.dates])
            t = LV.ic_table(mat[row_idx, :], fwd[100], mi)
            vals.append(None if not t else t["mean"])
        cells = "".join(f"{(v if v is not None else np.nan):>8.3f}" for v in vals)
        npos = sum(1 for v in vals if v is not None and v > 0)
        nneg = sum(1 for v in vals if v is not None and v <= 0)
        print(f"{nm:<28}{cells}{f'{npos}/{nneg}':>9}")

    # ================= 5. 单指标 vs 合成 =================
    print()
    print(LINE)
    print(f"5. 单指标 vs 同族合成（硬条件化 DEFEND+PANIC，w={W_REF}）—— 合成是否真的更强")
    print("-" * 112)
    header()
    report(comp, "composite（基线）")
    for key, label in ((250, "单指标 W=250"), (500, "单指标 W=500"), ("syn", "同族三指标合成")):
        cc = build_cond_comp(comp, comp_r, crowd_r[key], m_dp, l12_mask, W_REF, hard=True)
        report(cc, f"硬条件化 {label}(w={W_REF})")

    # ================= 6. 分环境明细 =================
    print()
    print(LINE)
    print(f"6. 分环境明细（硬条件化 DEFEND+PANIC，同族合成，w={W_REF}）")
    print("   「环境内 Δ」是条件化的**收益来源**；其余环境按定义 Δ=0（硬条件化）")
    print("-" * 112)
    print(f"{'环境':<12}{'日数':>7}{'基线IC':>10}{'条件化IC':>11}{'Δ':>10}"
          f"{'基线组差@100':>14}{'条件化组差':>12}{'Δ组差':>10}")
    for e in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC"):
        m = env_mask_of(regime, fs.dates, (e,))
        s = cc_dp[row_idx, :]
        ic_b = LV.ic_table(comp[row_idx, :], fwd[100], m)
        ic_c = LV.ic_table(s, fwd[100], m)
        sp_b = LV.group_spread(comp[row_idx, :], fwd[100], mask_i=m)
        sp_c = LV.group_spread(s, fwd[100], mask_i=m)
        d_ic = (ic_c["mean"] - ic_b["mean"]) if (ic_c and ic_b) else None
        d_sp = (sp_c["spread"] - sp_b["spread"]) if (sp_c and sp_b) else None
        print(f"{e:<12}{int(m.sum()):>7}{_f(ic_b):>10}{_f(ic_c):>11}"
              f"{(f'{d_ic:+.4f}' if d_ic is not None else 'n/a'):>10}"
              f"{_pct(sp_b):>14}{_pct(sp_c):>12}"
              f"{(f'{d_sp * 100:+.2f}%' if d_sp is not None else 'n/a'):>10}")

    # ================= 7. T-1 regime 稳健性 =================
    print()
    print(LINE)
    print("7. T-1 regime 稳健性（生产 exposure_cap 用的是「前一日 regime」，口径需对齐）")
    print("-" * 112)
    print(f"{'lag':<8}{'条件集合':<16}{'全局IC@100':>12}{'ΔIC':>10}{'组差@100':>11}{'Δ组差':>10}")
    for lag, tag in ((0, "当日"), (1, "T-1")):
        for envs, label in ((("PANIC",), "仅PANIC"), (("DEFEND", "PANIC"), "DEFEND+PANIC")):
            m = env_mask_of(regime, fs.dates, envs, lag=lag)
            cc = build_cond_comp(comp, comp_r, crowd_r["syn"], m, l12_mask, W_REF, hard=True)
            s = cc[row_idx, :]
            ic = LV.ic_table(s, fwd[100])
            sp = LV.group_spread(s, fwd[100])
            d_ic = (ic["mean"] - base_r["mean"]) if (ic and base_r) else None
            d_sp = (sp["spread"] - base_sp100["spread"]) if (sp and base_sp100) else None
            print(f"{tag:<8}{label:<16}{_f(ic):>12}"
                  f"{(f'{d_ic:+.4f}' if d_ic is not None else 'n/a'):>10}"
                  f"{_pct(sp):>11}"
                  f"{(f'{d_sp * 100:+.2f}%' if d_sp is not None else 'n/a'):>10}")

    print()
    print(LINE)
    print("完成。组合层兑现见 lab_crowding_replay.py（cagr / maxdd / Sharpe 是否同向改善）。")


if __name__ == "__main__":
    main()
