# -*- coding: utf-8 -*-
"""价格之外的信息源（申万行业估值 / 换手 / 市值）增量信息诊断。

背景
----
`docs/趋势因子review-2026-09-28.md` 的结论是：价格类趋势因子已被榨干 ——
任何**逐日横截面单调变换**（减中位 / 分位化 / 与分位加权混合）对 Rank IC
都是 0 增量（数学上恒等）。因此唯一可能有增量的方向，是引入
**价格之外的信息**。

本脚本测的就是这条路上唯一的现成信息源：申万官方「指数分析」
（engine/fetch_sw_valuation.py 抓取，落 L0 data/raw/sw_valuation/），
逐日 × 逐行业提供：
    市盈率 pe / 市净率 pb / 股息率 dp   —— 定价信息（不是价格的换装）
    换手率 turnover / 成交额占比 sumrate / 平均流通市值 avg_mv —— 拥挤度与规模

为什么这些不违反「单调变换无增量」的结论
--------------------------------------
那条结论限定在**逐日横截面**的单调变换。本脚本的核心候选是
**各行业相对自身历史的分位**（trailing percentile，时间序列变换），
它改变的是横截面排序，因此是一条真正的新路径。

防前视
------
· 分位一律用**尾部窗口**计算（`trailing_pct`），不用全样本分位；
· 前瞻超额与 `review.fwd_stats` 同口径（ret − 行业等权 ret），向量化重写；
· 额外做一次「估值与价格是否只是同一信息」的内部一致性体检。

输出表
------
  0 数据可得性（覆盖率）+ 内部一致性体检
  A 全样本 Rank IC（H=20/80/100）
  B 控制 mainline 后的组内 IC（**增量检验**）+ 三分位明细
  C 分环境分层 IC（含 mainline 对照）—— 趋势失效处估值是否还能用
  D 与 mainline 的横截面秩相关（防「换装」）
  E 逐年 IC / 组内 IC
  F 组合因子（mainline + 估值的 z 加权）

用法：python engine/lab_valuation.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import rawstore as RS  # noqa: E402
from feature_store import EW_CODE  # noqa: E402

HZS = (20, 80, 100)
MIN_N = 8
VAL_FIELDS = ("close", "turnover", "pe", "pb", "dp", "sumrate", "avg_mv")

W3Y, W5Y, W1Y = 750, 1250, 250


# ------------------------------------------------------------------ 统计口径
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


def _agg(ics):
    a = np.asarray(ics, dtype=float)
    if len(a) < 30:
        return None
    mean, sd = float(a.mean()), float(a.std(ddof=1))
    ir = mean / sd if sd > 0 else 0.0
    return {"mean": mean, "ir": ir, "t": ir * np.sqrt(len(a)),
            "pos": float((a > 0).mean()), "n": len(a)}


def ic_table(score, fwd, mask_i=None):
    ics = []
    for i in range(C.WARMUP, fwd.shape[1]):
        if mask_i is not None and not mask_i[i]:
            continue
        v = spearman(score[:, i], fwd[:, i])
        if v is not None:
            ics.append(v)
    return _agg(ics)


def group_ic(score, fwd, ctrl, n_grp=3, mask_i=None, detail=False):
    """控制 ctrl（mainline）后的组内 Rank IC。detail=True 时返回各分位明细。"""
    ics, per = [], [[] for _ in range(n_grp)]
    for i in range(C.WARMUP, fwd.shape[1]):
        if mask_i is not None and not mask_i[i]:
            continue
        c_i, s_i, f_i = ctrl[:, i], score[:, i], fwd[:, i]
        m = ~np.isnan(c_i) & ~np.isnan(s_i) & ~np.isnan(f_i)
        if m.sum() < n_grp * MIN_N:
            continue
        cv = c_i[m]
        qs = np.nanquantile(cv, [k / n_grp for k in range(1, n_grp)])
        edges = [-np.inf, *qs, np.inf]
        vals = []
        for g in range(n_grp):
            p = m & (c_i > edges[g]) & (c_i <= edges[g + 1])
            if p.sum() < MIN_N:
                continue
            v = spearman(np.where(p, s_i, np.nan), np.where(p, f_i, np.nan))
            if v is not None:
                vals.append(v)
                per[g].append(v)
        if vals:
            ics.append(float(np.mean(vals)))
    out = _agg(ics)
    if not detail:
        return out
    return out, [_agg(p) for p in per]


# ------------------------------------------------------------------ 时间序列工具
def trailing_pct(mat: np.ndarray, W: int, min_periods: int) -> np.ndarray:
    """对每一行做「当日值在自身前 W 个交易日中的分位」（右对齐窗口，只看历史）。

    只用尾部窗口 ⇒ 无前视。返回同形状矩阵，前 W-1 列为 NaN。
    """
    out = np.full(mat.shape, np.nan)
    n = mat.shape[1]
    if n < W:
        return out
    for r in range(mat.shape[0]):
        x = mat[r]
        view = np.lib.stride_tricks.sliding_window_view(x, W)   # (n-W+1, W) 视图
        last = view[:, -1]
        cnt = np.sum(~np.isnan(view), axis=1)
        lt = np.sum(view < last[:, None], axis=1)
        eq = np.sum(view == last[:, None], axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            pct = (lt + 0.5 * eq) / np.where(cnt > 0, cnt, np.nan)
        pct = np.where(cnt >= min_periods, pct, np.nan)
        pct = np.where(np.isnan(last), np.nan, pct)
        out[r, W - 1:] = pct
    return out


def diff_n(mat: np.ndarray, k: int) -> np.ndarray:
    out = np.full(mat.shape, np.nan)
    if mat.shape[1] > k:
        out[:, k:] = mat[:, k:] - mat[:, :-k]
    return out


def safe_div(a, b):
    return np.where((np.abs(b) > 1e-12) & ~np.isnan(b), a / b, np.nan)


def group_spread(mat, fw, n_grp: int = 3, mask_i=None):
    """每日按因子分 n_grp 组，返回高分组/低分组的前瞻超额均值与差值。

    这是 IC 的「经济含义」版本：IC 是秩相关、看不出赚多少；分组差能看出量级。
    注意：H 日窗口彼此重叠，t 值会被高估，只能当量级参考，不当显著性证据。
    """
    hi, lo = [], []
    for i in range(C.WARMUP, fw.shape[1]):
        if mask_i is not None and not mask_i[i]:
            continue
        s, f = mat[:, i], fw[:, i]
        m = ~np.isnan(s) & ~np.isnan(f)
        if m.sum() < n_grp * MIN_N:
            continue
        sv, fv = s[m], f[m]
        q = np.nanquantile(sv, [1.0 / n_grp, 1.0 - 1.0 / n_grp])
        a, b = fv[sv > q[1]], fv[sv <= q[0]]
        if len(a) < MIN_N or len(b) < MIN_N:
            continue
        hi.append(float(a.mean()))
        lo.append(float(b.mean()))
    if len(hi) < 30:
        return None
    hi_a, lo_a = np.asarray(hi), np.asarray(lo)
    d = hi_a - lo_a
    sd = float(d.std(ddof=1))
    return {"hi": float(hi_a.mean()), "lo": float(lo_a.mean()),
            "spread": float(d.mean()), "n": len(d),
            "t": (float(d.mean()) / sd * np.sqrt(len(d))) if sd > 0 else 0.0,
            "win": float((d > 0).mean())}


# ------------------------------------------------------------------ 数据加载
def load_val_panel(codes, dates):
    """从 L0 组装估值面板：{field: [n_c x n_d]}，外加逐行业覆盖天数与缺数据清单。"""
    idx = {d: i for i, d in enumerate(dates)}
    n_c, n_d = len(codes), len(dates)
    out = {f: np.full((n_c, n_d), np.nan) for f in VAL_FIELDS}
    n_days = np.zeros(n_c, dtype=int)
    missing = []
    for r, c in enumerate(codes):
        recs = RS.get("sw_valuation", c)
        if not recs:
            missing.append(c)
            continue
        hit = 0
        for rec in recs:
            i = idx.get(str(rec.get("date")))
            if i is None:
                continue
            hit += 1
            for f in VAL_FIELDS:
                v = rec.get(f)
                if v is None:
                    continue
                try:
                    out[f][r, i] = float(v)
                except (TypeError, ValueError):
                    pass
        n_days[r] = hit
    return out, n_days, missing


def main() -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    dims = ctx["dims"]
    regime = ctx["regime"]
    codes = [c for c in fs.codes
             if c != EW_CODE and fs.meta.get(c, {}).get("level") in ("L1", "L2")]
    n_c, n_d = len(codes), len(fs.dates)
    # `dims` 里的矩阵是 len(fs.codes) x len(fs.dates)、按 fs.codes 的行序排的。
    # 标的池收窄到 L1/L2 之后，必须把这些行同步切出来——否则矩阵会静默错位，
    # 得出的 IC 全是假的（上一轮 lab_trend_quality 没做过滤，恰好没暴露这个坑）。
    row_idx = [fs.codes.index(c) for c in codes]
    print(f"样本：{n_c} 个行业 × {n_d} 个交易日（{fs.dates[0]} → {fs.dates[-1]}）")
    print(f"环境窗口：{min(regime)} → {max(regime)}\n")

    V, val_days, missing = load_val_panel(codes, fs.dates)

    # ---------------- 0. 可得性 ----------------
    print("=" * 96)
    print("0. 数据可得性（L0 sw_valuation 覆盖情况）")
    yr_of = np.array([d[:4] for d in fs.dates])
    has_pe = ~np.isnan(V["pe"])
    print(f"{'年份':<8}{'交易日':>7}{'有数据交易日':>13}{'行业-日覆盖率':>14}")
    print("-" * 96)
    for y in sorted(set(yr_of)):
        mk = yr_of == y
        days_y = int(mk.sum())
        got = int(np.nansum(np.any(has_pe[:, mk], axis=0)))
        cov = float(np.nanmean(has_pe[:, mk])) if days_y else 0.0
        print(f"{y:<8}{days_y:>7}{got:>13}{cov:>13.1%}")
    n_full = int((val_days == 0).sum())
    print(f"\n完全无估值数据的行业：{n_full} 个"
          + (f"（{missing[:10]}）" if missing else ""))
    if val_days.max() > 0:
        ok = [codes[i] for i in range(n_c) if val_days[i] > 0]
        firsts, lasts = [], []
        for i in range(n_c):
            ii = np.where(~np.isnan(V["pe"][i]))[0]
            if len(ii):
                firsts.append(fs.dates[ii[0]])
                lasts.append(fs.dates[ii[-1]])
        print(f"有数据的行业：{len(ok)} 个，单行业最多 {val_days.max()} 天")
        if firsts:
            print(f"最早数据日：{min(firsts)}；最晚数据日：{max(lasts)}")
    if n_full == n_c:
        print("\n[中止] L0 里没有任何估值数据 —— 先跑 engine/fetch_sw_valuation.py")
        return

    # ---------------- 内部一致性体检 ----------------
    print()
    print("=" * 96)
    print("0b. 内部一致性体检")
    close = np.vstack([fs.series(c, "close") for c in codes]).astype(float)

    # 0c 日期口径（防前视的**决定性**检验）：分析表的 close 必须等于价格面板**同日**收盘。
    #    若两者错位一天，说明这张表带前视，后面所有 IC 都不能信。
    #    这里用全历史逐值比对，比探针的 5 天窗口强得多，且不需要额外发请求。
    same = tot_c = 0
    bad_samples = []
    for r in range(n_c):
        for i in range(n_d):
            x, y = V["close"][r, i], close[r, i]
            if np.isnan(x) or np.isnan(y):
                continue
            tot_c += 1
            if abs(x - y) < 1e-6:
                same += 1
            elif len(bad_samples) < 6:
                bad_samples.append((codes[r], fs.dates[i], round(x, 4), round(y, 4)))
    if tot_c:
        print(f"  [日期口径] 分析表 close vs 价格面板同日 close：可比 {tot_c} 个，"
              f"逐值相同 {same}（{same / tot_c:.2%}）")
        if bad_samples:
            print(f"    不等样例 (code, date, 分析表close, 面板同日close)：")
            for s in bad_samples:
                print(f"      {s}")
        if same >= tot_c * 0.99:
            print("    → 通过：两表日期严格对齐，估值无前视 ✓")
        else:
            print("    → 注意：存在系统性不等，可能是面板前向填充或日期错位，"
                  "需结合样例判断")
    else:
        print("  [日期口径] 无可比样本")

    ret1 = np.full((n_c, n_d), np.nan)
    ret1[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0
    for f, nm in (("pe", "PE"), ("pb", "PB")):
        x = V[f]
        with np.errstate(invalid="ignore", divide="ignore"):
            d = diff_n(np.log(np.where(x > 0, x, np.nan)), 1)
        pairs = ~np.isnan(d) & ~np.isnan(ret1)
        if pairs.sum() < 100:
            print(f"  {nm}: 样本不足")
            continue
        c_ = float(np.corrcoef(d[pairs], ret1[pairs])[0, 1])
        print(f"  Δlog({nm}) 与当日收益的相关 {c_:+.3f} "
              f"（接近 1 = 逐日盯市重算，合理；接近 0 = 低频重述，需警惕） n={int(pairs.sum())}")

    # ---------------- 前瞻超额（与 review.fwd_stats 同口径）----------------
    ew = fs.series(EW_CODE, "close").astype(float)
    fwd = {}
    for H in HZS:
        r = close[:, H:] / close[:, :n_d - H] - 1.0
        ewr = ew[H:] / ew[:n_d - H] - 1.0
        M = np.full((n_c, n_d), np.nan)
        M[:, :n_d - H] = r - ewr[None, :]
        fwd[H] = M

    # ---------------- 因子构造 ----------------
    pe, pb, dp = V["pe"], V["pb"], V["dp"]
    turn, sumrate, avgmv = V["turnover"], V["sumrate"], V["avg_mv"]

    ep = np.where(pe > 0, safe_div(1.0, pe), np.nan)
    bp = np.where(pb > 0, safe_div(1.0, pb), np.nan)

    # ---- 隐含基本面：从「价格 + 估值」里把 E 与 B 分离出来 ----
    #   PE = P/E, PB = P/B  ⇒  E = P/PE,  B = P/PB,  ROE = E/B = PB/PE
    #   ⇒ 隐含盈利增速 g_E = Δlog(E) = Δlog(P) − Δlog(PE)
    # 关键在于：E 与 B **不是价格路径的函数** —— 它们只随财报与 TTM 滚动变化，
    # 与当日涨跌无关。所以 g_E / ROE 是真正的「基本面」信息，而不是价格的换装
    # （这也是本接口能给到的最接近「盈利/盈利增速」的东西——它本身不直接提供）。
    with np.errstate(invalid="ignore", divide="ignore"):
        E = np.where((pe > 0) & (close > 0), close / np.where(pe > 0, pe, np.nan), np.nan)
        Bk = np.where((pb > 0) & (close > 0), close / np.where(pb > 0, pb, np.nan), np.nan)
        roe = np.where((pe > 0) & (pb > 0), pb / pe, np.nan)
    logE = np.log(np.where(E > 0, E, np.nan))
    logB = np.log(np.where(Bk > 0, Bk, np.nan))
    gE60, gE250 = diff_n(logE, 60), diff_n(logE, 250)
    gB250 = diff_n(logB, 250)
    roe_chg60 = diff_n(np.log(np.where(roe > 0, roe, np.nan)), 60)

    pe_pct3 = trailing_pct(pe, W3Y, 250)
    pb_pct3 = trailing_pct(pb, W3Y, 250)
    pe_pct5 = trailing_pct(pe, W5Y, 400)
    pb_pct5 = trailing_pct(pb, W5Y, 400)
    turn_pct1 = trailing_pct(turn, W1Y, 120)

    ml = dims["mainline"][row_idx, :]

    F = {
        "mainline（现行，对照）": ml,
        "EP = 1/PE（盈利收益率）": ep,
        "BP = 1/PB（账面市值比）": bp,
        "DP 股息率": dp,
        "PE 分位(3y) 取负": -pe_pct3,
        "PB 分位(3y) 取负": -pb_pct3,
        "PE 分位(5y) 取负": -pe_pct5,
        "PB 分位(5y) 取负": -pb_pct5,
        "PB 分位 60d 变化 取负": -diff_n(pb_pct3, 60),
        "PE 分位 60d 变化 取负": -diff_n(pe_pct3, 60),
        "换手分位(1y) 取负": -turn_pct1,
        "换手 60d 变化": diff_n(turn, 60),
        "成交额占比 sumrate": sumrate,
        "小市值 -log(平均流通市值)": -np.log(np.where(avgmv > 0, avgmv, np.nan)),
        # --- 隐含基本面（E/B 从估值里分离出来的部分）---
        "隐含盈利增速 g_E(250d)": gE250,
        "隐含盈利增速 g_E(60d)": gE60,
        "隐含净资产增速 g_B(250d)": gB250,
        "隐含ROE = PB/PE": roe,
        "隐含ROE 60d 变化": roe_chg60,
    }

    # ---------------- A ----------------
    print()
    print("=" * 96)
    print("A. 全样本横截面 Rank IC（逐日 spearman；ex_ew 前瞻超额；WARMUP 起）")
    print(f"{'因子':<30}" + "".join(f"{'IC@' + str(H):>9}{'t':>7}{'+|':>7}" for H in HZS))
    print("-" * 96)
    for name, mat in F.items():
        cells = f"{name:<30}"
        for H in HZS:
            r = ic_table(mat, fwd[H])
            cells += (f"{r['mean']:>9.3f}{r['t']:>7.1f}{r['pos']:>7.1%}" if r
                      else f"{'—':>9}{'—':>7}{'—':>7}")
        print(cells)

    # ---------------- A2. 同口径（同批交易日）对照 ----------------
    # 估值分位类因子需要 750/1250 日预热，只在 2015 年之后才有值；而 mainline 覆盖全样本。
    # 直接比两者的全样本 IC 是**不同子样本**的比较，会凭空造出「增量」。
    # 这里把 mainline 放到候选因子自己那批交易日上重算，差异才可比。
    def mask_of(mat, fw):
        return np.array([(~np.isnan(mat[:, i]) & ~np.isnan(fw[:, i])).sum() >= MIN_N
                         for i in range(fw.shape[1])])

    print()
    print("=" * 96)
    print("A2. 同口径对照（**这一步不看会误判**）：候选只在它有数据的那批交易日上评估，")
    print("    mainline 在**同一批交易日**上重算；Δ 才是真增量")
    print(f"{'因子':<28}{'IC@100(候选)':>13}{'IC@100(ml同日)':>15}{'Δ':>8}{'共同日数':>9}")
    print("-" * 96)
    for name, mat in F.items():
        mk = mask_of(mat, fwd[100])
        r_c = ic_table(mat, fwd[100])
        r_m = ic_table(ml, fwd[100], mk)
        if not r_c or not r_m:
            print(f"{name:<28}{'—':>13}{'—':>15}{'—':>8}{int(mk.sum()):>9}")
            continue
        print(f"{name:<28}{r_c['mean']:>13.3f}{r_m['mean']:>15.3f}"
              f"{r_c['mean'] - r_m['mean']:>8.3f}{r_c['n']:>9}")

    # ---------------- B ----------------
    print()
    print("=" * 96)
    print("B. 控制 mainline 后的组内 IC（每日按 mainline 三分位分组，组内 spearman 再平均）")
    print("   显著为正 = 在「同样强的趋势」内部还能再分好坏 = 真增量")
    print(f"{'因子':<30}{'组内IC@80':>11}{'t':>7}{'组内IC@100':>11}{'t':>7}{'组内IC@20':>11}{'t':>7}")
    print("-" * 96)
    for name, mat in F.items():
        if name.startswith("mainline"):
            continue
        cells = f"{name:<30}"
        for H in (80, 100, 20):
            g = group_ic(mat, fwd[H], ml)
            cells += (f"{g['mean']:>11.3f}{g['t']:>7.1f}" if g
                      else f"{'—':>11}{'—':>7}")
        print(cells)

    # ---------------- B2 ----------------
    print()
    print("B2. 三分位明细（组内 IC@100）：低动量的名字身上估值是否更有效？")
    print(f"{'因子':<30}{'弱(低mainline)':>15}{'中':>12}{'强(高mainline)':>15}")
    print("-" * 96)
    for nm in ["EP = 1/PE（盈利收益率）", "BP = 1/PB（账面市值比）", "PB 分位(3y) 取负",
               "PE 分位(3y) 取负", "DP 股息率", "换手分位(1y) 取负",
               "小市值 -log(平均流通市值)", "隐含盈利增速 g_E(250d)", "隐含ROE = PB/PE"]:
        _g, det = group_ic(F[nm], fwd[100], ml, detail=True)
        cells = f"{nm:<30}"
        for d in det:
            cells += (f"{d['mean']:>11.3f}({d['n'] // 10:>2})" if d else f"{'—':>15}")
        print(cells)

    # ---------------- C ----------------
    print()
    print("=" * 96)
    print("C. 分环境分层 IC（H=100）：趋势失效处（PANIC/DEFEND），估值是否还能用？")
    reg_of = np.array([(regime.get(d) or {}).get("regime") for d in fs.dates], dtype=object)
    sel = ["mainline（现行，对照）", "EP = 1/PE（盈利收益率）", "BP = 1/PB（账面市值比）",
           "DP 股息率", "PB 分位(3y) 取负", "PE 分位(3y) 取负", "换手分位(1y) 取负",
           "小市值 -log(平均流通市值)", "隐含盈利增速 g_E(250d)", "隐含ROE = PB/PE"]
    print(f"{'因子':<30}" + "".join(f"{r:>14}" for r in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC")))
    print("-" * 96)
    for name in sel:
        mat = F[name]
        cells = f"{name:<30}"
        for rg in ("ATTACK", "NEUTRAL", "DEFEND", "PANIC"):
            mk = np.array([bool(x == rg) for x in reg_of])
            r = ic_table(mat, fwd[100], mk)
            cells += (f"{r['mean']:>10.3f}({r['n'] // 10:>2})" if r else f"{'—':>14}")
        print(cells)

    # ---------------- D ----------------
    print()
    print("=" * 96)
    print("D. 与 mainline 的逐日横截面秩相关（|高| = 可能是同一信息的换装）")
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

    # ---------------- E ----------------
    print()
    print("=" * 96)
    print("E. 逐年 IC（H=100）—— 增量是否跨年普遍（防单年耀眼）")
    key = ["mainline（现行，对照）", "EP = 1/PE（盈利收益率）", "BP = 1/PB（账面市值比）",
           "PB 分位(3y) 取负", "PE 分位(3y) 取负", "换手分位(1y) 取负",
           "隐含盈利增速 g_E(250d)", "隐含ROE = PB/PE"]
    eyr = sorted({d[:4] for d in fs.dates[C.WARMUP:-1] if d >= "2016"})
    print(f"{'因子':<28}" + "".join(f"{y[2:]:>8}" for y in eyr))
    print("-" * 96)
    for nm in key:
        cells = f"{nm:<28}"
        for y in eyr:
            mk = np.array([d[:4] == y for d in fs.dates])
            r = ic_table(F[nm], fwd[100], mk)
            cells += (f"{r['mean']:>8.3f}" if r else f"{'—':>8}")
        print(cells)
    print(f"{'（组内，控制 mainline）':<26}" + "".join(f"{y[2:]:>8}" for y in eyr))
    for nm in key[1:]:
        cells = f"{nm:<28}"
        for y in eyr:
            mk = np.array([d[:4] == y for d in fs.dates])
            g = group_ic(F[nm], fwd[100], ml, mask_i=mk)
            cells += (f"{g['mean']:>8.3f}" if g else f"{'—':>8}")
        print(cells)

    # ---------------- F ----------------
    def zs(mat):
        m = np.nanmean(mat, axis=0, keepdims=True)
        s = np.nanstd(mat, axis=0, keepdims=True)
        return np.where(s > 1e-12, (mat - m) / s, np.nan)

    print()
    print("=" * 96)
    print("F. 组合因子（同日横截面 z 标准化后加权）—— 动量 × 估值 是否互补")
    print("   「ml同日子集」列 = mainline 只在**该组合有值的那些交易日**上的 IC，")
    print("   必须和它比；否则预热期缺失会把不同子样本的差异充当成增益。")
    combos = {
        "现行 mainline（对照）": ml,
        "0.7·ml + 0.3·(-PB分位3y)": 0.7 * zs(ml) + 0.3 * zs(F["PB 分位(3y) 取负"]),
        "0.7·ml + 0.3·EP": 0.7 * zs(ml) + 0.3 * zs(ep),
        "0.6·ml + 0.2·EP + 0.2·(-PB分位3y)": (0.6 * zs(ml) + 0.2 * zs(ep)
                                              + 0.2 * zs(F["PB 分位(3y) 取负"])),
        "0.7·ml + 0.3·DP": 0.7 * zs(ml) + 0.3 * zs(dp),
        "0.7·ml + 0.3·BP(1/PB)": 0.7 * zs(ml) + 0.3 * zs(bp),
        "0.7·ml + 0.3·(-换手分位1y)": 0.7 * zs(ml) + 0.3 * zs(F["换手分位(1y) 取负"]),
    }
    print(f"{'组合':<34}{'IC@20':>9}{'IC@80':>9}{'IC@100':>9}{'t':>7}"
          f"{'ml同日子集':>11}{'Δ':>8}")
    print("-" * 96)
    for nm, mat in combos.items():
        cells = f"{nm:<34}"
        for H in (20, 80):
            r = ic_table(mat, fwd[H])
            cells += (f"{r['mean']:>9.3f}" if r else f"{'—':>9}")
        r100 = ic_table(mat, fwd[100])
        cells += (f"{r100['mean']:>9.3f}{r100['t']:>7.1f}" if r100 else f"{'—':>9}{'—':>7}")
        if r100:
            mk = np.array([(~np.isnan(mat[:, i]) & ~np.isnan(fwd[100][:, i])).sum() >= MIN_N
                           for i in range(n_d)])
            r_base = ic_table(ml, fwd[100], mk)
            if r_base:
                cells += (f"{r_base['mean']:>11.3f}"
                          f"{r100['mean'] - r_base['mean']:>8.3f}")
        print(cells)

    # ---------------- G. 经济含义（分组收益差）----------------
    print()
    print("=" * 96)
    print("G. 经济含义：每日按因子分三组，「高分组 − 低分组」的前瞻超额差")
    print("   注：H 日窗口彼此重叠 ⇒ t 值被高估，只当量级参考；未扣交易成本")
    sel_g = ["mainline（现行，对照）", "BP = 1/PB（账面市值比）", "DP 股息率",
             "EP = 1/PE（盈利收益率）", "换手分位(1y) 取负", "换手 60d 变化",
             "成交额占比 sumrate", "小市值 -log(平均流通市值)",
             "PE 分位(3y) 取负", "PB 分位(3y) 取负",
             "隐含盈利增速 g_E(250d)", "隐含ROE = PB/PE"]
    print(f"{'因子':<26}{'H':>4}{'高分组':>9}{'低分组':>9}{'差':>9}"
          f"{'差(年化)':>10}{'t':>7}{'胜率':>8}")
    print("-" * 96)
    for nm in sel_g:
        for H in (20, 100):
            r = group_spread(F[nm], fwd[H])
            if not r:
                continue
            ann = (1.0 + r["spread"]) ** (244.0 / H) - 1.0
            print(f"{nm:<26}{H:>4}{r['hi']:>9.2%}{r['lo']:>9.2%}"
                  f"{r['spread']:>9.2%}{ann:>10.2%}{r['t']:>7.1f}{r['win']:>8.1%}")
    print()
    print("   组合信号的分组差（对照：mainline 单因子）")
    combos_g = {
        "mainline（单因子）": ml,
        "0.7·ml + 0.3·BP": 0.7 * zs(ml) + 0.3 * zs(bp),
        "0.7·ml + 0.3·低换手分位": 0.7 * zs(ml) + 0.3 * zs(F["换手分位(1y) 取负"]),
        "0.7·ml + 0.3·小市值": 0.7 * zs(ml) + 0.3 * zs(F["小市值 -log(平均流通市值)"]),
    }
    for nm, mat in combos_g.items():
        for H in (20, 100):
            r = group_spread(mat, fwd[H])
            if not r:
                continue
            ann = (1.0 + r["spread"]) ** (244.0 / H) - 1.0
            print(f"{nm:<26}{H:>4}{r['hi']:>9.2%}{r['lo']:>9.2%}"
                  f"{r['spread']:>9.2%}{ann:>10.2%}{r['t']:>7.1f}{r['win']:>8.1%}")
    print()


if __name__ == "__main__":
    main()
