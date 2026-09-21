# -*- coding: utf-8 -*-
"""ETF 结算层：把 ARK 的方向结论换成「真实可执行」的资金曲线。

为什么需要这一层：
    申万行业指数是不可交易的理论指数，用它算出来的收益无法兑现。
    因此这里做严格分工：
        - 行业指数（板块）   -> 只负责「方向判断」与信号胜率
        - 行业 ETF          -> 只负责「收益结算」与资金曲线
    ETF 上市之前的日期，该行业视为不可交易，不参与任何收益统计，
    绝不用指数回填。

交易假设（保守、可复核）：
    信号日 T 收盘后产生结论 -> T+1 开盘买入 -> 持有 H 个交易日 -> T+1+H 收盘卖出
    成本 = 双边佣金 + 双边滑点（ETF 免印花税）
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import etf_map as EM  # noqa: E402


class EtfBook:
    """ETF 行情账簿：按行业名取可交易 ETF，给出真实成交价。"""

    def __init__(self, panel: dict):
        self.dates: List[str] = panel["dates"]
        self.idx = {d: i for i, d in enumerate(self.dates)}
        self.series: Dict[str, dict] = panel["series"]
        self.sw_to_etf: Dict[str, str] = panel["sw_to_etf"]
        # 逐日选择：sw -> {date: secid}，回测必须用这个，避免前视与僵尸 ETF
        self.sw_choice: Dict[str, Dict[str, str]] = panel.get("sw_choice", {})
        self.listing: Dict[str, dict] = panel["listing"]
        self.tradeable: Dict[str, List[str]] = panel.get("tradeable", {})
        # 基准 ETF（用于结算口径的超额比较）
        self.bench_etf = self._pick_bench()

    def _pick_bench(self) -> Optional[str]:
        for sid in ("1.510300", "1.510310", "0.159919"):
            if sid in self.series and self.series[sid]:
                return sid
        return None

    def etf_of(self, sw_name: str, date: Optional[str] = None) -> Optional[str]:
        """取该行业在某日的 ETF。给 date 时用逐日选择（回测），否则用当前主 ETF。"""
        if date is not None:
            ch = self.sw_choice.get(sw_name)
            if ch:
                return ch.get(date)
            return None
        return self.sw_to_etf.get(sw_name)

    def is_tradeable(self, sw_name: str, date: str) -> bool:
        ch = self.sw_choice.get(sw_name)
        if ch:
            return date in ch
        return sw_name in self.tradeable.get(date, [])

    def row(self, secid: str, date: str) -> Optional[dict]:
        return self.series.get(secid, {}).get(date)

    def close(self, secid: str, i: int) -> Optional[float]:
        if i < 0 or i >= len(self.dates):
            return None
        r = self.series.get(secid, {}).get(self.dates[i])
        if not r:
            return None
        v = r.get("close")
        return float(v) if v == v else None

    def open_(self, secid: str, i: int) -> Optional[float]:
        if i < 0 or i >= len(self.dates):
            return None
        r = self.series.get(secid, {}).get(self.dates[i])
        if not r:
            return None
        v = r.get("open") or r.get("close")
        return float(v) if v == v else None

    def listing_date(self, secid: str) -> Optional[str]:
        it = self.listing.get(secid)
        return it["start"] if it else None

    # ---------------------------------------------------------- 单笔结算
    def ma_at(self, secid: str, i: int, window: int = 20) -> Optional[float]:
        """标的自身均线（用于执行结论里写明的失效条件）。"""
        vals = []
        for k in range(max(0, i - window + 1), i + 1):
            c = self.close(secid, k)
            if c:
                vals.append(c)
        if len(vals) < max(5, window // 2):
            return None
        return sum(vals) / len(vals)

    def settle_trade(self, sw_name: str, date: str, H: int,
                     early_exit: bool = False,
                     exit_ma: int = 20) -> Optional[dict]:
        """一次完整交易：T+1 开盘买，T+1+H 收盘卖。不可交易返回 None。

        early_exit=True 时执行结论里已经写明的失效条件之一
        （收盘跌破 MA20 即退出），而不是死扛到建议周期结束。
        这条规则来自 classify 输出的 invalidation，属于事前定义，不是事后拟合。
        """
        i0 = self.idx.get(date)
        if i0 is None:
            return None
        b_i = i0 + 1                       # 买入日
        s_i = i0 + 1 + H                   # 卖出日
        if b_i >= len(self.dates) or s_i >= len(self.dates):
            return None
        # 用「买入日」当时可交易的那只 ETF，并持有到卖出（中途不换 ETF）
        sid = self.etf_of(sw_name, self.dates[b_i])
        if sid is None:
            return None
        buy = self.open_(sid, b_i)
        sell = self.close(sid, s_i)
        if not buy or buy <= 0:
            return None
        if not sell:
            # 卖出日停牌/无数据：向前找最近有成交的日期平仓
            for k in range(s_i, b_i, -1):
                c = self.close(sid, k)
                if c:
                    sell, s_i = c, k
                    break
            if not sell:
                return None

        exited_early = False
        if early_exit:
            # 失效条件（事前写死在结论里）：连续 3 个交易日收盘跌破 MA20 才退出。
            # 单日跌破是噪音——行业 ETF 在 60-100 日的建议周期里几乎必然出现单日跌破，
            # 实测该规则会 100% 触发、平均只持有 7 天，把有效持仓全部洗掉。
            below = 0
            for k in range(b_i + 1, s_i + 1):
                c = self.close(sid, k)
                ma = self.ma_at(sid, k, exit_ma)
                if c and ma and c < ma:
                    below += 1
                else:
                    below = 0
                if below >= 3:
                    nxt = self.open_(sid, min(k + 1, len(self.dates) - 1))
                    sell = nxt or c
                    s_i = min(k + 1, len(self.dates) - 1)
                    exited_early = True
                    break

        gross = sell / buy - 1.0
        net = gross - C.ROUND_TRIP_COST
        # 持仓期最大回撤（按收盘价序列）
        seg = []
        for k in range(b_i, s_i + 1):
            c = self.close(sid, k)
            if c:
                seg.append(c)
        dd = (min(seg) / buy - 1.0) if seg else None

        # 同期基准 ETF 收益（结算口径的超额）
        ex_bench = None
        if self.bench_etf:
            bb = self.open_(self.bench_etf, b_i)
            bs = self.close(self.bench_etf, s_i)
            if bb and bs and bb > 0:
                ex_bench = net - (bs / bb - 1.0 - C.ROUND_TRIP_COST)

        # 退出原因：写进回执，供曲线上的卖出点标注（前端悬浮展示）
        if exited_early:
            reason = f"失效条件触发：连续 3 日收盘跌破 MA{exit_ma}，次日开盘平仓"
        elif s_i != i0 + 1 + H:
            reason = "到期日无行情，顺延至最近有成交日收盘"
        else:
            reason = f"持有到期（建议周期 {int(H)} 个交易日）"

        return {
            "etf": sid,
            "etf_name": self.listing.get(sid, {}).get("name", sid),
            "buy_date": self.dates[b_i], "sell_date": self.dates[s_i],
            "buy_price": round(buy, 4), "sell_price": round(sell, 4),
            "gross_ret": round(gross, 5),
            "cost": round(C.ROUND_TRIP_COST, 5),
            "net_ret": round(net, 5),
            "maxdd": round(dd, 5) if dd is not None else None,
            "ex_bench": round(ex_bench, 5) if ex_bench is not None else None,
            "held_days": s_i - b_i,
            "exited_early": exited_early,
            "reason": reason,
        }


# ------------------------------------------------------------------ 绩效指标
def perf_stats(nav: np.ndarray, dates: List[str],
               bench: Optional[np.ndarray] = None) -> dict:
    """从净值序列算年化 / 回撤 / Sharpe / Calmar 等。"""
    if nav.size < 5:
        return {}
    total = float(nav[-1] - 1.0)
    years = len(nav) / 252.0
    cagr = float(nav[-1] ** (1.0 / years) - 1.0) if years > 0 and nav[-1] > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1.0
    maxdd = float(dd.min())
    mdd_i = int(dd.argmin())
    # 最长水下天数
    under = 0
    cur = 0
    for v in dd:
        if v < -1e-9:
            cur += 1
            under = max(under, cur)
        else:
            cur = 0
    rets = np.diff(nav) / nav[:-1]
    vol = float(np.std(rets) * np.sqrt(252)) if rets.size > 2 else 0.0
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-9 else None
    downside = rets[rets < 0]
    dvol = float(np.std(downside) * np.sqrt(252)) if downside.size > 2 else 0.0
    sortino = float(rets.mean() * 252 / dvol) if dvol > 1e-9 else None
    calmar = float(cagr / abs(maxdd)) if maxdd < -1e-9 else None
    # 月度
    monthly = defaultdict(list)
    for k, d in enumerate(dates[1:], start=1):
        monthly[d[:7]].append(nav[k] / nav[k - 1] - 1.0)
    mret = {m: float(np.prod([1 + x for x in v]) - 1) for m, v in monthly.items()}
    mpos = float(np.mean([1 if v > 0 else 0 for v in mret.values()])) if mret else None

    out = {
        "total_return": round(total, 4),
        "cagr": round(cagr, 4),
        "maxdd": round(maxdd, 4),
        "maxdd_date": dates[mdd_i] if mdd_i < len(dates) else None,
        "max_underwater_days": int(under),
        "vol": round(vol, 4),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "monthly_win_rate": round(mpos, 3) if mpos is not None else None,
        "months": len(mret),
        "days": int(nav.size),
        "years": round(years, 2),
    }
    if bench is not None and bench.size == nav.size:
        btotal = float(bench[-1] - 1.0)
        bcagr = float(bench[-1] ** (1.0 / years) - 1.0) if years > 0 else 0.0
        bpeak = np.maximum.accumulate(bench)
        bmaxdd = float((bench / bpeak - 1.0).min())
        out.update({
            "bench_total": round(btotal, 4),
            "bench_cagr": round(bcagr, 4),
            "bench_maxdd": round(bmaxdd, 4),
            "excess_cagr": round(cagr - bcagr, 4),
        })
    return out


def maxdd_of(nav: np.ndarray) -> float:
    if nav.size == 0:
        return 0.0
    peak = np.maximum.accumulate(nav)
    return float((nav / peak - 1.0).min())


def holding_stats(dates: List[str], events: List[dict]) -> dict:
    """从 B/S 成交事件统计「平均持仓时间」——用的是与曲线同一批真实成交。

    注意：事件的 i 是净值序列下标，必须用 nav_dates 映射买入日，
    不能用 book.dates（两者差一个对齐首点）。
    """
    di = {d: i for i, d in enumerate(dates)}
    sells = [e for e in events if e.get("k") == "S"]
    held: List[int] = []
    by_action: Dict[str, List[int]] = defaultdict(list)
    by_horizon: Dict[str, List[int]] = defaultdict(list)
    by_year: Dict[str, List[int]] = defaultdict(list)
    for e in sells:
        bd = e.get("bd")
        if bd not in di or e.get("i") is None:
            continue
        h = int(e["i"] - di[bd])
        held.append(h)
        if e.get("a"):
            by_action[e["a"]].append(h)
        if e.get("h"):
            by_horizon[str(int(e["h"]))].append(h)
        if bd:
            by_year[bd[:4]].append(h)

    def _agg(v: List[int]) -> Optional[dict]:
        if not v:
            return None
        a = np.array(v, dtype=float)
        return {"n": len(v), "avg": round(float(a.mean()), 1),
                "median": round(float(np.median(a)), 1),
                "min": int(a.min()), "max": int(a.max())}

    # 持有天数分布：按「约 1 个月 / 2 个月 / 3 个月 / 4 个月以上」分桶
    buckets = {"≤21日(1个月内)": 0, "22-42日": 0, "43-63日": 0,
               "64-84日": 0, "85日以上": 0}
    for h in held:
        if h <= 21:
            buckets["≤21日(1个月内)"] += 1
        elif h <= 42:
            buckets["22-42日"] += 1
        elif h <= 63:
            buckets["43-63日"] += 1
        elif h <= 84:
            buckets["64-84日"] += 1
        else:
            buckets["85日以上"] += 1

    overall = _agg(held) or {}
    return {
        "n": len(held),
        "avg_days": overall.get("avg"),
        "median_days": overall.get("median"),
        "min_days": overall.get("min"),
        "max_days": overall.get("max"),
        "avg_months": round(overall["avg"] / 21.0, 1) if overall.get("avg") else None,
        "dist": buckets,
        "by_action": {k: _agg(v) for k, v in by_action.items()},
        "by_horizon": {k: _agg(v) for k, v in sorted(by_horizon.items(), key=lambda x: int(x[0]))},
        "by_year": {k: _agg(v) for k, v in sorted(by_year.items())},
    }


# ------------------------------------------------------------------ 组合模拟
def bench_ma_gate(book: EtfBook, window: int = 200) -> Optional[np.ndarray]:
    """市场趋势门控：基准 ETF 收盘价在自身 MA(window) 之上的交易日才允许建仓。

    这不是「择时」策略，而是复盘得出的风控约束——原版组合在 2021-09 见顶后
    回撤 37.8%、水下 815 个交易日，根因是在市场整体下行趋势里持续满仓。
    """
    sid = book.bench_etf
    if not sid:
        return None
    px = np.array([book.close(sid, i) or np.nan for i in range(len(book.dates))],
                  dtype=float)
    ok = ~np.isnan(px)
    if ok.sum() < window + 10:
        return None
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(len(px)), idxs, px[idxs])
    ma = np.convolve(px, np.ones(window) / window, mode="full")[:len(px)]
    return px > ma


def _sigma(book: "EtfBook", sid: str, k: int, window: int = 60) -> float:
    """标的截至第 k-1 日的年化波动（只用已实现数据，无前视）。"""
    vals = [book.close(sid, j) for j in range(max(0, k - window), k)]
    vals = [v for v in vals if v]
    if len(vals) < 11:
        return 0.25
    rr = np.diff(vals) / np.array(vals[:-1])
    sig = float(np.std(rr) * np.sqrt(252))
    return sig if sig > 1e-4 else 0.25


def simulate_etf_portfolio(book: EtfBook, trades: List[dict],
                           start_date: Optional[str] = None,
                           max_holdings: int = 999,
                           market_gate: bool = False,
                           gate_window: int = 200,
                           risk_parity: bool = False,
                           rp_window: int = 60,
                           vol_target: Optional[float] = None,
                           vol_window: int = 60,
                           vol_floor: float = 0.80,
                           cash_yield: float = 0.015,
                           score_tilt: float = 0.0,
                           max_new_per_day: Optional[int] = None,
                           max_per_sid: Optional[int] = None) -> dict:
    """把全部可交易结论按「等权 + 各自持有期」滚成资金曲线。

    trades: [{date, sw_name, horizon, ...}]
    规则：
      - 每个交易日，当日新开仓的结论与已有持仓**等权**配置（满仓、无择时加减仓）
      - 持仓数量上限 max_holdings（超出时按综合分取前 N，模拟真实资金约束）
      - 只在可交易行业上建仓；不可交易的结论直接跳过（不进入曲线）
      - market_gate=True 时，只在基准 ETF 位于其 MA200 上方时才新建仓

    风控增强（默认关闭，关闭时与历史口径完全一致）：
      - risk_parity=True：持仓间按 1/σ 分配权重（风险预算等价），而非等权。
        高波动行业不再和低波动行业占同样的资金，组合特质波动下降。
      - vol_target=X：组合已实现波动（过去 vol_window 日）高于 X 时按比例降仓，
        最低不低于 vol_floor；未投入部分按 cash_yield 计现金收益。
        依据是波动聚集性——高波动期往往也是回撤累积期。
      这两条只改变**权重与总暴露**，不改变任何一笔买卖的时点与标的，
      因此方向判断胜率、成交笔数等结论层指标不受影响。

    配置增强（默认关闭）：
      - score_tilt>0：持仓权重再乘 (综合分/50)^score_tilt，让高分结论占更大权重，
        替代「所有结论一视同仁」的等权。不改变成交集合与时点。
      - max_new_per_day=N：每日最多新建 N 个仓位（容量上限 max_holdings 不变），
        避免在信号密集日把资金均摊到低分结论上。
      - max_per_sid=N：同一只 ETF 同时最多占 N 个槽位。两层架构下多条细分赛道结论
        会共用同一只一级行业 ETF 作代理，若不限制，就等于在这只 ETF 上反复加码。
        实验台（lab_opt6）结论：这条**不是** Pareto 改进——它确实能压回撤
        （同 ETF≤2 时 -29.91% → -26.46%），但年化要付 2.6pp 的代价（12.57% → 9.97%）。
        因此不进生产配置，只在复盘页作为「再降回撤的价码」显式列出，让取舍可见。
    """
    dates = book.dates
    if start_date:
        s_i = book.idx.get(start_date)
        if s_i is None:
            s_i = 0
    else:
        s_i = 0

    gate = bench_ma_gate(book, gate_window) if market_gate else None
    blocked_by_gate = 0

    # 建仓计划：按日期归集
    plan = defaultdict(list)
    skipped_untradable = 0
    for t in trades:
        d = t["date"]
        i0 = book.idx.get(d)
        if i0 is None or i0 < s_i:
            continue
        H = int(t["horizon"])
        b_i = i0 + 1
        # 若单笔结算给出了实际卖出日（如触发失效条件提前退出），以它为准，
        # 否则按建议周期到期卖出。组合曲线必须复用单笔的真实持有区间。
        if t.get("sell_date"):
            s_i_ = book.idx.get(t["sell_date"], i0 + 1 + H)
        else:
            s_i_ = i0 + 1 + H
        if b_i >= len(dates) or s_i_ >= len(dates) or s_i_ <= b_i:
            continue
        sid0 = t.get("etf_sid") or book.etf_of(t["sw_name"], dates[b_i])
        if sid0 is None:
            skipped_untradable += 1
            continue
        if gate is not None and not gate[b_i]:
            blocked_by_gate += 1
            continue
        plan[b_i].append({"sw": t["sw_name"], "end": s_i_, "secid": sid0,
                          "score": t.get("composite", 0),
                          "horizon": H, "action": t.get("action"),
                          "reason": t.get("reason")})

    holdings: List[dict] = []      # 活跃持仓 [{sw, end, secid, last_close}]
    nav, nav_dates = [1.0], [dates[max(s_i, 1) - 1] if s_i > 0 else dates[0]]
    bench_nav = [1.0]
    cur = 1.0
    bcur = 1.0
    bench_sid = book.bench_etf
    prev_close_cache: Dict[str, float] = {}
    # 成交事件：曲线上的 B/S 点，逐笔可追溯（前端悬浮展示）
    events: List[dict] = []
    n_executed = 0
    blocked_by_capacity = 0
    blocked_by_sid_cap = 0
    rf = cash_yield / 252.0
    ret_hist: List[float] = []
    exposure_log: List[float] = []
    hold_count: List[int] = []

    def _bench_ret(from_i: int, to_i: int) -> Optional[float]:
        """同期基准 ETF 的净收益（同一成本口径），用于单笔超额。"""
        if not bench_sid:
            return None
        bb = book.open_(bench_sid, from_i)
        bs = book.close(bench_sid, to_i)
        if not bb or not bs or bb <= 0:
            return None
        return bs / bb - 1.0 - C.ROUND_TRIP_COST

    start_k = max(s_i, 1)
    for k in range(start_k, len(dates)):
        d = dates[k]
        day_events: List[dict] = []
        # 1) 先把到期持仓按「昨日收盘 -> 今日收盘」的收益计入（今日收盘卖出）
        day_rets = []          # [(日收益, 权重)]
        new_holdings = []
        for h in holdings:
            c = book.close(h["secid"], k)
            p = prev_close_cache.get(h["sid"])
            if risk_parity:
                h["sigma"] = _sigma(book, h["secid"], k, rp_window)
                h["w"] = 1.0 / max(h["sigma"], 0.05)
            else:
                h["w"] = 1.0
            # 分数加权：高分结论占更大权重（tilt=0 时与等权完全一致）
            if score_tilt:
                h["w"] *= max(float(h.get("score") or 50.0), 1.0) ** score_tilt / (50.0 ** score_tilt)
            if c and p:
                day_rets.append((c / p - 1.0, h["w"]))
            if h["end"] > k:          # 未到期，继续持有
                h2 = dict(h)
                new_holdings.append(h2)
            else:
                # 到期：在今日收盘卖出。单笔盈亏以组合实际成交价重算，
                # 保证悬浮里看到的收益与曲线用的是同一笔钱。
                bpx = h.get("buy_price")
                gross = (c / bpx - 1.0) if (c and bpx) else None
                net = (gross - C.ROUND_TRIP_COST) if gross is not None else None
                bre = _bench_ret(h.get("buy_i", k), k)
                day_events.append({
                    "k": "S", "sw": h["sw"], "sid": h["secid"],
                    "price": round(c, 4) if c else None,
                    "bd": dates[h["buy_i"]] if h.get("buy_i") is not None else None,
                    "bpx": round(bpx, 4) if bpx else None,
                    "h": h.get("horizon"), "a": h.get("action"),
                    "why": h.get("reason"),
                    "ret": round(net, 5) if net is not None else None,
                    "ex": round(net - bre, 5) if (net is not None and bre is not None) else None,
                })
        holdings = new_holdings

        # 2) 新开仓：今日开盘买入，当日收益 = 收盘/开盘
        if k in plan:
            adds = sorted(plan[k], key=lambda x: -x.get("score", 0))
            if max_new_per_day is not None:
                adds = adds[:max_new_per_day]
            room = max(0, max_holdings - len(holdings))
            blocked_by_capacity += max(0, len(adds) - room)
            for item in adds[:room]:
                sid = item.get("secid") or book.etf_of(item["sw"], dates[k])
                if not sid:
                    continue
                # 同一只 ETF 的槽位上限（None = 不限）。只挡「再买同一只」，
                # 不改动任何已有持仓的到期日与卖出价。
                if max_per_sid is not None:
                    if sum(1 for x in holdings if x["sid"] == sid) >= max_per_sid:
                        blocked_by_sid_cap += 1
                        continue
                o = book.open_(sid, k)
                if not o:
                    continue
                sig = _sigma(book, sid, k, rp_window) if risk_parity else 0.25
                w = (1.0 / max(sig, 0.05)) if risk_parity else 1.0
                if score_tilt:
                    w *= max(float(item.get("score") or 50.0), 1.0) ** score_tilt / (50.0 ** score_tilt)
                holdings.append({"sw": item["sw"], "sid": sid, "secid": sid,
                                 "end": item["end"], "buy_i": k, "buy_price": o,
                                 "horizon": item.get("horizon"),
                                 "action": item.get("action"),
                                 "reason": item.get("reason"),
                                 "score": item.get("score"),
                                 "sigma": sig, "w": w})
                n_executed += 1
                day_events.append({
                    "k": "B", "sw": item["sw"], "sid": sid,
                    "price": round(o, 4), "h": item.get("horizon"),
                    "a": item.get("action"),
                    "sc": round(float(item.get("score") or 0), 1),
                })
                c = book.close(sid, k)
                if c:
                    day_rets.append((c / o - 1.0, holdings[-1]["w"]))

        # 3) 更新 prev_close
        prev_close_cache = {}
        for h in holdings:
            c = book.close(h["secid"], k)
            if c:
                prev_close_cache[h["sid"]] = c

        if day_rets:
            w_sum = sum(w for _, w in day_rets)
            r_raw = float(sum(x * w for x, w in day_rets) / w_sum) if w_sum > 0 else 0.0
        else:
            r_raw = 0.0

        # 4) 波动目标：高波动期按比例降仓，剩余部分计现金收益（无前视）
        expo = 1.0
        if vol_target is not None and len(ret_hist) >= 20:
            sig = float(np.std(ret_hist[-vol_window:]) * np.sqrt(252))
            if sig > 1e-6:
                expo = float(np.clip(vol_target / sig, vol_floor, 1.0))
        r = expo * r_raw + (1.0 - expo) * rf
        cur *= (1.0 + r)
        ret_hist.append(r)
        nav.append(cur)
        nav_dates.append(d)
        hold_count.append(len(holdings))
        exposure_log.append(expo)

        # 事件锚定到当日净值点（前端按此索引在曲线上定位）
        if day_events:
            idx = len(nav) - 1
            for ev in day_events:
                ev["i"] = idx
            events.extend(day_events)

        # 基准
        if bench_sid:
            bprev = book.close(bench_sid, k - 1)
            bnow = book.close(bench_sid, k)
            if bprev and bnow:
                bcur *= (bnow / bprev)
        bench_nav.append(bcur)

    nav_arr = np.array(nav)
    bench_arr = np.array(bench_nav)
    stats = perf_stats(nav_arr, nav_dates, bench_arr if bench_sid else None)
    # 运行期口径：ETF 覆盖不足的年份组合空仓，会稀释年化，单列一条如实展示
    first_i = next((i for i, x in enumerate(hold_count) if x > 0), 0) + 1
    if 0 < first_i < len(nav) - 100:
        eff = perf_stats(nav_arr[first_i:], nav_dates[first_i:],
                         bench_arr[first_i:] if bench_sid else None)
        eff["start"] = nav_dates[first_i]
        stats["effective"] = eff
    stats["skipped_untradable"] = skipped_untradable
    stats["executed_trades"] = n_executed
    stats["planned_trades"] = sum(len(v) for v in plan.values())
    stats["blocked_by_capacity"] = blocked_by_capacity
    stats["blocked_by_sid_cap"] = blocked_by_sid_cap
    stats["max_per_sid"] = max_per_sid
    stats["blocked_by_gate"] = blocked_by_gate
    stats["market_gate"] = bool(market_gate)
    stats["max_holdings"] = max_holdings
    stats["risk_parity"] = bool(risk_parity)
    stats["vol_target"] = vol_target
    stats["vol_floor"] = vol_floor if vol_target is not None else None
    stats["avg_exposure"] = round(float(np.mean(exposure_log)), 3) if exposure_log else None
    stats["avg_holdings"] = round(float(np.mean(hold_count)), 2) if hold_count else None
    # 实际投入比例 = 持仓数/容量 × 波动目标暴露系数（前端辅助轴直接用这一列）
    inv = [min(1.0, h / max_holdings) * e for h, e in zip(hold_count, exposure_log)]
    stats["avg_invested"] = round(float(np.mean(inv)), 3) if inv else None
    stats["max_holdings_used"] = int(max(hold_count)) if hold_count else 0
    # 成交事件里出现的 ETF 名称表（事件只存 secid，前端查名）
    used = {ev["sid"] for ev in events}
    etf_names = {sid: book.listing.get(sid, {}).get("name", sid) for sid in used}
    return {
        "dates": nav_dates,
        "nav": [round(x, 5) for x in nav],
        "bench": [round(x, 5) for x in bench_nav],
        "stats": stats,
        "events": events,
        "etf_names": etf_names,
        # 逐日仓位序列，与 dates/nav 严格等长（首点是起始基准日，尚无持仓，前置补 0）
        #   holdings = 当日实际持仓只数；exposure = 波动目标的暴露系数；
        #   invested = 实际投入比例。三者都来自真实模拟，不是事后估算。
        "holdings": [0] + hold_count,
        "exposure": [1.0] + [round(x, 4) for x in exposure_log],
        "invested": [0.0] + [round(x, 4) for x in inv],
    }


# -------------------------------------------------------------- 当前持仓与「今日出手」
def open_positions(book: EtfBook, signals: List[dict], as_of: str,
                   max_holdings: int = 32,
                   exit_ma: int = 20, exit_confirm: int = 3,
                   soon_days: int = 5) -> dict:
    """重建「截至 as_of 当日仍在场」的持仓，并给出今日需要出手的标的。

    signals: 全部历史结论（含尚未走完建议周期的），字段见下。与
    simulate_etf_portfolio 用同一套建仓规则（T+1 开盘买、容量上限、
    按综合分排序取前 N），因此这里列出的持仓就是那条资金曲线上真实存在的仓位，
    不是另算一套。唯一的差别是：本函数允许持有期尚未走完的结论进入，
    因为「今天该卖什么」看的是当下持仓，而不是已经结算完的历史。

    出手规则分两类，口径严格分开：
      · 到期出手（主口径）：持有到建议周期结束，到期日收盘卖出；
      · 失效预警（对照口径 B）：连续 exit_confirm 日收盘跌破 MA{exit_ma}，
        结论里写明的失效条件之一。主口径不执行它，只作风险提示。
    """
    dates = book.dates
    empty = {"as_of": as_of, "n_open": 0, "n_sell_today": 0, "n_warn": 0,
             "open": [], "sell_today": [], "sell_soon": [], "warn": [],
             "avg_held": None, "max_holdings": max_holdings}
    as_i = book.idx.get(as_of)
    if as_i is None:
        return empty

    # 1) 建仓计划（只保留买入日 <= as_of 的）
    plan: Dict[int, List[dict]] = defaultdict(list)
    for s in signals:
        i0 = book.idx.get(s.get("date") or "")
        if i0 is None:
            continue
        b_i = i0 + 1
        if b_i > as_i:
            continue
        H = int(s.get("horizon") or 0)
        if H <= 0:
            continue
        # 到期日不截断到数据末端：截断会把「周期还没走完」的持仓误判成「今日到期」。
        # 超出可用数据的部分只影响 end_date 的日历展示，不影响属不属于在场持仓。
        e_i = b_i + H
        sid = s.get("etf_sid") or book.etf_of(s["sw_name"], dates[b_i])
        if not sid:
            continue
        plan[b_i].append({
            "sw": s["sw_name"], "sid": sid, "buy_i": b_i, "end": e_i,
            "horizon": H, "action": s.get("action"),
            "score": float(s.get("composite") or 0),
            "buy_price": book.open_(sid, b_i),
        })
    if not plan:
        return empty

    # 2) 逐日重放（含容量约束），把到期仓位在到期日剔除
    holdings: List[dict] = []
    sell_today: List[dict] = []
    for k in range(min(plan), as_i + 1):
        kept = []
        for h in holdings:
            if h["end"] <= k:
                if k == as_i:
                    sell_today.append(h)
            else:
                kept.append(h)
        holdings = kept
        for it in sorted(plan.get(k, []), key=lambda x: -x["score"]):
            if len(holdings) >= max_holdings:
                break
            if not it.get("buy_price"):
                continue
            holdings.append(it)

    # 3) 组装回执
    def brief(h: dict, sold_today: bool) -> dict:
        sid = h["sid"]
        px = book.close(sid, as_i)
        bpx = h.get("buy_price")
        gross = (px / bpx - 1.0) if (px and bpx) else None
        net = (gross - C.ROUND_TRIP_COST) if gross is not None else None
        # 失效条件：连续 exit_confirm 日收盘跌破 MA
        below = 0
        for k in range(as_i, max(h["buy_i"], as_i - exit_confirm * 3) - 1, -1):
            c = book.close(sid, k)
            ma = book.ma_at(sid, k, exit_ma)
            if c and ma and c < ma:
                below += 1
            else:
                break
        beyond = h["end"] >= len(dates)
        # 「还剩几个交易日才到期」是一个纯计数（end - as_i），不需要未来价格就能算准，
        # 因此即便到期日历日超出数据末端，也应当如实给出。
        # 只有「到期日是几月几号」（end_date）才依赖日历，超出数据时留空。
        # 早先此处把 beyond 的 due_in 一并置空，后果是「N 个交易日内到期」一节
        # 永远为空——因为 as_of 就是数据最后一天时，仍有剩余周期的持仓必然 beyond。
        due_in = max(0, h["end"] - as_i)
        return {
            "sw": h["sw"], "level": None,
            "etf": book.listing.get(sid, {}).get("name", sid),
            "etf_code": sid.split(".")[-1],
            "sid": sid,
            "action": h.get("action"), "horizon": h["horizon"],
            "buy_date": dates[h["buy_i"]],
            "buy_price": round(bpx, 4) if bpx else None,
            "price": round(px, 4) if px else None,
            "ret": round(net, 5) if net is not None else None,
            "held": as_i - h["buy_i"],
            "end_date": None if beyond else dates[h["end"]],
            "due_in": due_in,
            "beyond_data": beyond,
            "score": round(h["score"], 1),
            "invalid": below >= exit_confirm,
            "invalid_note": (f"连续 {below} 日收盘跌破 MA{exit_ma}" if below >= exit_confirm else None),
            "exit_hint": ("今日收盘到期卖出" if sold_today
                          else (f"建议周期还剩约 {h['end'] - as_i} 个交易日（数据尚未走完）"
                                if beyond else
                                ("已超期，按最近收盘价处理" if h["end"] < as_i
                                 else f"{h['end'] - as_i} 个交易日后到期"))),
        }

    open_list = [brief(h, False) for h in holdings]
    open_list.sort(key=lambda x: (x["due_in"] if x["due_in"] is not None else 10 ** 6,
                                 -x["score"]))
    today_list = [brief(h, True) for h in sell_today]
    today_list.sort(key=lambda x: -x["score"])
    soon = [x for x in open_list
            if x["due_in"] is not None and 0 < x["due_in"] <= soon_days]
    warn = [x for x in open_list if x["invalid"]]
    held_days = [x["held"] for x in open_list]
    unused = [x for x in open_list if x["price"] is None]
    return {
        "as_of": as_of,
        "n_open": len(open_list),
        "n_sell_today": len(today_list),
        "n_warn": len(warn),
        "n_unpriced": len(unused),
        "open": open_list,
        "sell_today": today_list,
        "sell_soon": soon,
        "warn": warn,
        "avg_held": round(float(np.mean(held_days)), 1) if held_days else None,
        "median_held": round(float(np.median(held_days)), 1) if held_days else None,
        "max_held": int(max(held_days)) if held_days else None,
        "soon_days": soon_days,
        "max_holdings": max_holdings,
        "rule": ("建仓：信号日 T 收盘出结论 → T+1 开盘买入；"
                 "出手：持有到建议周期结束的当日收盘卖出（主口径）。"
                 f"失效预警另按结论写明的条件（连续 {exit_confirm} 日收盘跌破 "
                 f"MA{exit_ma}）单独标注，主口径不执行该提前退出。"),
    }


# ------------------------------------------------------------------ 牛熊分段
def market_phases(book: EtfBook, bench_sid: Optional[str] = None,
                  bear_dd: float = -0.20) -> List[dict]:
    """按基准 ETF 的历史高点回撤客观划分牛熊段（不靠人工贴标签）。"""
    sid = bench_sid or book.bench_etf
    if not sid:
        return []
    dates = book.dates
    px = []
    for d in dates:
        r = book.row(sid, d)
        px.append(r["close"] if r else np.nan)
    arr = np.array(px, dtype=float)
    ok = ~np.isnan(arr)
    if ok.sum() < 100:
        return []
    # 用前向填充处理缺失
    idxs = np.where(ok)[0]
    arr = np.interp(np.arange(len(arr)), idxs, arr[idxs])
    peak = np.maximum.accumulate(arr)
    dd = arr / peak - 1.0

    phases = []
    cur_phase = "BULL" if dd[0] > bear_dd else "BEAR"
    start = 0
    for i in range(1, len(dd)):
        ph = "BEAR" if dd[i] <= bear_dd else "BULL"
        if ph != cur_phase:
            phases.append({"phase": cur_phase, "start": dates[start],
                           "end": dates[i - 1], "days": i - start})
            cur_phase, start = ph, i
    phases.append({"phase": cur_phase, "start": dates[start],
                   "end": dates[-1], "days": len(dd) - start})
    # 合并过短的段（< 40 交易日）到相邻段
    merged = []
    for p in phases:
        if merged and p["days"] < 40:
            merged[-1]["end"] = p["end"]
            merged[-1]["days"] += p["days"]
        else:
            merged.append(dict(p))
    return merged


def stats_by_phase(trades: List[dict], phases: List[dict]) -> List[dict]:
    """按牛熊段统计结论表现。"""
    out = []
    for p in phases:
        sub = [t for t in trades
               if p["start"] <= t.get("date", "") <= p["end"]
               and t.get("net_ret") is not None]
        if not sub:
            out.append({"phase": p["phase"], "start": p["start"], "end": p["end"],
                        "n": 0})
            continue
        a = np.array([t["net_ret"] for t in sub])
        e = np.array([t["ex_bench"] for t in sub if t.get("ex_bench") is not None])
        out.append({
            "phase": p["phase"], "start": p["start"], "end": p["end"],
            "n": len(sub),
            "win_rate": round(float((a > 0).mean()), 3),
            "avg_ret": round(float(a.mean()), 4),
            "avg_excess": round(float(e.mean()), 4) if e.size else None,
            "avg_maxdd": round(float(np.mean(
                [t["maxdd"] for t in sub if t.get("maxdd") is not None] or [0])), 4),
        })
    return out


def load_etf_panel(path: Optional[str] = None) -> dict:
    path = path or f"{C.CACHE_DIR}/etf_panel.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
