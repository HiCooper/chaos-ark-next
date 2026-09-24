# -*- coding: utf-8 -*-
"""MATURE 降权系数的样本外审计（离线，不写任何生产产物）。

背景：2026-09-22 依据 lab_mature_weight 落参 `mature_weight = 0.7`（成熟期主线综合分降权），
2026-09-24 回退到 1.0（旧行为）。config 注释引用了「walk-forward 审计 engine/oos_b.py」，
但该脚本并未入库 —— 本脚本用**生产同源代码路径**复现该结论：

    对每个 mw ∈ {0.7, 1.0}：
      1) 用同一份特征库重建当日结论（classify.MATURE_WEIGHT 覆盖，等价于改 config）；
      2) 走生产 `review.build_review` 的结算口径（真实 ETF、可执行账户）；
      3) 输出全窗口指标 + 逐年收益 + 「近端（2024 起）」的年化，看假设是否在样本外成立。

注意：这是**同一份历史**上的两个变体比较，不是真正的滚动样本外；「近端」只用来检验
「mw=0.7 的降权假设在最近的行情里是否仍然成立」——按因子挖掘的规矩，未被数据支持的
假设应当被拒绝（这正是回退 1.0 的理由）。

用法：python lab_mature_oos.py            # 默认比较 0.7 与 1.0
      python lab_mature_oos.py 0.5 0.7 1.0
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import classify as CL  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402


def _year_curve(dates: List[str], nav: List[float]) -> Dict[str, float]:
    """按自然年切分的收益（起点 = 上一年最后一个交易日收盘，与前端图表口径一致）。"""
    out: Dict[str, float] = {}
    for i, d in enumerate(dates):
        y = d[:4]
        if y in out:
            continue
        base = nav[i - 1] if i > 0 else nav[i]
        # 该年最后一个交易日
        j = i
        while j + 1 < len(dates) and dates[j + 1][:4] == y:
            j += 1
        out[y] = nav[j] / base - 1.0 if base else 0.0
    return out


def _cagr_between(dates: List[str], nav: List[float], start: str) -> float:
    i = next((k for k, d in enumerate(dates) if d >= start), None)
    if i is None:
        return float("nan")
    base = nav[i - 1] if i > 0 else nav[i]
    years = (len(nav) - i) / 243.0
    return (nav[-1] / base) ** (1.0 / years) - 1.0 if years > 0 and base else float("nan")


def run_variant(mw: float, ctx: dict, book, dates: List[str]) -> dict:
    CL.MATURE_WEIGHT = mw                      # 生产代码读的就是这个模块常量
    conclusions = PL.run_range(ctx, dates, book=book)
    rep = RV.build_review(ctx["fs"], conclusions, dates, ctx["fwd"], ctx["regime"], book=book)
    st = rep["settlement"]
    s = st["stats"]
    curve = st["curve"]
    yr = _year_curve(curve["dates"], curve["nav"])
    return {
        "mw": mw,
        "cagr": s["cagr"], "maxdd": s["maxdd"], "sharpe": s["sharpe"], "calmar": s["calmar"],
        "avg_deployed": s["avg_deployed"], "executed": s["executed_trades"],
        "year": yr,
        "recent_cagr": _cagr_between(curve["dates"], curve["nav"], "2024-01-01"),
        "recent_bench": _cagr_between(curve["dates"], curve["bench"], "2024-01-01"),
    }


def main() -> None:
    mws = [float(x) for x in sys.argv[1:]] or [0.7, 1.0]
    ctx = PL.load_context()
    fs = ctx["fs"]
    book = PL.load_etf_book()
    # 与 pipeline 完全一致的回测窗口
    start_i = C.WARMUP
    if C.BACKTEST_START:
        bi = next((i for i, d in enumerate(fs.dates) if d >= C.BACKTEST_START), None)
        if bi is not None:
            start_i = max(start_i, bi)
    dates = fs.dates[start_i:]
    print(f"窗口 {dates[0]} → {dates[-1]}（{len(dates)} 个交易日）· "
          f"engine {PL.ENGINE_VERSION}\n")

    rows = []
    for mw in mws:
        print(f"=== mature_weight = {mw} ===")
        r = run_variant(mw, ctx, book, dates)
        rows.append(r)
        print(f"  全窗口 年化 {r['cagr']:>7.2%}  回撤 {r['maxdd']:>7.2%}  Sharpe {r['sharpe']:.3f}  "
              f"Calmar {r['calmar']:.3f}  平均投出 {r['avg_deployed']:.1%}  成交 {r['executed']}")
        print(f"  近端（2024-01 起）账户年化 {r['recent_cagr']:>7.2%}  基准 {r['recent_bench']:>7.2%}  "
              f"超额 {r['recent_cagr'] - r['recent_bench']:>+7.2%}")
        print("  逐年：" + "  ".join(f"{y} {v:+.1%}" for y, v in sorted(r["year"].items())))

    print("\n对比（近端 2024 起的账户年化 / 全窗口年化）：")
    for r in rows:
        print(f"  mw={r['mw']:<4} 近端 {r['recent_cagr']:>+7.2%}  全窗口 {r['cagr']:>+7.2%}  "
              f"回撤 {r['maxdd']:>7.2%}  Calmar {r['calmar']:.3f}")


if __name__ == "__main__":
    main()
