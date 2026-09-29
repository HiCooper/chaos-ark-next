# -*- coding: utf-8 -*-
"""拥挤度深入研究（方向 1）—— 判定「低换手分位」是否值得落参。

⚠️ 本脚本纠正上一轮的一个口径错误
--------------------------------------------------------------------------------
`docs/估值与基本面信息源实验-2026-09-29.md` 里所有对照都写成「mainline（现行）」，
但 `config.BASE_WEIGHTS` 里 mainline 只占 **0.34** —— 生产跑的是六维加权 `composite`。
更关键的是：六维里的 `exhaust`（权重 0.12）构成是
    exhaust = 0.50·norm(-vol_ratio) + 0.30·norm(-amount_ratio) + 0.20·(0.55-up_vol_share)
即「低量能 / 低换手」**已经在生产口径里占了一席**。所以拿 mainline 当基线会给
拥挤度**系统性高估**增量。本脚本全部改用 composite 做对照。

输出 6 张表（只读，不写任何生产产物）：
    0  基线口径对比（mainline vs composite vs exhaust）
    1  换手分位窗口档扫描 W = 60/120/250/500
    2  权重扫描 w = 0.10~0.40（在 W=250 上）
    3  同族三指标合成（低换手分位 / 换手60d变化 / 成交额占比）
    4  逐年组内 IC（防单年耀眼）
    5  分环境组内 IC
    6  分组收益差（经济含义，H=20 / H=100）
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
from feature_store import EW_CODE  # noqa: E402

HZS = (20, 80, 100)
MIN_N = 8
WINDOWS = (60, 120, 250, 500)
WEIGHTS = (0.10, 0.20, 0.30, 0.40)
W_MAIN = 250
LINE = "=" * 108


def _f(d, key="mean", nd=3) -> str:
    if not d or d.get(key) is None:
        return "n/a"
    v = d[key]
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _pct(d, key="spread") -> str:
    v = (d or {}).get(key)
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def same_mask(mat, fw) -> np.ndarray:
    """候选因子「有值」的交易日掩码 —— 同口径对照的前提。"""
    return np.array([np.sum(~np.isnan(mat[:, i]) & ~np.isnan(fw[:, i])) >= MIN_N
                     for i in range(fw.shape[1])])


def xs_rank(m: np.ndarray) -> np.ndarray:
    """逐日横截面秩分位（0~1）。"""
    out = np.full(m.shape, np.nan)
    for i in range(m.shape[1]):
        col = m[:, i]
        mk = ~np.isnan(col)
        if mk.sum() < MIN_N:
            continue
        out[mk, i] = pd.Series(col[mk]).rank(pct=True).to_numpy()
    return out


def main() -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    dims = ctx["dims"]
    regime = ctx["regime"]
    codes = [c for c in fs.codes
             if c != EW_CODE and fs.meta.get(c, {}).get("level") in ("L1", "L2")]
    n_c, n_d = len(codes), len(fs.dates)
    # dims 里的矩阵按 fs.codes 行序排；收窄标的池后必须同步切片，否则静默错位
    row_idx = [fs.codes.index(c) for c in codes]

    ml = dims["mainline"][row_idx, :]
    comp = dims["composite"][row_idx, :]
    exh = dims["exhaust"][row_idx, :]

    print(LINE)
    print(f"样本：{n_c} 行业 × {n_d} 交易日（{fs.dates[0]} → {fs.dates[-1]}）  "
          f"引擎 {PL.ENGINE_VERSION}")
    print(LINE)

    # ---- 前瞻超额（与 review.fwd_stats 同构）：行业 − 行业等权
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

    cov = float(np.nanmean(~np.isnan(turnover))) if n_c else 0.0
    print(f"换手率面板覆盖率：{cov:.1%}（行业-日）；有数据的行业 "
          f"{int((val_days > 0).sum())}/{n_c}")

    # ---- 拥挤度原语
    tp = {W: LV.trailing_pct(turnover, W, int(W * 0.6)) for W in WINDOWS}
    crowd = {W: 100.0 * (1.0 - tp[W]) for W in WINDOWS}      # 低换手 -> 高分
    log_t = np.log(np.where(turnover > 0, turnover, np.nan))
    tchg60 = LV.diff_n(log_t, 60)

    # ================= 0. 基线口径对比 =================
    print()
    print(LINE)
    print("0. 基线口径对比 —— 上轮把 mainline 当「现行」，但它只占 BASE_WEIGHTS 的 0.34")
    print(f"   BASE_WEIGHTS = {C.BASE_WEIGHTS}")
    print("-" * 108)
    print(f"{'因子':<34}{'IC@20':>9}{'IC@80':>9}{'IC@100':>9}{'t@100':>9}"
          f"{'分组差@100':>12}{'分组差@20':>12}")
    for nm, mat in (("mainline（上轮当基线）", ml),
                    ("composite（真实基线）", comp),
                    ("exhaust（现有低量能维度）", exh)):
        a, b, c = (LV.ic_table(mat, fwd[h]) for h in HZS)
        sp100, sp20 = LV.group_spread(mat, fwd[100]), LV.group_spread(mat, fwd[20])
        print(f"{nm:<34}{_f(a):>9}{_f(b):>9}{_f(c):>9}{_f(c, 't'):>9}"
              f"{_pct(sp100):>12}{_pct(sp20):>12}")

    # ================= 1. 窗口档扫描 =================
    print()
    print(LINE)
    print("1. 换手分位窗口档扫描（W = 60/120/250/500）")
    print("   「组内IC」= 每日按 composite 分三组、组内算 spearman 再等权平均 ——")
    print("   这才是「在生产已有口径内部，还能不能分出好坏」的增量检验。")
    print("-" * 108)
    print(f"{'W':<6}{'IC@20':>9}{'IC@80':>9}{'IC@100':>9}{'组内IC@100':>12}{'t':>8}"
          f"{'组内IC@80':>12}{'控ml@100':>11}{'分组差@100':>12}{'分组差@20':>12}")
    for W in WINDOWS:
        s = crowd[W]
        g100 = LV.group_ic(s, fwd[100], comp)
        g80 = LV.group_ic(s, fwd[80], comp)
        gml = LV.group_ic(s, fwd[100], ml)
        sp100, sp20 = LV.group_spread(s, fwd[100]), LV.group_spread(s, fwd[20])
        print(f"{W:<6}{_f(LV.ic_table(s, fwd[20])):>9}{_f(LV.ic_table(s, fwd[80])):>9}"
              f"{_f(LV.ic_table(s, fwd[100])):>9}{_f(g100):>12}{_f(g100, 't'):>8}"
              f"{_f(g80):>12}{_f(gml):>11}{_pct(sp100):>12}{_pct(sp20):>12}")

    # ================= 2. 权重扫描 =================
    s0 = crowd[W_MAIN]
    mk0 = same_mask(s0, fwd[100])
    print()
    print(LINE)
    print(f"2. 权重扫描：combined = (1-w)·composite + w·低换手分位(W={W_MAIN})")
    print("   w 是「占 0~100 量表的权重」，与 BASE_WEIGHTS 同语义（可直接类比落参）")
    print(f"   同口径基准：composite 在「低换手分位有值」的 {int(mk0.sum())} 个交易日上"
          f"  IC@100 = {_f(LV.ic_table(comp, fwd[100], mk0))}")
    print("-" * 108)
    print(f"{'w':<8}{'IC@100(组合)':>14}{'Δ(同口径)':>12}{'组内IC@100':>12}{'t':>8}"
          f"{'分组差@100':>12}{'分组差@20':>12}{'高分组@100':>12}")
    for w in WEIGHTS:
        cb = (1.0 - w) * comp + w * s0
        ic_cb = LV.ic_table(cb, fwd[100])
        ic_base_mk = LV.ic_table(comp, fwd[100], mk0)
        delta = (ic_cb["mean"] - ic_base_mk["mean"]) if (ic_cb and ic_base_mk) else None
        gi = LV.group_ic(cb, fwd[100], comp)
        sp100, sp20 = LV.group_spread(cb, fwd[100]), LV.group_spread(cb, fwd[20])
        hi = f"{sp100['hi'] * 100:+.2f}%" if sp100 else "n/a"
        print(f"{w:<8.2f}{_f(ic_cb):>14}"
              f"{(f'{delta:+.3f}' if delta is not None else 'n/a'):>12}"
              f"{_f(gi):>12}{_f(gi, 't'):>8}{_pct(sp100):>12}{_pct(sp20):>12}{hi:>12}")

    # ================= 3. 同族合成 =================
    parts = [xs_rank(-tp[W_MAIN]), xs_rank(-tchg60), xs_rank(-sumrate)]
    syn = np.nanmean(np.stack(parts), axis=0)
    syn_score = 100.0 * syn
    print()
    print(LINE)
    print("3. 同族三指标合成 —— xs_rank(-换手分位) / xs_rank(-换手60d变化) / xs_rank(-成交额占比)")
    print("   目的：验证这三个量方向一致时，合成能否强过单指标（不是单指标噪声）")
    print("-" * 108)
    print(f"{'组合':<34}{'IC@20':>9}{'IC@80':>9}{'IC@100':>9}{'组内IC@100':>12}{'t':>8}"
          f"{'分组差@100':>12}{'分组差@20':>12}")
    for nm, s in (("低换手分位（单指标）", s0),
                  ("同族三指标合成", syn_score),
                  ("comp + 0.3·合成", 0.7 * comp + 0.3 * syn_score),
                  ("comp + 0.3·单指标", 0.7 * comp + 0.3 * s0)):
        gi = LV.group_ic(s, fwd[100], comp)
        sp100, sp20 = LV.group_spread(s, fwd[100]), LV.group_spread(s, fwd[20])
        print(f"{nm:<34}{_f(LV.ic_table(s, fwd[20])):>9}{_f(LV.ic_table(s, fwd[80])):>9}"
              f"{_f(LV.ic_table(s, fwd[100])):>9}{_f(gi):>12}{_f(gi, 't'):>8}"
              f"{_pct(sp100):>12}{_pct(sp20):>12}")

    # ================= 4. 逐年 =================
    print()
    print(LINE)
    print("4. 逐年组内 IC（控 composite，@100）—— 防单年耀眼")
    print("-" * 108)
    yrs = [y for y in sorted(set(d[:4] for d in fs.dates)) if y >= "2016"]
    print(f"{'因子':<30}" + "".join(f"{y[2:]:>8}" for y in yrs) + f"{'正/负':>9}")
    for nm, s in (("低换手分位 W=250", s0),
                  ("同族三指标合成", syn_score),
                  ("comp + 0.3·单指标", 0.7 * comp + 0.3 * s0),
                  ("comp + 0.3·合成", 0.7 * comp + 0.3 * syn_score)):
        vals = []
        for y in yrs:
            mi = np.array([d[:4] == y for d in fs.dates])
            g = LV.group_ic(s, fwd[100], comp, mask_i=mi)
            vals.append(None if not g else g["mean"])
        cells = "".join(f"{(v if v is not None else np.nan):>8.3f}" for v in vals)
        npos = sum(1 for v in vals if v is not None and v > 0)
        nneg = sum(1 for v in vals if v is not None and v <= 0)
        print(f"{nm:<30}{cells}{f'{npos}/{nneg}':>9}")

    # ================= 5. 分环境 =================
    print()
    print(LINE)
    print("5. 分环境组内 IC（@100，控 composite，括号为交易日数）")
    print("-" * 108)
    envs = ["ATTACK", "NEUTRAL", "DEFEND", "PANIC"]
    print(f"{'因子':<30}" + "".join(f"{e:>16}" for e in envs))
    for nm, s in (("低换手分位 W=250", s0),
                  ("同族三指标合成", syn_score),
                  ("comp + 0.3·单指标", 0.7 * comp + 0.3 * s0)):
        cells = ""
        for e in envs:
            mi = np.array([(regime.get(d) or {}).get("regime") == e for d in fs.dates])
            n = int(mi[C.WARMUP:].sum())
            g = LV.group_ic(s, fwd[100], comp, mask_i=mi)
            cells += f"{(f'{_f(g)}({n})'):>16}"
        print(f"{nm:<30}{cells}")

    # ================= 6. 分组收益差 =================
    print()
    print(LINE)
    print("6. 分组收益差（高分组 − 低分组的前瞻超额；H 窗口重叠 ⇒ t 值只当量级参考）")
    print("-" * 108)
    print(f"{'因子':<30}{'H':>5}{'高分组':>10}{'低分组':>10}{'差':>10}{'年化':>10}"
          f"{'t':>7}{'胜率':>8}")
    for nm, s in (("composite（基线）", comp),
                  ("低换手分位 W=250", s0),
                  ("同族三指标合成", syn_score),
                  ("comp + 0.3·单指标", 0.7 * comp + 0.3 * s0),
                  ("comp + 0.3·合成", 0.7 * comp + 0.3 * syn_score)):
        for H in (20, 100):
            sp = LV.group_spread(s, fwd[H])
            if not sp:
                continue
            ann = sp["spread"] * (250.0 / H)
            print(f"{nm:<30}{H:>5}{sp['hi'] * 100:>9.2f}%{sp['lo'] * 100:>9.2f}%"
                  f"{sp['spread'] * 100:>9.2f}%{ann * 100:>9.2f}%{sp['t']:>7.1f}"
                  f"{sp['win']:>7.1%}")

    # ================= 2b. 秩空间合成（表 2 的公平性检验） =================
    print()
    print(LINE)
    print("2b. 秩空间合成 —— composite 与拥挤度都转成横截面秩分位再混合（表 2 的公平性检验）")
    print("    理由：composite 是 0~100 的窄分布打分，低换手分位是 0~100 的**均匀**分位；")
    print("    按原量表直接混合时，均匀分布那一侧的横截面方差更大、会主导排序。")
    print("-" * 108)
    comp_r = xs_rank(comp)
    crowd_r = xs_rank(s0)
    base_r = LV.ic_table(comp_r, fwd[100], mk0)
    print(f"    同口径基准（comp 的秩分位，{int(mk0.sum())} 日）：IC@100 = {_f(base_r)}")
    print(f"{'w':<8}{'IC@100(秩组合)':>16}{'Δ':>11}{'组内IC@100':>12}{'t':>8}"
          f"{'分组差@100':>12}{'分组差@20':>12}")
    for w in WEIGHTS:
        cb = (1.0 - w) * comp_r + w * crowd_r
        ic = LV.ic_table(cb, fwd[100])
        d = (ic["mean"] - base_r["mean"]) if (ic and base_r) else None
        gi = LV.group_ic(cb, fwd[100], comp)
        sp100, sp20 = LV.group_spread(cb, fwd[100]), LV.group_spread(cb, fwd[20])
        print(f"{w:<8.2f}{_f(ic):>16}{(f'{d:+.4f}' if d is not None else 'n/a'):>11}"
              f"{_f(gi):>12}{_f(gi, 't'):>8}{_pct(sp100):>12}{_pct(sp20):>12}")

    # ================= 7. 条件化（只在弱市启用） =================
    print()
    print(LINE)
    print("7. 条件化：只在特定环境启用拥挤度。依据表 5 —— composite 在 PANIC 本就弱，")
    print("   而拥挤度在 DEFEND(0.034)/PANIC(0.043) 最强、ATTACK(0.015) 最弱。")
    print("   实现：环境外把拥挤度置中性 50（不改变 comp 的排序），环境内才让它参与。")
    print("-" * 108)
    print(f"{'环境':<16}{'w':>5}{'环境内IC(基线)':>16}{'环境内IC(组合)':>16}{'Δ':>11}"
          f"{'环境内组内IC':>14}{'分组差@100':>12}")
    for envs_on, label in ((("PANIC",), "仅 PANIC"),
                           (("DEFEND", "PANIC"), "DEFEND+PANIC"),
                           (("DEFEND",), "仅 DEFEND"),
                           (("ATTACK", "NEUTRAL"), "ATTACK+NEUTRAL")):
        m = np.array([(regime.get(d) or {}).get("regime") in envs_on for d in fs.dates])
        for w in (0.20, 0.30):
            s_cond = np.where(m[None, :] & ~np.isnan(s0), s0, 50.0)
            cb = (1.0 - w) * comp + w * s_cond
            ic_b = LV.ic_table(comp, fwd[100], m)
            ic_c = LV.ic_table(cb, fwd[100], m)
            d = (ic_c["mean"] - ic_b["mean"]) if (ic_c and ic_b) else None
            gi = LV.group_ic(cb, fwd[100], comp, mask_i=m)
            sp = LV.group_spread(cb, fwd[100], mask_i=m)
            print(f"{label:<16}{w:>5.2f}{_f(ic_b):>16}{_f(ic_c):>16}"
                  f"{(f'{d:+.4f}' if d is not None else 'n/a'):>11}"
                  f"{_f(gi):>14}{_pct(sp):>12}")

    # ================= 8. 条件化 × 同族合成（秩空间）+ regime 时点稳健性 =================
    print()
    print(LINE)
    print("8. 条件化 × 合成（秩空间混合；环境外把因子置中性，不改变 comp 在环境外的排序）")
    print("   表 7 用的是**当日** regime。这里同时给 T-1 版本 —— 生产 config 的")
    print("   exposure_cap 注释写的是「前一日 regime」，口径要对齐才算可落参。")
    print("-" * 108)
    comp_r2 = xs_rank(comp)
    print(f"{'因子':<10}{'regime':<8}{'环境':<14}{'w':>5}{'基线IC':>10}{'组合IC':>10}"
          f"{'Δ':>11}{'日数':>8}")
    for nm_f, s_f in (("单指标", s0), ("同族合成", syn_score)):
        s_r = xs_rank(s_f)
        for lag, tag in ((0, "当日"), (1, "T-1")):
            for envs_on, label in ((("PANIC",), "仅PANIC"),
                                   (("DEFEND", "PANIC"), "DEFEND+PANIC"),
                                   (("ATTACK", "NEUTRAL"), "ATTACK+NEU")):
                m = np.array([(regime.get(fs.dates[max(0, i - lag)]) or {}).get("regime")
                              in envs_on for i in range(n_d)])
                w = 0.30
                s_cond = np.where(m[None, :] & ~np.isnan(s_r), s_r, 0.5)
                cb = (1.0 - w) * comp_r2 + w * s_cond
                ic_b = LV.ic_table(comp_r2, fwd[100], m)
                ic_c = LV.ic_table(cb, fwd[100], m)
                d = (ic_c["mean"] - ic_b["mean"]) if (ic_c and ic_b) else None
                print(f"{nm_f:<10}{tag:<8}{label:<14}{w:>5.2f}{_f(ic_b):>10}{_f(ic_c):>10}"
                      f"{(f'{d:+.4f}' if d is not None else 'n/a'):>11}"
                      f"{int(m[C.WARMUP:].sum()):>8}")

    print()
    print(LINE)
    print("完成。上轮报告的「Δ=+0.016（相对 mainline）」请以本表 2 / 2b 的 Δ 列为准。")


if __name__ == "__main__":
    main()
