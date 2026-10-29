# -*- coding: utf-8 -*-
"""实验 A：**抄底通道**（离线，不写产物、不改生产配置）。

背景（2026-10-29 研究笔记，见 calibration-log.md）：
申万 120 个 L1/L2 序列 2018 起，未来 20 日相对沪深300 超额：

    条件                            事件数   超额胜率   超额均值    最差
    全样本                             —     49.0%   +0.24%   -67.7%
    20 日跌幅 ≤ -15%                 14862    64.7%   +3.08%   -35.0%
    同上 且 较近 5 日低点回升 > 3%      4418    72.8%   +4.64%   -20.9%

所以本实验只改一件事：在**主策略之外**加一条独立的抄底通道（独立 sleeve，20% 预算），
规则就是上面那两条件 + 持有 20 个交易日（与统计口径一致）。

三条口径严格分开：
  · 基线    = 现有生产策略（K=24、每日最多 2 笔、到期卖出），本金 100%
  · 抄底    = 仅抄底通道，本金 20%，单日可用额度 = 20% × 昨日账户总值，累计敞口 ≤ 20%
  · 组合    = 80% × 基线净值 + 20% × 抄底净值（两个 sleeve 相加，**不做任何再平衡**）

净值可加性成立的前提：`simulate_cash_account` 对初始本金是线性的（预算与现金约束都随 V 等比缩放），
所以 80% 本金跑出来的曲线 = 0.8 × 基线曲线。

用法：python lab_dip.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import settlement as ST  # noqa: E402

# ---- 抄底规则（与统计口径一致，先不调参） ----
DD_WINDOW = 20          # 跌幅窗口：20 个交易日
DD_TRIGGER = -0.15      # 触发：20 日跌幅 ≤ -15%
REB_WINDOW = 5          # 回升窗口：近 5 日最低点
REB_TRIGGER = 0.03      # 触发：较 5 日低点回升 > 3%
HOLD = 20               # 持有 20 个交易日（= 统计的超额窗口）
SLEEVE_CAP = 0.20       # 抄底通道敞口上限（占 sleeve 账户总值）
MAX_PER_DAY = 10        # 单日最多开几个抄底仓（分散，避免一天全压）


def _stats(nav: List[float], dates: List[str]) -> dict:
    nav = np.asarray(nav, dtype=float)
    n = len(nav)
    if n < 2:
        return {}
    years = n / 252.0
    cagr = (nav[-1] / nav[0]) ** (1 / years) - 1 if nav[0] > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1
    maxdd = float(dd.min())
    ret = np.diff(nav) / nav[:-1]
    vol = float(np.std(ret) * np.sqrt(252)) if len(ret) else 0.0
    sharpe = float(np.mean(ret) * 252 / vol) if vol > 0 else 0.0
    return {"cagr": float(cagr), "maxdd": maxdd, "vol": vol, "sharpe": sharpe,
            "calmar": float(cagr / abs(maxdd)) if maxdd < 0 else float("inf"),
            "total": float(nav[-1] / nav[0] - 1), "years": years}


def dip_signals(panel: dict, book, dd_trig: float = DD_TRIGGER,
                reb_trig: float = REB_TRIGGER) -> Dict[int, List[dict]]:
    """按抄底规则生成信号：{买入日索引(账本日历): [ {sid, sw, trigger_date, dd, reb} ]}"""
    dates = book.dates
    idx = book.idx
    out: Dict[int, List[dict]] = {}
    meta = panel.get("meta") or {}
    series = panel.get("series") or {}
    sw_to_etf = book.sw_to_etf or {}
    for code, s in series.items():
        lv = (meta.get(code) or {}).get("level")
        if lv not in ("L1", "L2"):
            continue
        name = (meta.get(code) or {}).get("name")
        sid = sw_to_etf.get(name)
        if not sid:
            continue
        ds = [d for d in dates if d in s]      # 与账本日历对齐（panel 日期 ⊂ 账本日期）
        if len(ds) < DD_WINDOW + 2:
            continue
        closes = np.array([s[d]["close"] for d in ds], dtype=float)
        for i in range(DD_WINDOW, len(ds) - 1):
            dd20 = closes[i] / closes[i - DD_WINDOW] - 1
            if dd20 > dd_trig:
                continue
            lo5 = closes[max(0, i - REB_WINDOW):i + 1].min()
            reb = closes[i] / lo5 - 1
            if reb <= reb_trig:
                continue
            bi = idx.get(ds[i]) + 1             # T+1 开盘买
            if bi >= len(dates):
                continue
            out.setdefault(bi, []).append({
                "sid": sid, "sw": name, "trigger": ds[i], "dd20": float(dd20),
                "reb": float(reb), "open_i": bi,
            })
    return out


def run_dip_sleeve(book, signals: Dict[int, List[dict]], start_date: str,
                   cap: float = SLEEVE_CAP) -> dict:
    """抄底 sleeve 的现金账：与生产同一套记账规则（T+1 开盘买、到期收盘卖、双边成本）。"""
    dates = book.dates
    i0 = next((i for i, d in enumerate(dates) if d >= start_date), 0)
    cash, nav, holdings = 1.0, [], []
    nav_path, nav_dates, deployed = [], [], []
    trades: List[dict] = []
    cost = C.ROUND_TRIP_COST
    for k in range(i0, len(dates)):
        # ① 开盘：按昨日账户总值给额度，累计敞口 ≤ cap × NAV
        sigs = signals.get(k) or []
        if sigs and cash > 1e-9:
            v_prev = nav_path[-1] if nav_path else 1.0
            room = max(0.0, cap * v_prev - sum(h["shares"] * (book.close(h["sid"], k - 1) or h["amt"] / h["shares"])
                                               for h in holdings))
            budget = min(room, cash)
            if budget > 1e-6:
                sigs = sigs[:MAX_PER_DAY]
                each = budget / len(sigs)
                for sg in sigs:
                    px = book.open_(sg["sid"], k)
                    if not px or px <= 0:
                        continue
                    amt = min(each, cash)
                    if amt <= 1e-6:
                        continue
                    cash -= amt
                    holdings.append({"sid": sg["sid"], "sw": sg["sw"], "buy_i": k,
                                     "buy_px": px, "shares": amt / px, "amt": amt,
                                     "end": k + HOLD, "trigger": sg["trigger"],
                                     "dd20": sg["dd20"], "reb": sg["reb"]})
        # ② 收盘：到期卖出
        kept = []
        for h in holdings:
            if h["end"] <= k:
                px = book.close(h["sid"], k) or h["buy_px"]
                proceeds = h["shares"] * px - h["amt"] * cost
                cash += proceeds
                trades.append({"sid": h["sid"], "sw": h["sw"], "buy_date": dates[h["buy_i"]],
                               "sell_date": dates[k], "held": k - h["buy_i"],
                               "ret": px / h["buy_px"] - 1 - cost})
                continue
            kept.append(h)
        holdings = kept
        mv = sum(h["shares"] * (book.close(h["sid"], k) or h["buy_px"]) for h in holdings)
        v = cash + mv
        nav_path.append(v)
        nav_dates.append(dates[k])
        deployed.append(mv / v if v > 0 else 0.0)
    rs = [t["ret"] for t in trades]
    st = _stats(nav_path, nav_dates)
    st.update({
        "n_trades": len(trades),
        "win_rate": (sum(1 for r in rs if r > 0) / len(rs)) if rs else None,
        "avg_ret": float(np.mean(rs)) if rs else None,
        "worst": float(min(rs)) if rs else None,
        "avg_deployed": float(np.mean(deployed)) if deployed else 0.0,
        "open_at_end": len(holdings),
    })
    return {"nav": nav_path, "dates": nav_dates, "stats": st, "trades": trades}


def yearly(nav: List[float], dates: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    by: Dict[str, List[int]] = {}
    for i, d in enumerate(dates):
        by.setdefault(d[:4], []).append(i)
    for y, ii in sorted(by.items()):
        if len(ii) < 20:
            continue
        a, b = nav[ii[0]], nav[ii[-1]]
        if a > 0:
            out[y] = b / a - 1
    return out


def main() -> None:
    book = PL.load_etf_book()
    panel = json.load(open(f"{C.CACHE_DIR}/panel.json", encoding="utf-8"))
    sim = json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))
    trades_prod = []
    for d, day in sim["days"].items():
        for sw, sid, w, sc, sell, H, act in day["p"]:
            trades_prod.append({"date": d, "sw_name": sw, "horizon": H, "composite": sc,
                                "sell_date": sell, "etf_sid": sid, "action": act})
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    base = dict(max_holdings=sim["max_holdings"], target_positions=sim["K"],
                risk_parity=True, rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cash_yield=RC["cash_yield"], score_tilt=PF["score_tilt"],
                max_new_per_day=sim["max_new_per_day"])

    print("[基线] 生产策略（本金 100%）...")
    r_base = ST.simulate_cash_account(book, trades_prod, start_date=C.BACKTEST_START, **base)
    nav_base = r_base["nav"]
    dates = r_base["dates"]

    print("[信号] 统计抄底触发点 ...")
    sigs = dip_signals(panel, book)
    n_sig = sum(len(v) for v in sigs.values())
    print(f"       触发日 {len(sigs)} 天 / 信号 {n_sig} 条"
          f"（{min(sigs) if sigs else '-'} ~ {max(sigs) if sigs else '-'} 索引）")

    print("[抄底] 独立 sleeve（本金 100%，敞口上限 20%）...")
    r_dip = run_dip_sleeve(book, sigs, C.BACKTEST_START)
    # 对齐到基线日历
    pos = {d: i for i, d in enumerate(r_dip["dates"])}
    dip_nav: List[float] = []
    for d in dates:
        if d in pos:
            dip_nav.append(r_dip["nav"][pos[d]])
        else:                                  # sleeve 尚未开始 / 该日无账本点 → 沿用上一值
            dip_nav.append(dip_nav[-1] if dip_nav else 1.0)

    comb = [0.8 * b + 0.2 * d for b, d in zip(nav_base, dip_nav)]
    s_b, s_d, s_c = _stats(nav_base, dates), r_dip["stats"], _stats(comb, dates)

    print(f"\n{'口径':<22}{'年化':>8}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}{'累计':>9}")
    for name, s in [("基线 100%", s_b), ("抄底 sleeve（20%额度）", s_d), ("组合 80/20", s_c)]:
        print(f"{name:<22}{s['cagr']:>8.2%}{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}"
              f"{s['calmar']:>8.3f}{s['total']:>9.1%}")

    print(f"\n抄底 sleeve 明细：成交 {s_d['n_trades']} 笔 · 胜率 "
          f"{s_d['win_rate']*100:.1f}% · 单笔均值 {s_d['avg_ret']*100:+.2f}% · "
          f"最差 {s_d['worst']*100:+.2f}% · 平均敞口 {s_d['avg_deployed']*100:.1f}%")
    print(f"增量（组合 − 基线）：年化 {s_c['cagr']-s_b['cagr']:+.2%} · "
          f"回撤 {s_c['maxdd']-s_b['maxdd']:+.2%} · Calmar {s_c['calmar']-s_b['calmar']:+.3f}")

    yb, yc = yearly(nav_base, dates), yearly(comb, dates)
    print(f"\n{'年份':<8}{'基线':>9}{'组合':>9}{'增量':>9}")
    for y in sorted(set(yb) & set(yc)):
        print(f"{y:<8}{yb[y]:>9.2%}{yc[y]:>9.2%}{yc[y]-yb[y]:>+9.2%}")




def _dip_trades(sigs: Dict[int, List[dict]], book, score: float = 52.0) -> List[dict]:
    """把抄底信号转成与生产同构的候选（同池竞争用）。"""
    out = []
    for bi, lst in sigs.items():
        d = book.dates[bi - 1]                  # 信号日 = 买入日的前一交易日
        if C.BACKTEST_START and d < C.BACKTEST_START:
            continue                            # 回测窗口之前的不算（否则独立起账的起点会被拉回 1999）
        for sg in lst:
            out.append({"date": d, "sw_name": sg["sw"], "horizon": HOLD,
                        "composite": score, "sell_date": None,
                        "etf_sid": sg["sid"], "action": "DIP"})
    return out


def main() -> None:
    book = PL.load_etf_book()
    panel = json.load(open(f"{C.CACHE_DIR}/panel.json", encoding="utf-8"))
    sim = json.load(open(f"{C.OUT_DIR}/sim.json", encoding="utf-8"))
    prod = []
    for d, day in sim["days"].items():
        for sw, sid, w, sc, sell, H, act in day["p"]:
            prod.append({"date": d, "sw_name": sw, "horizon": H, "composite": sc,
                         "sell_date": sell, "etf_sid": sid, "action": act})
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    base = dict(max_holdings=sim["max_holdings"], target_positions=sim["K"],
                risk_parity=True, rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cash_yield=RC["cash_yield"], score_tilt=PF["score_tilt"],
                max_new_per_day=sim["max_new_per_day"])

    def run(tr):
        r = ST.simulate_cash_account(book, tr, start_date=C.BACKTEST_START, **base)
        return r, r["stats"]

    r0, s0 = run(prod)
    sigs = dip_signals(panel, book)
    dip = _dip_trades(sigs, book)
    print(f"生产候选 {len(prod)} 条 · 抄底信号 {len(dip)} 条（{len(sigs)} 个触发日）\n")

    # ① 独立 sleeve 口径（资金预留）
    print("[口径 1] 独立 sleeve：80% 基线 + 20% 抄底专用额度（不做再平衡）")
    r_dip = run_dip_sleeve(book, sigs, C.BACKTEST_START)
    pos = {d: i for i, d in enumerate(r_dip["dates"])}
    dip_nav: List[float] = []
    for d in r0["dates"]:
        dip_nav.append(r_dip["nav"][pos[d]] if d in pos else (dip_nav[-1] if dip_nav else 1.0))
    comb = [0.8 * b + 0.2 * dd for b, dd in zip(r0["nav"], dip_nav)]
    s_c = _stats(comb, r0["dates"])
    sd = r_dip["stats"]
    print(f"   基线 100%      年化 {s0['cagr']:>7.2%}  回撤 {s0['maxdd']:>8.2%}  Calmar {s0['calmar']:.3f}")
    print(f"   抄底 sleeve    年化 {sd['cagr']:>7.2%}  回撤 {sd['maxdd']:>8.2%}  Calmar {sd['calmar']:.3f}"
          f"  成交 {sd['n_trades']} 笔 胜率 {sd['win_rate']*100:.1f}%  平均敞口 {sd['avg_deployed']*100:.1f}%")
    print(f"   组合 80/20     年化 {s_c['cagr']:>7.2%}  回撤 {s_c['maxdd']:>8.2%}  Calmar {s_c['calmar']:.3f}"
          f"   → 增量 年化 {s_c['cagr']-s0['cagr']:+.2%} 回撤 {s_c['maxdd']-s0['maxdd']:+.2%}")

    # ② 同池竞争口径（不预留资金）
    print("\n[口径 2] 同池竞争：抄底信号并入候选池，与主策略共享 K=24 槽位与现金（不预留额度）")
    r1, s1 = run(prod + dip)
    nb = sum(1 for e in r1["events"] if e["k"] == "B" and e.get("a") == "DIP")
    print(f"   基线           年化 {s0['cagr']:>7.2%}  回撤 {s0['maxdd']:>8.2%}  Sharpe {s0['sharpe']:.3f}"
          f"  Calmar {s0['calmar']:.3f}  均投出 {s0['avg_deployed']:>6.1%}  成交 {s0['executed_trades']}")
    print(f"   同池 + 抄底    年化 {s1['cagr']:>7.2%}  回撤 {s1['maxdd']:>8.2%}  Sharpe {s1['sharpe']:.3f}"
          f"  Calmar {s1['calmar']:.3f}  均投出 {s1['avg_deployed']:>6.1%}  成交 {s1['executed_trades']}"
          f"（抄底建仓 {nb} 笔）")
    print(f"   → 增量 年化 {s1['cagr']-s0['cagr']:+.2%} · 回撤 {s1['maxdd']-s0['maxdd']:+.2%}"
          f" · Calmar {s1['calmar']-s0['calmar']:+.3f} · 均投出 {s1['avg_deployed']-s0['avg_deployed']:+.1%}")

    # ③ 分年度（同池）
    yb, y1 = yearly(r0["nav"], r0["dates"]), yearly(r1["nav"], r1["dates"])
    print(f"\n{'年份':<8}{'基线':>9}{'同池':>9}{'增量':>9}")
    for y in sorted(set(yb) & set(y1)):
        print(f"{y:<8}{yb[y]:>9.2%}{y1[y]:>9.2%}{y1[y]-yb[y]:>+9.2%}")

    # ④ 参数敏感性（同池口径）
    print("\n[敏感性] 同池口径，逐项单变量扫描：")
    print(f"{'参数':<24}{'信号':>6}{'年化':>9}{'回撤':>9}{'Sharpe':>8}{'Calmar':>8}{'均投出':>8}")
    grid = ([("跌幅阈值", dict(dd_trig=t)) for t in (-0.10, -0.12, -0.15, -0.20)] +
            [("回升阈值", dict(reb_trig=t)) for t in (0.01, 0.03, 0.05)] +
            [("候选分(排序)", dict(score=v)) for v in (50.0, 52.0, 55.0, 58.0)])
    for label, kw in grid:
        kw = dict(kw)
        sc_ = kw.pop("score", 52.0)
        sg = dip_signals(panel, book, **kw) if ("dd_trig" in kw or "reb_trig" in kw) else sigs
        dt = _dip_trades(sg, book, score=sc_)
        _, s = run(prod + dt)
        shown = (f"{kw['dd_trig']:+.2f}" if "dd_trig" in kw else
                 f"{kw['reb_trig']:.2f}" if "reb_trig" in kw else f"{sc_:.0f}")
        print(f"{label+f'={shown}':<24}{len(dt):>6}{s['cagr']:>9.2%}"
              f"{s['maxdd']:>9.2%}{s['sharpe']:>8.3f}{s['calmar']:>8.3f}{s['avg_deployed']:>8.1%}")

    # ⑤ 滚动一年（独立起账）——本项目判断「是不是只在某一段好用」的标准检验
    #    注意：rolling_fresh_entry 的起点取「最早一条候选的日期」，两边的候选起点不同
    #    就会比到不同的窗口集（抄底信号比生产候选早），必须先对齐起点再比。
    cut = min(t["date"] for t in prod)
    dip_al = [t for t in dip if t["date"] >= cut]
    print(f"\n[滚动一年·独立起账] 对齐起点 {cut}（dip {len(dip)}→{len(dip_al)} 条）")
    print(f"{'口径':<14}{'n':>6}{'均值':>9}{'中位':>9}{'为正':>8}{'p5':>9}{'最差':>9}{'跑赢基准':>9}")
    for label, tr in [("基线", prod), ("同池+抄底", prod + dip_al)]:
        rf = ST.rolling_fresh_entry(book, tr, horizon=252, **base)
        if rf:
            print(f"{label:<14}{rf['n']:>6}{rf['mean']:>9.2%}{rf['median']:>9.2%}"
                  f"{rf['win_rate']:>8.1%}{rf['p5']:>9.2%}{rf['worst']:>9.2%}"
                  f"{rf.get('beat_rate', 0):>9.1%}")


if __name__ == "__main__":
    main()
