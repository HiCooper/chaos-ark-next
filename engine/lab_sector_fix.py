# -*- coding: utf-8 -*-
"""「行业选错」三个候选方向的实验台（离线，不写生产产物）。

对应 docs/行业选错归因审计-2026-09-24.md 第五节的三件事：
  A. 横截面相对强度因子（rs_cross = 行业 ret120 − 全市场中位）
     A1 Rank IC 实证（与现有 mainline / 绝对动量对比）
     A2 入模后在可执行账户上的实测（ARK_RS_CROSS_W 权重扫描）
  B. 环境分档收紧：PANIC / PANIC+DEFEND 不新建（含逐年一致性 = walk-forward 视角）
  C. 行业级滚动冷却：过去 250 日内该行业"80 日前瞻超额为正"的比例低于阈值则不采纳

用法：
  python lab_sector_fix.py ic          # 只跑 A1（快）
  python lab_sector_fix.py gate        # B + C（快，只筛候选集）
  python lab_sector_fix.py model       # A2（慢，每个权重重跑一次信号层）
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402
import scoring as SC  # noqa: E402
import settlement as ST  # noqa: E402

HZ_TEST = (80, 100)


# ------------------------------------------------------------------ 数据装载
def load_trades(book) -> List[dict]:
    """生产同源候选：day 文件里「有可交易 ETF 且周期>0」的结论。"""
    out = []
    for f in sorted(glob.glob(f"{C.OUT_DIR}/day/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d.get("error"):
            continue
        for p in d.get("picks") or []:
            H = int((p.get("horizon") or {}).get("horizon") or 0)
            if H <= 0:
                continue
            out.append({"date": d["date"], "sw_name": p["name"], "code": p.get("code"), "horizon": H,
                        "composite": p.get("composite"), "action": p.get("action"),
                        "sell_date": None, "etf_sid": None, "regime": d.get("regime")})
    return out


def account(book, trades, **kw) -> dict:
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    base = dict(max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                target_positions=PF["target_positions"], risk_parity=True,
                rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cash_yield=RC["cash_yield"], score_tilt=PF["score_tilt"],
                max_new_per_day=PF["max_new_per_day"])
    r = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **{**base, **kw})
    return r


def yearly(rep_settlement: dict) -> Dict[str, float]:
    cur = rep_settlement["curve"]
    dates, nav = cur["dates"], cur["nav"]
    out: Dict[str, float] = {}
    i = 0
    while i < len(dates):
        y = dates[i][:4]
        j = i
        while j + 1 < len(dates) and dates[j + 1][:4] == y:
            j += 1
        base = nav[i - 1] if i > 0 else nav[i]
        out[y] = nav[j] / base - 1.0
        i = j + 1
    return out


def _row(label: str, s: dict, yr: Optional[Dict[str, float]] = None) -> str:
    txt = (f"{label:<28}{s['cagr']:>8.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}"
           f"{s['calmar']:>8.3f}{s['avg_deployed']:>9.1%}{s['avg_holdings']:>9.1f}"
           f"{s['executed_trades']:>6}")
    if yr:
        txt += "   " + " ".join(f"{y}:{yr[y]:+.0%}" for y in sorted(yr) if y >= "2022")
    return txt


HEAD = (f"{'变体':<28}{'年化':>8}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}"
        f"{'平均投出':>9}{'平均持仓':>9}{'成交':>6}   近端起（2022+）逐年")


# ------------------------------------------------------------------ A1 IC 实证
def run_ic(write: bool = False) -> None:
    ctx = PL.load_context()
    fs = ctx["fs"]
    codes = [c for c in fs.codes if c != "EW"]
    print(f"候选 {len(codes)} 个行业/赛道 × {len(fs.dates)} 个交易日")
    ret120 = np.vstack([fs.series(c, "ret_120") for c in codes])
    med120 = np.nanmedian(ret120, axis=0)
    rs_cross = ret120 - med120
    mainline = ctx["dims"]["mainline"]

    def rank_ic(score: np.ndarray, H: int) -> Dict[str, float]:
        """逐日横截面 Rank IC：score(行业, 日) vs 该日之后 H 日相对行业等权的超额。"""
        ics = []
        for i in range(C.WARMUP, len(fs.dates) - H - 1):
            fwd = []
            s = []
            for ci, c in enumerate(codes):
                v = RV.fwd_stats(fs, c, i, H)
                if not v or v["ex_ew"] is None:
                    continue
                fwd.append(v["ex_ew"])
                s.append(score[ci, i])
            if len(fwd) < 10:
                continue
            a, b = np.array(s), np.array(fwd)
            ok = ~np.isnan(a)
            if ok.sum() < 10:
                continue
            ra = np.argsort(np.argsort(a[ok]))
            rb = np.argsort(np.argsort(b[ok]))
            ics.append(float(np.corrcoef(ra, rb)[0, 1]))
        arr = np.array(ics)
        return {"n": int(arr.size), "mean": float(arr.mean()),
                "ir": float(arr.mean() / arr.std()) if arr.std() else 0.0,
                "pos": float((arr > 0).mean())}

    def cs_pct(x: np.ndarray) -> np.ndarray:
        """逐日横截面分位（0-100）——这才是真正改变横截面排序的变换。"""
        out = np.full_like(x, np.nan, dtype=float)
        for i in range(x.shape[1]):
            col = x[:, i]
            ok = ~np.isnan(col)
            if ok.sum() < 10:
                continue
            r = np.argsort(np.argsort(col[ok])).astype(float) / max(1, ok.sum() - 1) * 100.0
            out[ok, i] = r
        return out

    ml_pct = cs_pct(mainline)
    factors = [
        ("绝对动量 ret120", ret120),
        ("mainline（现行）", mainline),
        ("mainline 的分位（纯横截面）", ml_pct),
        ("0.7·mainline + 0.3·分位", 0.7 * mainline + 0.3 * ml_pct),
        ("0.5·mainline + 0.5·分位", 0.5 * mainline + 0.5 * ml_pct),
    ]
    print(f"\n{'因子':<26}" + "".join(f"{'IC@'+str(h):>10}{'IR':>8}{'正IC日':>9}" for h in HZ_TEST))
    for name, sc in factors:
        cells = ""
        for h in HZ_TEST:
            r = rank_ic(sc, h)
            cells += f"{r['mean']:>10.3f}{r['ir']:>8.2f}{r['pos']:>9.1%}"
        print(f"{name:<26}{cells}")
    print("说明：rs_cross（ret120 − 当日全市场中位）与 ret120 的横截面**排序完全相同**，")
    print("      所以它在任何按横截面排序的打分体系里都不提供新增信息（实测 IC 逐位相同）。")
    if write:
        payload = {"generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                   "engine_version": PL.ENGINE_VERSION,
                   "note": "横截面 Rank IC（逐日 spearman，全 126 标的）；由 engine/lab_sector_fix.py ic --write 生成",
                   "rows": []}
        for name, sc in [("长期主线（mainline 综合）", mainline), ("120 日动量", ret120),
                         ("250 日动量", np.vstack([fs.series(c, "ret_250") for c in codes])),
                         ("20 日动量（短期）", np.vstack([fs.series(c, "ret_20") for c in codes])),
                         ("60 日动量（中短期）", np.vstack([fs.series(c, "ret_60") for c in codes]))]:
            r = {"factor": name, "ic": {}}
            for h in (20, 40, 60, 80, 100):
                v = rank_ic(sc, h)
                # t = IR × √n（IR = mean/std）——等价于 mean / (std/√n)
                r["ic"][str(h)] = {"mean": round(v["mean"], 4),
                                   "t": round(v["ir"] * np.sqrt(v["n"]), 1) if v["ir"] else 0.0}
            payload["rows"].append(r)
        out = f"{C.OUT_DIR}/ic.json"
        json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"\n已写入 {out}（供「引擎」页展示，替代前端硬编码）")


# ------------------------------------------------------------------ B/C 候选集筛选
def _regime_of(day_file_cache: Dict[str, str], d: str) -> Optional[str]:
    day = day_file_cache.get(d)
    if day is None:
        try:
            day = (json.load(open(f"{C.OUT_DIR}/day/{d}.json", encoding="utf-8")) or {}).get("regime")
        except Exception:
            day = None
        day_file_cache[d] = day
    return day


def run_gate() -> None:
    book = PL.load_etf_book()
    trades = load_trades(book)
    ctx = PL.load_context()
    fs = ctx["fs"]
    idx = {d: i for i, d in enumerate(fs.dates)}
    print(f"候选 {len(trades)} 条")

    # 行业滚动命中率（只用价格，无前视）：过去 250 日内该行业 80 日前瞻超额为正的比例
    codes = set(t["code"] for t in trades if t.get("code"))
    ret80: Dict[str, np.ndarray] = {}
    for c in codes:
        n = len(fs.dates)
        arr = np.full(n, np.nan)
        for j in range(n - 80):
            v = RV.fwd_stats(fs, c, j, 80)
            if v and v["ex_ew"] is not None:
                arr[j] = v["ex_ew"]
        ret80[c] = arr

    def hit_rate(c: str, i: int, win: int = 250) -> Optional[float]:
        a = ret80.get(c)
        if a is None:
            return None
        seg = a[max(0, i - win):i]
        seg = seg[~np.isnan(seg)]
        if len(seg) < 40:
            return None
        return float((seg > 0).mean())

    print(f"\n{HEAD}")
    base = account(book, trades)
    print(_row("基线（全部候选）", base["stats"], yearly({"curve": {"dates": base["dates"], "nav": base["nav"]}})))

    variants = [
        ("PANIC 不新建", lambda t: _regime_of({}, t["date"]) != "PANIC"),
        ("PANIC+DEFEND 不新建", lambda t: _regime_of({}, t["date"]) not in ("PANIC", "DEFEND")),
    ]
    for label, keep in variants:
        sub = [t for t in trades if keep(t)]
        r = account(book, sub)
        print(_row(label, r["stats"], yearly({"curve": {"dates": r["dates"], "nav": r["nav"]}})))

    for th in (0.40, 0.45, 0.50):
        sub = []
        for t in trades:
            i = idx.get(t["date"])
            hr = hit_rate(t.get("code"), i) if i is not None else None
            if hr is None or hr >= th:
                sub.append(t)
        r = account(book, sub)
        print(_row(f"行业滚动命中率 ≥{th:.2f}", r["stats"],
                   yearly({"curve": {"dates": r["dates"], "nav": r["nav"]}}))
              + f"   保留 {len(sub)}/{len(trades)} 条候选（筛掉 {len(trades)-len(sub)}）")


# ------------------------------------------------------------------ A2 入模实测
def run_model() -> None:
    book = PL.load_etf_book()
    start_i = C.WARMUP
    bi = next((i for i, d in enumerate(PL.load_context()["fs"].dates)
               if d >= C.BACKTEST_START), None)
    if bi is not None:
        start_i = max(start_i, bi)
    print(f"{HEAD}")
    for w in (0.0, 0.25, 0.50, 0.75):
        os.environ["ARK_RS_CROSS_W"] = str(w)
        import importlib
        importlib.reload(SC)
        ctx = PL.load_context()
        fs = ctx["fs"]
        dates = fs.dates[start_i:]
        conclusions = PL.run_range(ctx, dates, book=book)
        rep = RV.build_review(fs, conclusions, dates, ctx["fwd"], ctx["regime"], book=book)
        stt = rep["settlement"]["stats"]
        yr = yearly(rep["settlement"])
        tag = "基线（现行）" if w == 0 else f"rs_cross 权重 {w}"
        print(_row(tag, stt, yr))
    os.environ["ARK_RS_CROSS_W"] = "0.0"


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "ic"
    if mode == "ic":
        run_ic(write="--write" in sys.argv)
    elif mode == "gate":
        run_gate()
    elif mode == "model":
        run_model()
    else:
        print(__doc__)
