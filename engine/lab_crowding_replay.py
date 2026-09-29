# -*- coding: utf-8 -*-
"""条件化拥挤度的**组合层影子重放** —— IC 增量能否兑现成 cagr / maxdd / Sharpe。

为什么必须做这一步
--------------------------------------------------------------------------------
2024 那轮的教训：G2 变体把出手率从 4% 抬到 23%、IC 也更好看，却是**最差**的一版。
**IC 增量 ≠ cagr 增量**。所以条件化拥挤度在 IC 层的正 Δ（仅 PANIC +0.0124、
DEFEND+PANIC +0.0123，T-1 口径）只能算「有资格进入组合层验证」，不等于可落参。

注入方式（干净的单变量对照）
--------------------------------------------------------------------------------
`classify_day(fs, dims, comp, regime, state, fwd, date)` 的两个输入是分开的：
    - `comp`        → 决定**排序**（谁排前面 / 谁能进 Top-K / 谁过 composite 阈值）
    - `dims[mainline]` 等 → 决定**回避与闸门**（谁被 avoid、主线强度门槛）
本脚本**只替换 `ctx["comp"]`**（同步替换 `dims["composite"]` 以保持一致），
因此「谁被回避」的判定完全不动，变的只有排序 —— 单变量、可归因。

不写任何生产产物：结论只留在内存里、打到 stdout。**不改 config、不落参。**

用法：
    python lab_crowding_replay.py           # 全窗口（2018 起）
    python lab_crowding_replay.py 60        # 冒烟测试：只跑最后 60 天
"""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402
import lab_valuation as LV  # noqa: E402
import lab_crowding as LC  # noqa: E402
import lab_crowding_cond as LCC  # noqa: E402
from feature_store import EW_CODE  # noqa: E402

W_MAIN = 250
W_REF = 0.30
LINE = "=" * 108


def num(v, nd=3, pct=False) -> str:
    """None 安全格式化（短窗口下 sharpe / calmar 可能无定义）。"""
    if v is None:
        return "n/a"
    return f"{v * 100:.2f}%" if pct else f"{v:.{nd}f}"


def year_curve(dates: List[str], nav: List[float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for i, d in enumerate(dates):
        y = d[:4]
        if y in out:
            continue
        base = nav[i - 1] if i > 0 else nav[i]
        j = i
        while j + 1 < len(dates) and dates[j + 1][:4] == y:
            j += 1
        out[y] = nav[j] / base - 1.0 if base else 0.0
    return out


def cagr_between(dates: List[str], nav: List[float], start: str) -> float:
    i = next((k for k, d in enumerate(dates) if d >= start), None)
    if i is None:
        return float("nan")
    base = nav[i - 1] if i > 0 else nav[i]
    years = (len(nav) - i) / 243.0
    return (nav[-1] / base) ** (1.0 / years) - 1.0 if years > 0 and base else float("nan")


def sig(day: dict) -> tuple:
    """当日结论的「选股签名」—— 用于验证环境外是否逐日不变。"""
    return (tuple(p["code"] for p in day.get("picks", [])),
            tuple(w["code"] for w in day.get("watch", [])),
            tuple(a["code"] for a in day.get("avoid", [])))


def main() -> None:
    smoke = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    ctx = PL.load_context()
    fs = ctx["fs"]
    regime = ctx["regime"]
    dims = ctx["dims"]
    n_d = len(fs.dates)

    codes = [c for c in fs.codes
             if c != EW_CODE and fs.meta.get(c, {}).get("level") in ("L1", "L2")]
    l12_mask = np.array([c in set(codes) for c in fs.codes])
    comp = dims["composite"].copy()
    comp_r = np.full_like(comp, np.nan)
    comp_r[l12_mask, :] = LC.xs_rank(comp[l12_mask, :])

    V, val_days, missing = LV.load_val_panel(codes, fs.dates)
    turnover, sumrate = V["turnover"], V["sumrate"]
    tp250 = LV.trailing_pct(turnover, W_MAIN, int(W_MAIN * 0.6))
    log_t = np.log(np.where(turnover > 0, turnover, np.nan))
    parts = [LC.xs_rank(-tp250), LC.xs_rank(-LV.diff_n(log_t, 60)), LC.xs_rank(-sumrate)]
    syn = 100.0 * np.nanmean(np.stack(parts), axis=0)
    crowd_full = np.full((len(fs.codes), n_d), np.nan)
    crowd_full[l12_mask, :] = LC.xs_rank(syn)
    crowd_r = crowd_full

    # ---- 回测窗口（与 pipeline.main / lab_mature_oos 完全一致）
    start_i = C.WARMUP
    if C.BACKTEST_START:
        bi = next((i for i, d in enumerate(fs.dates) if d >= C.BACKTEST_START), None)
        if bi is not None:
            start_i = max(start_i, bi)
    dates = fs.dates[start_i:]
    if smoke:
        dates = dates[-smoke:]
    print(LINE)
    print(f"回测窗口 {dates[0]} → {dates[-1]}（{len(dates)} 个交易日）· engine {PL.ENGINE_VERSION}")
    print(f"拥挤度覆盖：面板 {np.nanmean(~np.isnan(turnover)):.1%} · "
          f"有数据行业 {int((val_days > 0).sum())}/{len(codes)}")
    print(LINE)

    book = PL.load_etf_book()

    variants = [("baseline（生产 composite）", comp, None, None)]
    all_m = np.ones(n_d, bool)
    variants.append(("全局加·同族合成 w=0.30",
                     LCC.build_cond_comp(comp, comp_r, crowd_r, all_m, l12_mask, W_REF, hard=True),
                     None, None))
    m_dp = LCC.env_mask_of(regime, fs.dates, ("DEFEND", "PANIC"))
    m_pa = LCC.env_mask_of(regime, fs.dates, ("PANIC",))
    variants.append(("硬条件化 DEFEND+PANIC w=0.30",
                     LCC.build_cond_comp(comp, comp_r, crowd_r, m_dp, l12_mask, W_REF, hard=True),
                     m_dp, "DEFEND+PANIC"))
    variants.append(("硬条件化 仅PANIC w=0.30",
                     LCC.build_cond_comp(comp, comp_r, crowd_r, m_pa, l12_mask, W_REF, hard=True),
                     m_pa, "仅PANIC"))
    variants.append(("硬条件化 DEFEND+PANIC w=0.70",
                     LCC.build_cond_comp(comp, comp_r, crowd_r, m_dp, l12_mask, 0.70, hard=True),
                     m_dp, "DEFEND+PANIC(w0.7)"))

    # ---- 阈值通过数自检：确认「保分布」映射没有把高分候选压扁 ----
    thr = float(C.CLASSIFY["buy_now_min"])
    nb_all = np.mean([(comp[l12_mask, i] >= thr).sum() for i in range(n_d)])
    nbo = np.mean([(comp[l12_mask, i] >= thr).sum() for i in range(n_d) if m_dp[i]])
    print(f"阈值自检（每日 ≥ buy_now_min={thr} 的候选数）：基线 全样本 {nb_all:.1f} / "
          f"DEFEND+PANIC 日 {nbo:.1f}")
    for name, cn, m, _ in variants[1:]:
        is_global = m is None
        sub = list(range(n_d)) if is_global else [i for i in range(n_d) if m[i]]
        base_n = nb_all if is_global else nbo
        n1 = np.mean([(cn[l12_mask, i] >= thr).sum() for i in sub])
        print(f"    {name:<30} 同口径 {n1:.1f}  （Δ {n1 - base_n:+.2f}）")

    rows, concl_ref = [], None
    for name, comp_new, m, label in variants:
        print(f"\n>>> 重放变体：{name}")
        t0 = time.time()
        ctx2 = dict(ctx)
        ctx2["comp"] = comp_new
        ctx2["dims"] = {**dims, "composite": comp_new}
        concl = PL.run_range(ctx2, dates, verbose=True, book=book)
        t_run = time.time() - t0
        t1 = time.time()
        rep = RV.build_review(fs, concl, dates, ctx["fwd"], regime, book=book)
        t_rev = time.time() - t1
        st = rep["settlement"]
        s = st["stats"]
        curve = st["curve"]
        yr = year_curve(curve["dates"], curve["nav"])
        rec = {"name": name, "cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s["sharpe"],
               "calmar": s["calmar"], "avg_deployed": s["avg_deployed"],
               "executed": s["executed_trades"], "year": yr,
               "recent_cagr": cagr_between(curve["dates"], curve["nav"], "2024-01-01"),
               "recent_bench": cagr_between(curve["dates"], curve["bench"], "2024-01-01")}
        rows.append(rec)
        print(f"    年化 {num(s['cagr'], pct=True):>8}  回撤 {num(s['maxdd'], pct=True):>8}  "
              f"Sharpe {num(s['sharpe']):>7}  Calmar {num(s['calmar']):>7}  "
              f"平均投出 {num(s['avg_deployed'], pct=True):>7}  成交 {s['executed_trades']}"
              f"   [{t_run:.0f}s 重放 + {t_rev:.0f}s 结算]")
        print(f"    近端(2024起) 账户 {num(rec['recent_cagr'], pct=True):>8}  "
              f"基准 {num(rec['recent_bench'], pct=True):>8}  "
              f"超额 {num((rec['recent_cagr'] - rec['recent_bench']) if rec['recent_cagr'] == rec['recent_cagr'] else None, pct=True):>8}")
        print("    逐年：" + "  ".join(f"{y} {v:+.1%}" for y, v in sorted(yr.items())))

        if concl_ref is None:
            concl_ref = concl                    # 第一个变体即 baseline，作为对照底稿
        if label is not None and m is not None:
            # 环境外一致性：硬条件化下，环境外的「选股签名」应与基线逐日相同
            off = [d for i, d in enumerate(fs.dates) if not m[i] and d in concl]
            diff = sum(1 for d in off if sig(concl[d]) != sig(concl_ref[d]))
            print(f"    [环境外一致性] {len(off)} 个环境外交易日中，选股签名与基线不同的有 "
                  f"{diff} 天 → {'✓ 严格不变（副作用为 0）' if diff == 0 else '⚠ 存在扰动'}")

    print()
    print(LINE)
    print("汇总（相对基线）")
    print("-" * 108)
    b = rows[0]
    print(f"{'变体':<28}{'年化':>9}{'Δ年化':>9}{'回撤':>9}{'Δ回撤':>9}"
          f"{'Sharpe':>9}{'ΔSharpe':>9}{'Calmar':>9}{'成交':>7}")
    for r in rows:
        d_cagr = (r['cagr'] - b['cagr']) if (r['cagr'] is not None and b['cagr'] is not None) else None
        d_dd = (r['maxdd'] - b['maxdd']) if (r['maxdd'] is not None and b['maxdd'] is not None) else None
        d_sh = (r['sharpe'] - b['sharpe']) if (r['sharpe'] is not None and b['sharpe'] is not None) else None
        print(f"{r['name']:<28}{num(r['cagr'], pct=True):>9}"
              f"{(f'{d_cagr * 100:+.2f}%' if d_cagr is not None else 'n/a'):>9}"
              f"{num(r['maxdd'], pct=True):>9}"
              f"{(f'{d_dd * 100:+.2f}%' if d_dd is not None else 'n/a'):>9}"
              f"{num(r['sharpe']):>9}"
              f"{(f'{d_sh:+.3f}' if d_sh is not None else 'n/a'):>9}"
              f"{num(r['calmar']):>9}{r['executed']:>7}")
    print()
    print("逐年对比：")
    ys = sorted(b["year"].keys())
    print(f"{'变体':<28}" + "".join(f"{y[2:]:>8}" for y in ys))
    for r in rows:
        print(f"{r['name']:<28}" + "".join(f"{r['year'].get(y, float('nan')):>8.1%}" for y in ys))
    print()
    print(LINE)
    print("完成（未写入任何生产产物）。")


if __name__ == "__main__":
    main()
