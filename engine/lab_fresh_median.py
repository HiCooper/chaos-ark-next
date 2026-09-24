# -*- coding: utf-8 -*-
"""「随机入场 · 持有一年（中位 +4.45%）」为什么这么差 —— 归因 + 候选突破方向实验台。

背景：复盘页 `settlement.stats.rolling_1y_fresh`（1509 个入市日各自从现金起账、持有 252 日）
中位 +4.45% < 同期基准中位 +5.11%。本脚本只做**诊断与反事实**，不写任何生产产物、
不改 config（结论出来后再单独决定是否落参）。

三个模式：
  diag   ① 精确复现生产 rolling_1y_fresh；② 按入市年份拆分布；③ 参与度/爬坡剖面；
         ④ 参与度与一年收益的关系；⑤ 近失候选（watch/ranking 次优）的前瞻质量
  park   闲置现金「β 兜底」：把未投出的现金按比例停泊在基准 ETF 上（core-satellite），
         看独立起账一年的分布与全窗口 Calmar 怎么变
  k      目标持仓笔数 K 对「随机入场一年」分布的影响（爬坡速度假设）

用法：
  python lab_fresh_median.py diag
  python lab_fresh_median.py park
  python lab_fresh_median.py k
"""
from __future__ import annotations

import glob
import json
import os
import sys
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402
import settlement as ST  # noqa: E402

HORIZON = 252
CACHE = "/tmp/fresh_base.json"


# ------------------------------------------------------------------ 数据装载
def first_tradable_i(book) -> int:
    """生产 rolling_fresh_entry 的起点：**首个「有可交易 ETF」的候选日**（2019-06-17）。

    不是数据起点，也不是 BACKTEST_START：ETF 上市前的候选被结算层跳过，
    所以「我某天入场」的最早可行日就是这只 ETF 真正可买的那天。
    """
    out = None
    for f in sorted(glob.glob(f"{C.OUT_DIR}/day/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d["date"] < C.BACKTEST_START:
            continue
        for p in d.get("picks") or []:
            if int((p.get("horizon") or {}).get("horizon") or 0) <= 0:
                continue
            if book.etf_of(p["name"], d["date"]):
                i = book.idx.get(d["date"])
                if i is not None and (out is None or i < out):
                    out = i
    return out or 0


def load_trades() -> List[dict]:
    """生产同源候选（与 lab_sector_fix.load_trades 一致）：day 文件里周期>0 的结论。"""
    out = []
    for f in sorted(glob.glob(f"{C.OUT_DIR}/day/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d.get("error"):
            continue
        for p in d.get("picks") or []:
            H = int((p.get("horizon") or {}).get("horizon") or 0)
            if H <= 0:
                continue
            out.append({"date": d["date"], "sw_name": p["name"], "code": p.get("code"),
                        "horizon": H, "composite": p.get("composite"),
                        "action": p.get("action"), "sell_date": None, "etf_sid": None})
    return out


def sim_kw(target_positions: Optional[int] = None) -> dict:
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    return dict(max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
                target_positions=target_positions or PF["target_positions"],
                risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
                vol_target=RC["vol_target"], vol_window=RC["vol_window"],
                vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
                score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])


def full_window(book, trades, **over) -> dict:
    kw = sim_kw()
    kw.update(over)
    return ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **kw)


# ------------------------------------------------------------------ β 兜底 overlay
def park_overlay(nav, bench, expo, events, dates, f: float, cost: float = 0.0) -> np.ndarray:
    """把「未投出的现金」按比例 f 停泊在基准 ETF 上（其余仍是不计息现金）。

    记账方式（逐日、与 nav 序列对齐）：
      · 账户每日市值 mv_k = nav_k × expo_k 不随停泊改变（停泊只影响闲置那一侧，
        而闲置侧的取用总能靠卖出停泊份额满足，所以建仓路径与基线逐位一致）；
      · 停泊份额 u 的变动：初始本金闲置部分按 B_0 买入 f 份；每笔建仓从闲置里取走 A，
        其中只有 f·A 来自停泊份额；每笔卖出所得 P 再停泊 f·P；
      · hybrid_k = mv_k + (1-f)·cash_k + u_k·B_k（未停泊的闲置现金仍按 0 计息）。
    f=1 且账户从不交易时退化为「满仓基准」，f=0 时与基线逐位相同，可作自检。
    """
    n = len(nav)
    B = np.asarray(bench, dtype=float)
    u = f * 1.0 / B[0]
    hyb = np.empty(n, dtype=float)
    by_day: Dict[int, List[dict]] = {}
    for ev in events:
        by_day.setdefault(int(ev.get("i", 0)), []).append(ev)
    for k in range(n):
        for ev in by_day.get(k, []):
            amt = float(ev.get("amt") or 0.0)
            if ev.get("k") == "B":
                u -= f * amt * (1.0 + cost) / B[k]
            elif ev.get("k") == "S":
                u += f * amt * (1.0 - cost) / B[k]
        e = float(expo[k])
        cash = nav[k] * (1.0 - e)
        hyb[k] = nav[k] * e + (1.0 - f) * cash + max(u, 0.0) * B[k]
    return hyb


def dist(a: np.ndarray) -> dict:
    return {"n": int(a.size), "mean": round(float(a.mean()), 4),
            "median": round(float(np.median(a)), 4),
            "win": round(float((a > 0).mean()), 3),
            "p5": round(float(np.percentile(a, 5)), 4),
            "worst": round(float(a.min()), 4),
            "best": round(float(a.max()), 4)}


# ------------------------------------------------------------------ diag
def run_diag() -> None:
    book = PL.load_etf_book()
    trades = load_trades()
    dates = book.dates
    bsid = book.bench_etf
    first = first_tradable_i(book)
    kw = sim_kw()
    rows, near = [], []
    print(f"入市日起点 {dates[first]}（首个可交易候选日）→ 共 {len(dates) - HORIZON - 1 - first} 个入市日")

    for i in range(first, max(first, len(dates) - HORIZON - 1)):
        r = ST.simulate_cash_account(book, trades, start_date=dates[i],
                                     end_date=dates[i + HORIZON], **kw)
        nav = r["nav"]
        if len(nav) <= HORIZON:
            continue
        b0 = book.close(bsid, book.idx[r["dates"][0]])
        b1 = book.close(bsid, book.idx[r["dates"][HORIZON]])
        ret = nav[HORIZON] / nav[0] - 1.0
        expo = np.array(r["exposure"], dtype=float)
        rows.append({
            "start": r["dates"][0], "year": r["dates"][0][:4], "ret": ret,
            "bench": (b1 / b0 - 1.0) if (b0 and b1) else None,
            "dep": float(expo.mean()),
            "dep0_60": float(expo[:60].mean()),
            "dep60_120": float(expo[60:120].mean()),
            "dep120": float(expo[120:].mean()),
            "hold": float(np.mean(r["holdings"])),
            "nav": [round(x, 5) for x in nav],
            "bench_nav": [round(x, 5) for x in r["bench"]],
            "expo": [round(x, 4) for x in expo],
            "events": [{"i": e["i"], "k": e["k"], "amt": e["amt"]} for e in r["events"]],
            "dates": r["dates"],
        })

    a = np.array([x["ret"] for x in rows])
    b = np.array([x["bench"] for x in rows if x["bench"] is not None])
    dep = np.array([x["dep"] for x in rows])
    print(f"复现生产 rolling_1y_fresh：n={a.size}  均值 {a.mean():+.2%}  中位 {np.median(a):+.2%}  "
          f"为正 {(a > 0).mean():.1%}  p5 {np.percentile(a, 5):+.2%}  最差 {a.min():+.2%}")
    print(f"                  同期基准：均值 {b.mean():+.2%}  中位 {np.median(b):+.2%}  "
          f"跑赢基准 {(a > b).mean():.1%}" if b.size == a.size else "")
    print("\n按入市年份拆（独立起账一年）")
    print(f"{'入市年':<8}{'样本':>6}{'中位':>10}{'均值':>10}{'基准中位':>11}{'为正':>8}"
          f"{'跑赢基准':>10}{'平均投出':>10}{'前60日投出':>11}{'平均持仓':>10}")
    for y in sorted({x["year"] for x in rows}):
        g = [x for x in rows if x["year"] == y]
        ga = np.array([x["ret"] for x in g])
        gb = np.array([x["bench"] for x in g if x["bench"] is not None])
        print(f"{y:<8}{len(g):>6}{np.median(ga):>10.2%}{ga.mean():>10.2%}"
              f"{np.median(gb):>11.2%}{(ga > 0).mean():>8.1%}{(ga > gb).mean():>10.1%}"
              f"{np.mean([x['dep'] for x in g]):>10.1%}{np.mean([x['dep0_60'] for x in g]):>11.1%}"
              f"{np.mean([x['hold'] for x in g]):>10.1f}")

    print("\n参与度 → 一年收益（按入市日平均投出三分位）")
    q = np.quantile(dep, [1 / 3, 2 / 3])
    for lab, m in (("低（<%.0f%%）" % (q[0] * 100), dep <= q[0]),
                   ("中", (dep > q[0]) & (dep <= q[1])),
                   ("高（>%.0f%%）" % (q[1] * 100), dep > q[1])):
        idx = np.where(m)[0]
        print(f"  {lab:<14} n={idx.size:>5}  中位 {np.median(a[idx]):+7.2%}  "
              f"均值 {a[idx].mean():+7.2%}  为正 {(a[idx] > 0).mean():5.1%}  "
              f"跑赢基准 {(a[idx] > b[idx]).mean():5.1%}")
    r = float(np.corrcoef(dep, a)[0, 1])
    print(f"  corr(平均投出, 一年收益) = {r:+.3f}")
    print(f"  全样本平均投出 {dep.mean():.1%}｜前 60 日 {np.mean([x['dep0_60'] for x in rows]):.1%}"
          f"｜60-120 日 {np.mean([x['dep60_120'] for x in rows]):.1%}"
          f"｜120 日后 {np.mean([x['dep120'] for x in rows]):.1%}")

    json.dump(rows, open(CACHE, "w"), ensure_ascii=False)
    print(f"\n逐起点明细已缓存到 {CACHE}（供 park 模式复用）")


# ------------------------------------------------------------------ park
def run_park() -> None:
    book = PL.load_etf_book()
    trades = load_trades()
    print("全窗口（2018-01-02 起，同一条曲线）")
    print(f"{'停泊比例':<10}{'年化':>9}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}{'平均投出':>9}")
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        r = full_window(book, trades)
        if f == 0.0:
            s = r["stats"]
        else:
            hyb = park_overlay(r["nav"], r["bench"], r["exposure"], r["events"], r["dates"],
                               f, cost=0.0007)
            navs = np.array(r["nav"])
            hyb = hyb / hyb[0]
            navs = navs / navs[0]
            s = ST.perf_stats(hyb, r["dates"], np.array(r["bench"]))
            s["avg_deployed"] = float(np.mean(hyb / navs * np.array(r["exposure"])))
        print(f"{f:<10.0%}{s['cagr']:>9.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}"
              f"{s['calmar']:>8.3f}{s['avg_deployed']:>9.1%}")

    if not os.path.exists(CACHE):
        print("缺 cache，先跑 diag")
        return
    rows = json.load(open(CACHE))
    print("\n独立起账一年（1509 个入市日）")
    print(f"{'停泊比例':<10}{'中位':>10}{'均值':>10}{'为正':>8}{'p5':>10}{'最差':>10}"
          f"{'基准中位':>11}{'跑赢基准':>10}")
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        rets, brs = [], []
        for x in rows:
            if f == 0.0:
                h = np.array(x["nav"])
            else:
                h = park_overlay(np.array(x["nav"]), np.array(x["bench_nav"]),
                                 np.array(x["expo"]), x["events"], x["dates"], f, cost=0.0007)
            rets.append(h[HORIZON] / h[0] - 1.0)
            brs.append(x["bench"])
        d = dist(np.array(rets))
        br = np.array(brs)
        print(f"{f:<10.0%}{d['median']:>10.2%}{d['mean']:>10.2%}{d['win']:>8.1%}"
              f"{d['p5']:>10.2%}{d['worst']:>10.2%}{np.median(br):>11.2%}"
              f"{(np.array(rets) > br).mean():>10.1%}")


# ------------------------------------------------------------------ k
def run_k() -> None:
    book = PL.load_etf_book()
    trades = load_trades()
    dates = book.dates
    first = first_tradable_i(book)
    bsid = book.bench_etf
    print("独立起账一年 × 目标持仓笔数 K（爬坡速度假设：K 越小单笔越大、建仓越快）")
    print(f"{'K':<6}{'中位':>10}{'均值':>10}{'为正':>8}{'p5':>10}{'最差':>10}{'跑赢基准':>10}"
          f"{'平均投出':>10}{'前60日投出':>11}")
    for K in (8, 12, 16, 20, 24, 32):
        kw = sim_kw(target_positions=K)
        rets, brs, deps, d60 = [], [], [], []
        for i in range(first, max(first, len(dates) - HORIZON - 1)):
            r = ST.simulate_cash_account(book, trades, start_date=dates[i],
                                         end_date=dates[i + HORIZON], **kw)
            nav = r["nav"]
            if len(nav) <= HORIZON:
                continue
            rets.append(nav[HORIZON] / nav[0] - 1.0)
            b0 = book.close(bsid, book.idx[r["dates"][0]])
            b1 = book.close(bsid, book.idx[r["dates"][HORIZON]])
            brs.append((b1 / b0 - 1.0) if (b0 and b1) else np.nan)
            e = np.array(r["exposure"], dtype=float)
            deps.append(float(e.mean()))
            d60.append(float(e[:60].mean()))
        a = np.array(rets)
        b = np.array(brs)
        print(f"{K:<6}{np.median(a):>10.2%}{a.mean():>10.2%}{(a > 0).mean():>8.1%}"
              f"{np.percentile(a, 5):>10.2%}{a.min():>10.2%}"
              f"{np.nanmean(a > b):>10.1%}{np.mean(deps):>10.1%}{np.mean(d60):>11.1%}")


def load_probe_trades(min_picks: int = 2, max_comp: Optional[float] = None,
                      only_zero_day: bool = False, cap: int = 2) -> List[dict]:
    """近失候选兜底（「试探仓」思路的等额版本）：结论不足时，用 ranking 里 action=WATCH
    的次优候选补到每天 cap 笔。horizon 用 ranking 的 hz_picked（80/100，与生产同源）。

    注意：sim 没有「单笔减仓」参数，所以这里是**等额买入**（比真正的试探仓更激进）。
    若等额都亏，缩小仓位的版本只会衰减亏损幅度，方向即可判死。
    """
    out = []
    for f in sorted(glob.glob(f"{C.OUT_DIR}/day/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d.get("error") or d["date"] < C.BACKTEST_START:
            continue
        picks = d.get("picks") or []
        if len(picks) >= min_picks:
            continue
        if only_zero_day and picks:
            continue
        watch = [r for r in (d.get("ranking") or []) if r.get("action") == "WATCH"]
        watch.sort(key=lambda r: -(r.get("composite") or 0))
        room = cap - len(picks)
        for r in watch[:max(0, room)]:
            if r.get("composite") is None:
                continue
            if max_comp is not None and r["composite"] > max_comp:
                continue
            out.append({"date": d["date"], "sw_name": r["name"], "code": r.get("code"),
                        "horizon": int(r.get("hz_picked") or 80),
                        "composite": r.get("composite"), "action": "WATCH",
                        "sell_date": None, "etf_sid": None})
    return out


def fresh_stats(book, trades, K: Optional[int] = None, f_park: float = 0.0, **over) -> dict:
    """独立起账一年的分布（生产口径：起点 = 首个可交易候选日）。"""
    dates = book.dates
    first = first_tradable_i(book)
    bsid = book.bench_etf
    kw = sim_kw(target_positions=K)
    kw.update(over)
    rets, brs = [], []
    for i in range(first, max(first, len(dates) - HORIZON - 1)):
        r = ST.simulate_cash_account(book, trades, start_date=dates[i],
                                     end_date=dates[i + HORIZON], **kw)
        nav = r["nav"]
        if len(nav) <= HORIZON:
            continue
        if f_park:
            h = park_overlay(nav, r["bench"], r["exposure"], r["events"], r["dates"],
                             f_park, cost=0.0007)
        else:
            h = nav
        rets.append(h[HORIZON] / h[0] - 1.0)
        b0 = book.close(bsid, book.idx[r["dates"][0]])
        b1 = book.close(bsid, book.idx[r["dates"][HORIZON]])
        brs.append((b1 / b0 - 1.0) if (b0 and b1) else np.nan)
    a, b = np.array(rets), np.array(brs)
    return {"median": np.median(a), "mean": a.mean(), "win": (a > 0).mean(),
            "p5": np.percentile(a, 5), "worst": a.min(), "beat": np.nanmean(a > b)}


# ------------------------------------------------------------------ probe
def run_probe() -> None:
    book = PL.load_etf_book()
    picks = load_trades()
    base = full_window(book, picks)
    s = base["stats"]
    print(f"{'变体':<34}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'平均投出':>9}{'成交':>7}"
          f"{'一年中位':>10}{'为正':>8}{'跑赢基准':>10}")
    print(f"{'基线（仅共振结论）':<34}{s['cagr']:>9.2%}{s['maxdd']:>9.2%}{s['calmar']:>8.3f}"
          f"{s['avg_deployed']:>9.1%}{s['executed_trades']:>7}"
          + "".join(f"{v:>10.2%}" for v in (fresh_stats(book, picks)['median'],)) + "")
    variants = [
        ("零结论日补 1 笔 WATCH", load_probe_trades(only_zero_day=True, cap=1)),
        ("零结论日补 2 笔 WATCH", load_probe_trades(only_zero_day=True, cap=2)),
        ("不足 2 笔就补满（含 1 笔日）", load_probe_trades(min_picks=2, cap=2)),
        ("只补 composite≤50 的近失候选", load_probe_trades(min_picks=2, cap=2, max_comp=50.0)),
    ]
    for label, extra in variants:
        tr = picks + extra
        r = full_window(book, tr)
        st = r["stats"]
        fr = fresh_stats(book, tr)
        print(f"{label:<34}{st['cagr']:>9.2%}{st['maxdd']:>9.2%}{st['calmar']:>8.3f}"
              f"{st['avg_deployed']:>9.1%}{st['executed_trades']:>7}"
              f"{fr['median']:>10.2%}{fr['win']:>8.1%}{fr['beat']:>10.1%}"
              f"   补 {len(extra)} 笔候选")


# ================================================================== 纯标的族
# 约束（用户口径，2026-09-24）：**不得混入策略自己信号集之外的任何标的**。
# 因此下面三个方向的共同前提是——候选集就是 `load_trades()` 那 2125 条共振结论、
# 标的池就是它们映射到的行业 ETF，一行不加。只改「持有多久 / 一天能建几笔 /
# 闲置现金的收益率」。

def scale_horizon(trades: List[dict], m: float) -> List[dict]:
    """把每笔的持有期 ×m（80/100 → 100/125、120/150 …）。标的与信号集不变。"""
    return [dict(t, horizon=max(1, int(round(t["horizon"] * m)))) for t in trades]


def _row(trades: List[dict], book, **over) -> str:
    r = full_window(book, trades, **over)
    s = r["stats"]
    fr = fresh_stats(book, trades, **over)
    return (f"{s['cagr']:>9.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}{s['calmar']:>8.3f}"
            f"{s['avg_deployed']:>9.1%}{s.get('avg_holdings', float('nan')):>9.1f}"
            f"{s['executed_trades']:>7}{s.get('blocked_by_capacity', 0):>7}"
            f"{s.get('blocked_by_cash', 0):>7}{fr['median']:>10.2%}{fr['win']:>8.1%}"
            f"{fr['beat']:>10.1%}")


HDR = (f"{'变体':<20}{'年化':>9}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}{'平均投出':>9}"
       f"{'平均持仓':>9}{'成交':>7}{'容量挡':>7}{'现金挡':>7}{'一年中位':>10}{'为正':>8}{'跑赢基准':>10}")


def run_hold() -> None:
    """P1｜延长持有期：到期不急着回现金，让资金留在自己的标的上。"""
    book = PL.load_etf_book()
    trades = load_trades()
    print("P1｜延长持有期（标的池 / 信号集一行不改，只改「持有多久」）")
    print(HDR)
    for m, lab in ((1.0, "80/100（现行）"), (1.25, "100/125"), (1.5, "120/150"),
                   (2.0, "160/200"), (2.5, "200/250"), (3.0, "240/300")):
        print(f"{lab:<20}" + _row(scale_horizon(trades, m), book))
    print("\nP1b｜延长持有 + 破位退出（连续 2 日收于 MA20 下 → 次日开盘平仓，仍是自己的标的）")
    print(HDR)
    for m, lab in ((1.0, "80/100 + 破位"), (1.5, "120/150 + 破位"), (2.0, "160/200 + 破位"),
                   (3.0, "240/300 + 破位")):
        print(f"{lab:<20}" + _row(scale_horizon(trades, m), book, break_days=2, break_ma=20))


def run_flow() -> None:
    """P2｜单日吞吐：信号密集日不再只取 2 笔（资金仍是同一池标的）。"""
    book = PL.load_etf_book()
    trades = load_trades()
    print("P2｜放宽「每日最多建仓 2 笔」——信号密集日的其余结论也进（同一池标的）")
    print(HDR)
    for n in (2, 3, 4, 6, 8):
        print(f"{'max_new_per_day=' + str(n):<20}" + _row(trades, book, max_new_per_day=n))


def run_cash() -> None:
    """P3｜闲置现金计息：不加任何标的，只把 cash_yield 从 0 抬起。"""
    book = PL.load_etf_book()
    trades = load_trades()
    print("P3｜闲置现金按无风险利率计息（**不新增任何标的**，只改闲置资金的收益率）")
    print(HDR)
    for cy in (0.0, 0.01, 0.015, 0.02, 0.025):
        print(f"{'cash_yield=' + format(cy, '.1%'):<20}" + _row(trades, book, cash_yield=cy))


def roll_trades(trades: List[dict], book, max_ext: Optional[int] = None) -> List[dict]:
    """P4 的核心：**到期日之后若一个结论都没有，就继续持有到下一个结论日**（封顶 max_ext 日）。

    与 P1「无脑把所有持有期 ×2」的区别：密集期一笔都不改（槽位立刻被新信号接上），
    只有信号饥荒时仓位才留下来 —— 即"延长"只作用在本来会空闲的槽位上。
    """
    import bisect
    sig_i = sorted({book.idx[t["date"]] for t in trades if t["date"] in book.idx})
    out = []
    for t in trades:
        i0 = book.idx.get(t["date"])
        if i0 is None:
            out.append(t)
            continue
        exp_i = i0 + 1 + int(t["horizon"])
        j = bisect.bisect_right(sig_i, exp_i)
        if j >= len(sig_i):
            out.append(t)
            continue
        nxt = sig_i[j]
        if max_ext is not None:
            nxt = min(nxt, exp_i + max_ext)
        nt = dict(t)
        nt["sell_date"] = book.dates[nxt]
        out.append(nt)
    return out


def run_roll() -> None:
    """P4｜到期只在「下一天有结论」时才换手，否则续持（封顶 max_ext 个交易日）。"""
    book = PL.load_etf_book()
    trades = load_trades()
    print("P4｜到期续持到下一个结论日（只在槽位本来会空闲时才延长；标的与信号集不变）")
    print(HDR)
    print(f"{'基线 80/100':<20}" + _row(trades, book))
    for cap in (20, 40, 60, 90, None):
        lab = f"续持封顶 {cap} 日" if cap else "续持不封顶"
        print(f"{lab:<20}" + _row(roll_trades(trades, book, cap), book))


def run_ext() -> None:
    """P5｜引擎原生的「到期仍在 MA 上方就延展一次」（extend_on_trend）。"""
    book = PL.load_etf_book()
    trades = load_trades()
    print("P5｜引擎原生到期延展（到期日收盘仍在 MA20 上方 → 再延 extend_days 日，只延一次）")
    print(HDR)
    print(f"{'基线 80/100':<20}" + _row(trades, book))
    for ed in (20, 40, 60, 100):
        print(f"{'延展 ' + str(ed) + ' 日':<20}"
              + _row(trades, book, extend_on_trend=True, extend_days=ed))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "diag"
    {"diag": run_diag, "park": run_park, "k": run_k, "probe": run_probe,
     "hold": run_hold, "flow": run_flow, "cash": run_cash,
     "roll": run_roll, "ext": run_ext}.get(mode, run_diag)()

