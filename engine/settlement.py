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

    # 滚动一年分布：每个交易日作为假想入市日、持有 252 个交易日的收益分布。
    # 回答「任意时点进场持有一年会怎样」——比单一 CAGR 更接近真实体验。
    roll = None
    if nav.size > 252 + 10:
        rr = nav[252:] / nav[:-252] - 1.0
        roll = {
            "n": int(rr.size),
            "mean": round(float(rr.mean()), 4),
            "median": round(float(np.median(rr)), 4),
            "win_rate": round(float((rr > 0).mean()), 3),
            "p5": round(float(np.percentile(rr, 5)), 4),
            "p95": round(float(np.percentile(rr, 95)), 4),
            "worst": round(float(rr.min()), 4),
            "best": round(float(rr.max()), 4),
        }

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
        "rolling_1y": roll,
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


def build_exposure_cap(book: EtfBook, cfg: Optional[dict] = None,
                       regime: Optional[dict] = None) -> Optional[np.ndarray]:
    """F5 暴露上限序列（生产落参版，与 lab_downyear3 的 F5 逐位一致）。

    cap = max(动量cap, 环境cap)。两者取**大** = 任一状态好转即释放——
    这是机制必需而非调参：取 min（双重保守）会把年化砍到 4~6%
    （lab_downyear5 R2 参数网格，18 组 max 族全落在 11.45~11.95% 宽平台）。

      - 动量cap：基准 ETF 250 日收益 < 0 -> 上限 momentum_floor（ark-1.7.0: 0.25）
      - 环境cap：前一日市场环境 PANIC/DEFEND/NEUTRAL/ATTACK -> 0.10/0.35/0.70/1.00
        （ark-1.7.0 加陡档；1.6.0 为 0.30/0.55/0.80/1.00）

    无前视：动量序列右移一日（第 k 日只用 ≤k-1 收盘价），环境用 dates[k-1]
    的 regime（build 时已内嵌右移，不再二次移位）。regime 缺失时退化为
    纯动量 cap。返回 None 表示不启用（关闭或数据不足）。
    """
    cfg = cfg if cfg is not None else (C.RISK_CONTROL.get("exposure_cap") or {})
    if not cfg.get("enabled"):
        return None
    sid = book.bench_etf
    if not sid:
        return None
    n = len(book.dates)
    win = int(cfg.get("momentum_window", 250))
    floor = float(cfg.get("momentum_floor", 0.35))

    px = np.array([book.close(sid, i) or np.nan for i in range(n)], dtype=float)
    ok = ~np.isnan(px)
    if ok.sum() < win + 10:
        return None
    idxs = np.where(ok)[0]
    px = np.interp(np.arange(n), idxs, px[idxs])
    mom = np.full(n, np.nan)
    mom[win:] = px[win:] / px[:-win] - 1.0
    mom_cap = np.where(mom < 0, floor, 1.0)
    mom_cap[np.isnan(mom)] = 1.0
    mom_cap = np.concatenate([[mom_cap[0]], mom_cap[:-1]])  # 右移一日

    env_caps = cfg.get("env_caps") or {}
    if regime is None:
        path = f"{C.CACHE_DIR}/regime.json"
        if os.path.exists(path):
            with open(path) as f:
                regime = json.load(f)
    env_cap = np.ones(n)
    if regime:
        for k in range(1, n):
            e = (regime.get(book.dates[k - 1]) or {}).get("regime")
            env_cap[k] = float(env_caps.get(e, 1.0))
    return np.maximum(mom_cap, env_cap)


def plan_w(book: "EtfBook", sid: str, buy_i: int, score: Optional[float],
           risk_parity: bool, rp_window: int, score_tilt: float) -> float:
    """建仓权重 w = 1/σ × (综合分/50)^score_tilt，与 step 2 建仓时的算法逐位一致。

    独立成函数是为了让「按本金换算」的导出复用同一口径——前端只需要原始 w，
    占比 = 暴露系数 × w / Σw，不需要自己重算 σ。
    """
    sig = _sigma(book, sid, buy_i, rp_window) if risk_parity else 0.25
    w = (1.0 / max(sig, 0.05)) if risk_parity else 1.0
    if score_tilt:
        w *= max(float(score or 50.0), 1.0) ** score_tilt / (50.0 ** score_tilt)
    return w


def _sigma(book: "EtfBook", sid: str, k: int, window: int = 60) -> float:
    """标的截至第 k-1 日的年化波动（只用已实现数据，无前视）。"""
    vals = [book.close(sid, j) for j in range(max(0, k - window), k)]
    vals = [v for v in vals if v]
    if len(vals) < 11:
        return 0.25
    rr = np.diff(vals) / np.array(vals[:-1])
    sig = float(np.std(rr) * np.sqrt(252))
    return sig if sig > 1e-4 else 0.25


def simulate_cash_account(book: EtfBook, trades: List[dict],
                          start_date: Optional[str] = None,
                          max_holdings: int = 32,
                          initial_capital: float = 1.0,
                          target_positions: Optional[int] = None,
                          max_new_per_day: Optional[int] = None,
                          risk_parity: bool = False,
                          rp_window: int = 60,
                          score_tilt: float = 0.0,
                          vol_target: Optional[float] = None,
                          vol_window: int = 60,
                          vol_floor: float = 0.80,
                          floor_series: Optional[np.ndarray] = None,
                          cap_series: Optional[np.ndarray] = None,
                          entry_gate: Optional[callable] = None,
                          sid_cooldown: Optional[int] = None,
                          sid_cooldown_loss_below: float = 0.0,
                          market_gate: bool = False,
                          gate_window: int = 200,
                          max_per_sid: Optional[int] = None,
                          cash_yield: float = 0.010,
                          end_date: Optional[str] = None) -> dict:
    """**有限资金池的可执行账户（主口径）**：净值由现金账逐日记账产生。

    与"用公式给净值"的旧口径的根本区别——这里**没有每日再平衡**。账户只有：
      · 初始本金一笔钱，之后**卖出所得是唯一资金来源**；
      · 建仓在 T+1 开盘，金额同时受「预算」与「可用现金」约束；
      · 卖出只在规则允许时发生（默认只有持有到期），所得现金供后续建仓；
      · 未投入的现金按 cash_yield 计息。
    持仓权重一旦建立就**随价格自由漂移**，不再被拉回目标。因此：
      · 1/σ 与分数倾斜只决定「建仓时的预算大小」；
      · 波动目标 / 暴露上限只决定「建仓预算的闸门」，**无法凭空降低存量暴露**
        （要降必须显式卖出，本口径不采用）。

    资金分配（逐笔）：
        K   = target_positions（目标持仓笔数；None → max_holdings）
        wᵢ  = 建仓时点的 (1/σᵢ) × (综合分ᵢ/50)^score_tilt
        预算 = V_prev × expo × (wᵢ / w̄) / K      # w̄ = 在场持仓∪今日候选 的平均 w
        买入 = min(预算, 可用现金)                # 现金不够就少买，绝不卖出腾挪

    时序（严格按真实先后）：① 开盘买入（**只用昨收现金**）→ ② 收盘卖出
    （当日卖出所得**次日**才可用）→ ③ 现金计息 → ④ 收盘盯市。
    因此「当日到期的持仓」在当日开盘仍占槽位，腾出的槽位次日才可用。

    成本：按开仓名义额计 C.ROUND_TRIP_COST（佣金万二 + 滑点万五，双边），
    在卖出时从所得扣除，使单笔实现收益 = 毛收益 − 成本，与 settle_trade 同口径。
    """
    dates = book.dates
    s_i = 0
    if start_date:
        s_i = book.idx.get(start_date)
        if s_i is None:
            # 非交易日（周末/节假日）顺延到下一个交易日；否则会静默退回全历史
            nxt = [i for i, d0 in enumerate(dates) if d0 >= start_date]
            s_i = nxt[0] if nxt else 0
    start_k = max(s_i, 1)
    # 只用前 N 个交易日时（滚动一年分布）可提前收尾：账户路径与"跑到今天"完全一致，
    # 因为任何一天的建仓/盯市都只用 ≤ 当日的行情（无前视）。
    k_end = len(dates)
    if end_date:
        e_i = book.idx.get(end_date)
        if e_i is None:
            nxt = [i for i, x in enumerate(dates) if x >= end_date]
            e_i = nxt[0] if nxt else len(dates) - 1
        k_end = min(len(dates), e_i + 1)
    K = int(target_positions or max_holdings or 1)
    COST = C.ROUND_TRIP_COST
    rf = cash_yield / 252.0

    gate = bench_ma_gate(book, gate_window) if market_gate else None
    blocked_by_gate = 0

    # ---------------- 建仓候选（信号日 T 的结论 → 买入日 T+1） ----------------
    plan: Dict[int, List[dict]] = defaultdict(list)
    skipped_untradable = 0
    for t in trades:
        i0 = book.idx.get(t["date"])
        if i0 is None or i0 < s_i:
            continue
        H = int(t["horizon"])
        b_i = i0 + 1
        if t.get("sell_date"):
            e_i = book.idx.get(t["sell_date"], i0 + 1 + H)
        else:
            e_i = i0 + 1 + H
        if b_i >= len(dates) or e_i <= b_i:
            continue
        sid0 = t.get("etf_sid") or book.etf_of(t["sw_name"], dates[b_i])
        if sid0 is None:
            skipped_untradable += 1
            continue
        if gate is not None and not gate[b_i]:
            blocked_by_gate += 1
            continue
        if entry_gate is not None and not entry_gate(sid0, b_i):
            blocked_by_gate += 1
            continue
        plan[b_i].append({"sw": t["sw_name"], "sid": sid0, "end": e_i,
                          "horizon": H, "action": t.get("action"),
                          "reason": t.get("reason"),
                          "score": float(t.get("composite") or 0)})

    # ---------------- 账户状态 ----------------
    cash = float(initial_capital)
    holdings: List[dict] = []
    nav: List[float] = [float(initial_capital)]
    nav_dates: List[str] = [dates[max(start_k, 1) - 1] if start_k > 0 else dates[0]]
    bench_nav: List[float] = [1.0]
    cur, bcur = float(initial_capital), 1.0
    events: List[dict] = []
    plans: Dict[str, dict] = {}
    hold_count: List[int] = []
    gate_log: List[float] = []          # 每日实际使用的建仓预算闸门（暴露系数）
    deployed_log: List[float] = []
    cash_ratio_log: List[float] = []
    ret_hist: List[float] = []
    n_buys = n_sells = 0
    blocked_by_capacity = blocked_by_cash = blocked_by_sid_cap = blocked_by_cooldown = 0
    tickets: List[float] = []
    sid_block_until: Dict[str, int] = {}
    bench_sid = book.bench_etf
    prev_close_cache: Dict[str, float] = {}

    def _bench_ret(from_i: int, to_i: int) -> Optional[float]:
        if not bench_sid:
            return None
        bb, bs = book.open_(bench_sid, from_i), book.close(bench_sid, to_i)
        if not bb or not bs or bb <= 0:
            return None
        return bs / bb - 1.0 - COST

    for k in range(start_k, k_end):
        d = dates[k]
        day_events: List[dict] = []
        prev_holdings = list(holdings)          # T 收盘账本（当日买入前）
        V_prev = nav[-1]                        # 昨收账户总值（无前视）

        # ---- 当日建仓预算闸门：波动目标 × 暴露上限（只用 ≤k-1 的数据）----
        expo = 1.0
        floor_k = float(floor_series[k]) if floor_series is not None else vol_floor
        if vol_target is not None and len(ret_hist) >= 20:
            sig = float(np.std(ret_hist[-vol_window:]) * np.sqrt(252)) if ret_hist else 0.0
            if sig > 1e-6:
                expo = float(np.clip(vol_target / sig, floor_k, 1.0))
        if cap_series is not None:
            expo = float(np.clip(min(expo, float(cap_series[k])), 0.0, 1.0))

        # ---- ① 开盘买入（只用昨收现金；当日到期仍占槽位）----
        adds_all = sorted(plan.get(k, []), key=lambda x: -x["score"])
        if max_new_per_day is not None:
            adds_all = adds_all[:max_new_per_day]
        slots_free = max(0, max_holdings - len(holdings))
        blocked_by_capacity += max(0, len(adds_all) - slots_free)
        adds = adds_all[:slots_free]
        if adds:
            # w̄：在场持仓（建仓时权重）× 今日候选（当日权重），用于预算倾斜归一
            ws = [h["w"] for h in holdings]
            cand_w = []
            for it in adds:
                w0 = plan_w(book, it["sid"], k, it["score"], risk_parity, rp_window, score_tilt)
                it["w"] = w0
                cand_w.append(w0)
            allw = ws + cand_w
            w_bar = float(np.mean(allw)) if allw else 1.0
            for it in adds:
                sid0 = it["sid"]
                if max_per_sid is not None and sum(1 for x in holdings if x["sid"] == sid0) >= max_per_sid:
                    blocked_by_sid_cap += 1
                    continue
                if sid_cooldown is not None and sid_block_until.get(sid0, -1) >= k:
                    blocked_by_cooldown += 1
                    continue
                px = book.open_(sid0, k)
                if not px or px <= 0:
                    continue
                budget = V_prev * expo * (it["w"] / w_bar) / K if K > 0 else 0.0
                amt = min(budget, cash)
                if amt <= 1e-9 or amt < budget * 0.05:
                    blocked_by_cash += 1
                    continue
                shares = amt / px
                cash -= amt
                holdings.append({"sw": it["sw"], "sid": sid0, "secid": sid0,
                                 "buy_i": k, "buy_px": px, "shares": shares, "amt": amt,
                                 "end": it["end"], "horizon": it["horizon"],
                                 "action": it["action"], "reason": it["reason"],
                                 "score": it["score"], "w": it["w"]})
                n_buys += 1
                tickets.append(amt)
                mv_i = shares * (book.close(sid0, k) or px)
                day_events.append({
                    # 事件精度：price 6 位、amt/sh 10 位 —— verify_ledger 要用它严格重放现金账，
                    # 6 位小数在"把现金花到接近 0"的建仓上会累积出 ~1e-5 的舍入亏空。
                    "k": "B", "sw": it["sw"], "sid": sid0, "price": round(px, 6),
                    "h": it["horizon"], "a": it["action"], "sc": round(it["score"], 1),
                    "w": round(it["w"], 4), "amt": round(amt, 10), "sh": round(shares, 10),
                    "w_share": round(mv_i / (cash + sum(
                        x["shares"] * (book.close(x["sid"], k) or x["buy_px"]) for x in holdings)), 5)
                    if (cash + sum(x["shares"] * (book.close(x["sid"], k) or x["buy_px"])
                                   for x in holdings)) > 0 else None,
                })

        # ---- ② 收盘卖出（到期；当日所得次日才可用）----
        kept: List[dict] = []
        for h in holdings:
            c = book.close(h["secid"], k)
            if h["end"] <= k:
                px = c or prev_close_cache.get(h["sid"])
                if px:
                    proceeds = h["shares"] * px - h["amt"] * COST
                    cash += proceeds
                    gross = px / h["buy_px"] - 1.0 if h["buy_px"] else None
                    net = (gross - COST) if gross is not None else None
                    bre = _bench_ret(h["buy_i"], k)
                    n_sells += 1
                    if sid_cooldown is not None and net is not None and net < sid_cooldown_loss_below:
                        sid_block_until[h["sid"]] = k + sid_cooldown
                    day_events.append({
                        "k": "S", "sw": h["sw"], "sid": h["secid"],
                        "price": round(px, 6), "bd": dates[h["buy_i"]],
                        "bpx": round(h["buy_px"], 6), "h": h["horizon"], "a": h["action"],
                        "why": h["reason"], "gross": round(gross, 5) if gross is not None else None,
                        "ret": round(net, 5) if net is not None else None,
                        "ex": round(net - bre, 5) if (net is not None and bre is not None) else None,
                        "w": round(h["w"], 4), "amt": round(proceeds, 10),
                        "sh": round(h["shares"], 10),
                        "w_share": round(h["shares"] * px / (cash - proceeds + h["shares"] * px), 5)
                        if (cash - proceeds + h["shares"] * px) > 0 else None,
                    })
                continue
            kept.append(h)
        holdings = kept

        # ---- ③ 现金计息 ----④ 盯市 ----
        cash *= (1.0 + rf)
        mv = 0.0
        for h in holdings:
            c = book.close(h["secid"], k) or prev_close_cache.get(h["sid"]) or h["buy_px"]
            mv += h["shares"] * c
        cur = cash + mv
        nav.append(cur)
        nav_dates.append(d)
        hold_count.append(len(holdings))
        gate_log.append(expo)
        deployed_log.append(mv / cur if cur > 0 else 0.0)
        cash_ratio_log.append(cash / cur if cur > 0 else 1.0)
        ret_hist.append(cur / V_prev - 1.0 if V_prev > 0 else 0.0)

        if day_events:
            idx = len(nav) - 1
            for ev in day_events:
                ev["i"] = idx
            events.extend(day_events)

        # ---- 逐日「建仓计划块」（「我的持仓」页用；只含 ≤T 数据）----
        if k - 1 >= start_k:
            sig_date = dates[k - 1]
            book_rows, ws_now = [], [h["w"] for h in prev_holdings]
            w_bar_now = float(np.mean(ws_now)) if ws_now else 1.0
            for h in prev_holdings:
                book_rows.append([h["secid"], dates[h["buy_i"]], round(h["w"], 4),
                                  1 if h["end"] <= k else 0])
            pick_rows = [[it["sw"], it["sid"],
                          round(plan_w(book, it["sid"], k, it["score"], risk_parity,
                                       rp_window, score_tilt), 4),
                          round(it["score"], 2)] for it in plan.get(k, [])]
            plans[sig_date] = {
                "date": sig_date, "buy_date": d, "exposure": round(expo, 4),
                "max_holdings": int(max_holdings), "max_new_per_day": int(max_new_per_day or 0),
                "target_positions": K,
                "budget_per_slot": round(V_prev * expo / K, 6) if K else 0.0,
                "cash_ratio": round(cash_ratio_log[-1], 4),
                "w_bar": round(w_bar_now, 4),
                "room": int(max(0, max_holdings - len(prev_holdings))),
                "book": book_rows, "picks": pick_rows,
            }

        # ---- 基准 ----
        if bench_sid:
            bprev, bnow = book.close(bench_sid, k - 1), book.close(bench_sid, k)
            if bprev and bnow:
                bcur *= (bnow / bprev)
        bench_nav.append(bcur)
        prev_close_cache = {h["sid"]: book.close(h["secid"], k) for h in holdings
                            if book.close(h["secid"], k)}

    # ---------------- 统计 ----------------
    nav_arr = np.array(nav)
    bench_arr = np.array(bench_nav)
    stats = perf_stats(nav_arr, nav_dates, bench_arr if bench_sid else None)
    first_i = next((i for i, x in enumerate(hold_count) if x > 0), 0) + 1
    if 0 < first_i < len(nav) - 100:
        eff = perf_stats(nav_arr[first_i:], nav_dates[first_i:],
                         bench_arr[first_i:] if bench_sid else None)
        eff["start"] = nav_dates[first_i]
        stats["effective"] = eff
    stats.update({
        "basis": "executable_cash_account",
        "skipped_untradable": skipped_untradable,
        "executed_trades": n_buys,
        "planned_trades": sum(len(v) for v in plan.values()),
        "blocked_by_capacity": blocked_by_capacity,
        "blocked_by_cash": blocked_by_cash,
        "blocked_by_sid_cap": blocked_by_sid_cap,
        "blocked_by_cooldown": blocked_by_cooldown,
        "blocked_by_gate": blocked_by_gate,
        "sid_cooldown": sid_cooldown,
        "market_gate": bool(market_gate),
        "max_holdings": max_holdings,
        "target_positions": K,
        "initial_capital": float(initial_capital),
        "risk_parity": bool(risk_parity),
        "vol_target": vol_target,
        "vol_floor": vol_floor if vol_target is not None else None,
        "floor_series": floor_series is not None,
        "cap_series": cap_series is not None,
        "avg_deployed": round(float(np.mean(deployed_log)), 3) if deployed_log else None,
        "avg_cash_ratio": round(float(np.mean(cash_ratio_log)), 3) if cash_ratio_log else None,
        "min_cash_ratio": round(float(np.min(cash_ratio_log)), 4) if cash_ratio_log else None,
        "final_cash_ratio": round(cash_ratio_log[-1], 4) if cash_ratio_log else None,
        "avg_holdings": round(float(np.mean(hold_count)), 2) if hold_count else None,
        "max_holdings_used": int(max(hold_count)) if hold_count else 0,
        "avg_ticket": round(float(np.mean(tickets)), 5) if tickets else None,
        "n_sells": n_sells,
        "open_at_end": len(holdings),
    })
    stats["avg_exposure"] = stats["avg_deployed"]
    stats["avg_invested"] = stats["avg_deployed"]
    used = {ev["sid"] for ev in events}
    etf_names = {sid: book.listing.get(sid, {}).get("name", sid) for sid in used}
    return {
        "dates": nav_dates,
        "nav": [round(x, 5) for x in nav],
        "bench": [round(x, 5) for x in bench_nav],
        "stats": stats,
        "events": events,
        "etf_names": etf_names,
        "plans": plans,
        "end_book": [[h["secid"], dates[h["buy_i"]], round(h["w"], 4)] for h in holdings],
        "holdings": [0] + hold_count,
        # gate = 建仓预算闸门（vol_target × 暴露上限），与 nav 等长；起始日为 1.0
        "gate": [1.0] + [round(x, 4) for x in gate_log],
        "exposure": [0.0] + [round(x, 4) for x in deployed_log],
        "invested": [0.0] + [round(x, 4) for x in deployed_log],
    }


def rolling_fresh_entry(book: EtfBook, trades: List[dict],
                        horizon: int = 252, **kw) -> Optional[dict]:
    """**独立起账**的一年分布：把每个交易日都当作入市日，各起一本新账（初始全现金）。

    与 `perf_stats` 里的 rolling_1y 不是同一个问题：
      · perf_stats.rolling_1y = 同一条净值曲线自身的 252 日滚动窗口（假设从数据起点
        就一直在场，窗口之间有重叠与路径依赖）；
      · 本函数 = 「我某天入场、只持有一年」的真实体验——每个入市日的一切都从现金重来，
        账户状态（现金/槽位/在手笔数）也重来，这才是预期管理该看的分布。
    同时给出**同一窗口**的基准 ETF 收益，用来回答「随机入场持有一年，能不能跑赢躺着不动」。

    无前视：每个起点只用该起点之后的行情，且跑满 horizon 个交易日即收尾（路径与跑到今天一致）。
    """
    dates = book.dates
    rets: List[float] = []
    brets: List[float] = []
    bsid = book.bench_etf
    # 起点：全部交易日，但留出 horizon 个交易日；不早于第一笔候选结论。
    first = min((book.idx[t["date"]] for t in trades if t.get("date") in book.idx),
                default=0)
    for i in range(first, max(first, len(dates) - horizon - 1)):
        r = simulate_cash_account(book, trades, start_date=dates[i], end_date=dates[i + horizon], **kw)
        nav = r["nav"]
        if len(nav) <= horizon:
            continue
        rets.append(nav[horizon] / nav[0] - 1.0)
        if bsid:
            b0 = book.close(bsid, book.idx[r["dates"][0]])
            b1 = book.close(bsid, book.idx[r["dates"][horizon]])
            if b0 and b1:
                brets.append(b1 / b0 - 1.0)
    if len(rets) < 50:
        return None
    a = np.array(rets)
    b = np.array(brets) if len(brets) == len(rets) else None
    out = {
        "n": int(a.size),
        "mean": round(float(a.mean()), 4),
        "median": round(float(np.median(a)), 4),
        "win_rate": round(float((a > 0).mean()), 3),
        "p5": round(float(np.percentile(a, 5)), 4),
        "p95": round(float(np.percentile(a, 95)), 4),
        "worst": round(float(a.min()), 4),
        "best": round(float(a.max()), 4),
        "horizon": int(horizon),
    }
    if b is not None:
        out.update({
            "bench_mean": round(float(b.mean()), 4),
            "bench_median": round(float(np.median(b)), 4),
            "beat_rate": round(float((a > b).mean()), 3),
        })
    return out


def simulate_etf_portfolio(book: EtfBook, trades: List[dict],
                           start_date: Optional[str] = None,
                           max_holdings: int = 999,
                           target_positions: Optional[int] = None,
                           initial_capital: float = 1.0,
                           market_gate: bool = False,
                           gate_window: int = 200,
                           risk_parity: bool = False,
                           rp_window: int = 60,
                           vol_target: Optional[float] = None,
                           vol_window: int = 60,
                           vol_floor: float = 0.80,
                           floor_series: Optional[np.ndarray] = None,
                           cap_series: Optional[np.ndarray] = None,
                           loser_deweight: Optional[dict] = None,
                           entry_gate: Optional[callable] = None,
                           sid_cooldown: Optional[int] = None,
                           sid_cooldown_loss_below: float = 0.0,
                           cash_yield: float = 0.010,
                           score_tilt: float = 0.0,
                           max_new_per_day: Optional[int] = None,
                           max_per_sid: Optional[int] = None) -> dict:
    """把全部可交易结论滚成**有限资金池的可执行资金曲线**（主口径）。

    自 ark-1.8.0 起，本函数不再自己算净值，而是委托 simulate_cash_account()：
    现金账户逐日记账、只买不卖（除到期）、无每日再平衡。保留本入口只是为了
    不打断 review / lab / verify 的调用点；参数语义见 simulate_cash_account。

    参数（沿用旧名，语义已改为**入场侧**）：
      · risk_parity / rp_window：建仓预算按 1/σ 倾斜（不再是每日重算权重）
      · score_tilt：建仓预算再乘 (综合分/50)^tilt
      · vol_target / vol_floor / floor_series / cap_series：只作为**建仓预算闸门**，
        不能降低存量暴露（存量只能靠到期自然退出）
      · max_holdings / max_new_per_day / max_per_sid / sid_cooldown / entry_gate /
        market_gate：入场约束，语义不变
    """
    # 旧口径（每日再平衡、公式给净值）已废弃：统一委托给「有限资金池可执行账户」。
    # 保留本函数名与参数是为了不打断 review / lab / verify 的调用点。
    return simulate_cash_account(
        book, trades, start_date=start_date, max_holdings=max_holdings,
        initial_capital=initial_capital,
        max_new_per_day=max_new_per_day,
        target_positions=(target_positions if target_positions is not None
                          else getattr(C, "PORTFOLIO", {}).get("target_positions")),
        risk_parity=risk_parity, rp_window=rp_window, score_tilt=score_tilt,
        vol_target=vol_target, vol_window=vol_window, vol_floor=vol_floor,
        floor_series=floor_series, cap_series=cap_series, entry_gate=entry_gate,
        sid_cooldown=sid_cooldown, sid_cooldown_loss_below=sid_cooldown_loss_below,
        market_gate=market_gate, gate_window=gate_window,
        max_per_sid=max_per_sid, cash_yield=cash_yield,
    )


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
