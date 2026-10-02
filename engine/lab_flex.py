# -*- coding: utf-8 -*-
"""持仓灵活性实验台：容量约束诊断 + 退出规则阴影重放（离线，只读，不写生产产物）。

背景（用户提出的问题）：
    现行主口径下**卖出只有一个原因——持有到期**（H=80/100，见 settlement.simulate_cash_account
    的 `h["end"] <= k` 分支）。而账户容量 32 槽、每笔预算 = V/K（K=24），于是出现两个后果：
      · 槽位被"锁"80-100 天，中途再好的新信号也挤不进来（blocked_by_capacity）；
      · 存量仓位在中途无论走成什么样都不能减（"要降存量只能显式卖出，本口径不采用"）。

    历史上已否决的退出规则全部是**对称的**（MA20 破位 / 移动止损 / 到期延展）：它们对赢家与输家
    一视同仁，实测把每笔期望从 +5.67% 砍到 +1.12%，持仓只剩 3.6 笔（docs/周期错配归因审计）。
    本实验台测的是**从未实现过的两类**：
      A. 相对退出（换仓 / 淘汰赛）：只有当新信号显著强于"最弱在位持仓"时才卖弱换强；
      B. 非对称绝对退出：固定止损 / 止盈 / 时间止损 / 环境破位清仓。

    但在跑任何规则之前，必须先回答一个纯统计问题：**被容量挡掉的信号，事后是否真的比在位持仓
    更好？** 若答案是否定的，那么"灵活性"最多只能改善回撤（少投钱），不可能改善收益。

用法：
    python engine/lab_flex.py diag     # 诊断：容量剖面 + 被挡候选 vs 在位持仓（约 1-2min）
    python engine/lab_flex.py path     # 持仓路径自相关：浮盈能否预测剩余收益（约 1min）
    python engine/lab_flex.py base     # 复现基线（与 review.json 对照）
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import settlement as ST  # noqa: E402

SIM = f"{C.OUT_DIR}/sim.json"
REVIEW = f"{C.OUT_DIR}/review.json"


# ---------------------------------------------------------------- 装载
def load_trades() -> list[dict]:
    """生产同源候选：sim.json 的 days[*].p（与 lab_exit.py 同一来源）。"""
    s = json.load(open(SIM, encoding="utf-8"))
    out = []
    for d, day in s["days"].items():
        for sw, sid, w, sc, sell, H, act in day["p"]:
            out.append({"date": d, "sw_name": sw, "horizon": int(H), "composite": sc,
                        "sell_date": sell, "etf_sid": sid, "action": act})
    return out


def base_kwargs(sim: dict) -> dict:
    """生产参数（与 review/pipeline 调用点一致）。"""
    RC, PF = C.RISK_CONTROL, C.PORTFOLIO
    return dict(max_holdings=sim["max_holdings"], target_positions=sim["K"],
                risk_parity=True, rp_window=RC["rp_window"],
                vol_target=RC["vol_target"], vol_window=RC["vol_window"],
                vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
                score_tilt=PF["score_tilt"], max_new_per_day=sim["max_new_per_day"])


def fwd(book, sid: str, k: int, h: int) -> float | None:
    """决策日 k（**行情账簿下标**）之后 h 个交易日的**可执行**前瞻收益：
    k 开盘买 → k+h 收盘卖。"""
    if sid is None:
        return None
    b = book.open_(sid, k)
    s = book.close(sid, k + h)
    if not b or not s or b <= 0:
        return None
    return s / b - 1.0 - C.ROUND_TRIP_COST


def fwd_mark(book, sid: str, k: int, h: int) -> float | None:
    """同一窗口的**持有**收益（收盘→收盘），用于在位持仓的镜像对照（它已在场内）。"""
    if sid is None:
        return None
    a = book.close(sid, k)
    s = book.close(sid, k + h)
    if not a or not s or a <= 0:
        return None
    return s / a - 1.0


def fmt(v, pct=True, digits=2):
    if v is None:
        return "  n/a"
    return (f"{100*v:.{digits}f}%" if pct else f"{v:.{digits}f}")


# ---------------------------------------------------------------- diag
def cmd_diag() -> None:
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    kw = base_kwargs(sim)
    r = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **kw)
    st = r["stats"]
    print("=" * 92)
    print("① 基线复现（sim.json 生产同源候选）")
    print("=" * 92)
    print(f"  候选 {len(trades)} 条 / 账户起算 {C.BACKTEST_START} / K={kw['target_positions']} "
          f"/ 容量 {kw['max_holdings']} / 每日新建上限 {kw['max_new_per_day']}")
    print(f"  年化 {fmt(st['cagr'])}  回撤 {fmt(st['maxdd'])}  Sharpe {st['sharpe']}  "
          f"Calmar {st['calmar']}")
    print(f"  平均投出 {fmt(st['avg_deployed'])}  平均持仓 {st['avg_holdings']} 笔  "
          f"最大用到 {st['max_holdings_used']} 槽")
    print(f"  成交 {st['executed_trades']}  / 计划 {st['planned_trades']}"
          f"（成交率 {100*st['executed_trades']/max(1,st['planned_trades']):.1f}%）")
    print(f"  挡单：容量 {st['blocked_by_capacity']} / 现金 {st['blocked_by_cash']} / "
          f"闸门 {st['blocked_by_gate']} / 同标的上限 {st['blocked_by_sid_cap']}")
    try:
        q = json.load(open(REVIEW, encoding="utf-8"))["settlement"]["stats"]
        print(f"  —— 生产 review.json 同期读数：年化 {fmt(q['cagr'])} 回撤 {fmt(q['maxdd'])} "
              f"Sharpe {q['sharpe']} 成交 {q['executed_trades']} 计划 {q['planned_trades']}")
    except Exception as e:  # pragma: no cover
        print(f"  （review.json 读取失败：{e}）")

    dates = r["dates"]
    # nav / nav_dates / holdings / invested 四者等长且逐位对齐（首元素是起账哨兵），
    # 直接用即可 —— 不要再切 [1:]，两边一起切会把对齐又错开一位。
    hold_s = np.array(r["holdings"], dtype=float)
    inv_s = np.array(r["invested"], dtype=float)
    n = len(dates)
    assert len(hold_s) == n and len(inv_s) == n, (len(dates), len(hold_s), len(inv_s))
    cap = kw["max_holdings"]

    print()
    print("=" * 92)
    print("② 容量占用剖面（槽位是真正的稀缺品吗）")
    print("=" * 92)
    at_cap = hold_s >= cap
    print(f"  满仓天数 {int(at_cap.sum())}/{n} = {100*at_cap.mean():.1f}%")
    for lo, hi in (("2018-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"),
                   ("2022-01-01", "2023-12-31"), ("2024-01-01", "2025-12-31"),
                   ("2026-01-01", "2099-12-31")):
        m = np.array([lo <= d <= hi for d in dates])
        if not m.any():
            continue
        print(f"  {lo[:4]}-{hi[:4]}  满仓 {100*at_cap[m].mean():5.1f}%  "
              f"平均持仓 {hold_s[m].mean():5.2f} 笔  平均投出 {fmt(float(inv_s[m].mean()))}"
              f"  峰值投出 {fmt(float(inv_s[m].max()))}")
    print(f"  全区间：平均持仓 {hold_s.mean():.2f} 笔 / 平均投出 {fmt(float(inv_s.mean()))}")

    # ---------- 被挡候选 vs 在位持仓 ----------
    # 逐日重建在位持仓（取 result.plans，它存的就是"当日买入前"的账本快照）
    plans = r["plans"]
    # 已成交集合：(买入日, 标的)
    executed = set()
    for ev in r["events"]:
        if ev["k"] == "B":
            executed.add((dates[ev["i"]], ev["sid"]))

    # ⚠️ 索引口径：dates/nav/holdings 是**净值序列**下标，价格查询要用**行情账簿**下标，
    # 两者差一个起账偏移（本机 dates[0]=2018-01-01 在 book.dates 的第 4357 位）。
    # 直接把净值下标喂给 book.open_ 会查到 2013 年的空数据，静默返回 None。
    def bi_of(d: str):
        return book.idx.get(d)

    blocked_f, held_f, exec_f = [], [], []
    swap_rows = []
    n_full = 0
    for sig, pl in plans.items():
        kb = bi_of(pl["buy_date"])
        if kb is None:
            continue
        bk = pl.get("book") or []          # 当日买入前的在位持仓 [sid, buy_date, w, expired]
        if len(bk) < cap:
            continue                        # 还没满仓 → 不存在"挤不进来"的问题
        n_full += 1
        picks = pl.get("picks") or []      # 当日计划 [sw, sid, w, score]
        if not picks:
            continue
        held = [x[0] for x in bk]
        cand = [(p[1], p[3]) for p in picks if (pl["buy_date"], p[1]) not in executed]
        took = [(p[1], p[3]) for p in picks if (pl["buy_date"], p[1]) in executed]
        Hs = [t["horizon"] for t in trades if t["date"] == sig]
        if not Hs:
            continue
        h = int(np.median(Hs))
        for sid, sc in cand:
            f = fwd(book, sid, kb, h)
            if f is not None:
                blocked_f.append(f)
        for sid, sc in took:
            f = fwd(book, sid, kb, h)
            if f is not None:
                exec_f.append(f)
        # 换仓对照：被挡候选 vs 在位持仓的同期收益（同一窗口、同一决策时点）
        cf = [fwd(book, s, kb, h) for s, _ in cand]
        cf = [x for x in cf if x is not None]
        hf = [fwd_mark(book, s, kb, h) for s in held]
        hf = [x for x in hf if x is not None]
        hmax = [x for x, _ in zip(hf, hf)]
        if cf and hf:
            swap_rows.append((pl["buy_date"], float(np.mean(cf)), float(np.min(hf)),
                              float(np.mean(hf)), float(np.max(hf)), len(held)))
    print(f"\n  （满仓且在账本快照里可见的决策日：{n_full} 天）")

    def desc(name, xs):
        if not xs:
            print(f"  {name:28s} n=0")
            return
        a = np.array(xs)
        print(f"  {name:28s} n={a.size:4d}  均值 {100*a.mean():+6.2f}%  "
              f"中位 {100*np.median(a):+6.2f}%  胜率 {100*(a>0).mean():5.1f}%  "
              f"p5 {100*np.percentile(a,5):+7.2f}%  p95 {100*np.percentile(a,95):+7.2f}%")

    print()
    print("=" * 92)
    print("③ 满仓日：被容量挡掉的候选 vs 当日真正成交的候选（同样持有期，可执行口径）")
    print("=" * 92)
    desc("被挡候选（没挤进来）", blocked_f)
    desc("成交候选（挤进来了）", exec_f)
    if blocked_f and exec_f:
        d = np.mean(blocked_f) - np.mean(exec_f)
        print(f"  → 均值差 {100*d:+.2f}pp（被挡 − 成交）；"
              f"{'被挡的更好' if d > 0 else '被挡的更差'}")

    if swap_rows:
        print()
        print("=" * 92)
        print("④ 换仓的即期算术：被挡新候选 vs 在位持仓的同期收益（满仓日）")
        print("=" * 92)
        cand_m = np.array([x[1] for x in swap_rows])
        weak_m = np.array([x[2] for x in swap_rows])
        all_m = np.array([x[3] for x in swap_rows])
        print(f"  满仓且当日有候选被挡的决策日：{len(swap_rows)} 天")
        print(f"  被挡候选（均值）      {100*cand_m.mean():+6.2f}%")
        print(f"  在位最弱持仓（同期）  {100*weak_m.mean():+6.2f}%   → 差 "
              f"{100*(cand_m.mean()-weak_m.mean()):+.2f}pp")
        print(f"  在位全体现持仓（同期）{100*all_m.mean():+6.2f}%   → 差 "
              f"{100*(cand_m.mean()-all_m.mean()):+.2f}pp")
        win = (cand_m > weak_m).mean()
        print(f"  逐日比较「被挡候选 > 最弱持仓」的比例：{100*win:.1f}%")


# ---------------------------------------------------------------- path
def cmd_path() -> None:
    """持仓路径自相关：持有到第 t 日时的浮盈，能否预测"从 t 日到到期"的剩余收益？

    这是所有"砍亏留盈"类退出规则的必要前提。若相关系数 ≈ 0，则任何基于当前浮盈的
    退出规则都只是把随机数重新切一刀，不可能改善期望（只会增加摩擦）。
    """
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    kw = base_kwargs(sim)
    r = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **kw)
    dates = r["dates"]

    # 从事件重建每笔已成交交易（用**日期字符串**，避免净值下标/行情下标混用）
    pos = defaultdict(list)
    done = []
    for ev in r["events"]:
        if ev["k"] == "B":
            # B 事件没有 bd 字段（那是 S 事件带的），买入日就是事件当日
            pos[(dates[ev["i"]], ev["sw"], ev["sid"])].append(ev)
        elif ev["k"] == "S":
            key = (ev["bd"], ev["sw"], ev["sid"])   # key[0] 即买入日
            if pos.get(key):
                b = pos[key].pop(0)
                done.append((key[0], dates[ev["i"]], b["sid"]))
    print(f"重建已成交交易 {len(done)} 笔")

    pts = defaultdict(list)
    for bd, sd, sid in done:
        bi, si = book.idx.get(bd), book.idx.get(sd)
        if bi is None or si is None:
            continue
        H = si - bi
        if H < 40:
            continue
        b = book.open_(sid, bi)
        if not b or b <= 0:
            continue
        cs = book.close(sid, si)
        if not cs:
            continue
        for t in (10, 20, 30, 40, 60):
            if t >= H:
                continue
            ct = book.close(sid, bi + t)
            if not ct:
                continue
            run = ct / b - 1.0                 # 截至第 t 日的浮盈（可观测）
            rem = cs / ct - 1.0                # 从第 t 日到实际卖出的剩余收益
            pts[t].append((run, rem))

    print()
    print("=" * 92)
    print("⑤ 持仓路径：第 t 日浮盈 → 剩余收益（到实际到期）的相关性")
    print("=" * 92)
    print(f"  {'t(日)':>6} {'n':>6} {'corr':>8} {'浮盈<0组 剩余均值':>18} "
          f"{'浮盈>0组 剩余均值':>18} {'差':>8}")
    for t in (10, 20, 30, 40, 60):
        v = pts.get(t) or []
        if len(v) < 30:
            continue
        a = np.array([x[0] for x in v])
        b = np.array([x[1] for x in v])
        corr = float(np.corrcoef(a, b)[0, 1])
        lo = b[a < 0]
        hi = b[a >= 0]
        print(f"  {t:>6} {a.size:>6} {corr:>8.3f} "
              f"{100*lo.mean() if lo.size else float('nan'):>17.2f}% "
              f"{100*hi.mean() if hi.size else float('nan'):>17.2f}% "
              f"{100*(hi.mean()-lo.mean()) if lo.size and hi.size else float('nan'):>7.2f}pp")
    print()
    print("  读法：corr ≈ 0 或为负 ⇒ 浮盈不可外推，「砍亏留盈」没有统计基础；")
    print("        corr 显著为正 ⇒ 赢家确实会继续赢，非对称退出值得一试。")


# ---------------------------------------------------------------- base
def cmd_base() -> None:
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    kw = base_kwargs(sim)
    r = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **kw)
    s = r["stats"]
    for k in ("cagr", "maxdd", "sharpe", "calmar", "avg_deployed", "avg_holdings",
              "executed_trades", "planned_trades", "blocked_by_capacity",
              "blocked_by_cash", "days", "open_at_end", "final_cash_ratio"):
        print(f"  {k:22s} = {s.get(k)}")
    print(f"  exit_rule              = {s.get('exit_rule')}")


# ================================================================== 带退出钩子的账户
def simulate(book, trades, exits: dict | None = None, start_date=None,
             max_holdings: int = 32, initial_capital: float = 1.0,
             target_positions: int = 24, max_new_per_day=None,
             risk_parity: bool = True, rp_window: int = 60, score_tilt: float = 0.0,
             cash_yield: float = 0.0, regime: dict | None = None,
             collect_plans: bool = False,
             vol_target=None, vol_window: int = 60, vol_floor: float = 0.8,
             **ignored) -> dict:
    """与 `settlement.simulate_cash_account` **逐位一致**的可执行账户，另加退出钩子。

    一致性保证：`exits=None` 时本函数必须与生产函数给出完全相同的 nav 序列。
    用法上先跑 `cmd_verify()` 做逐位比对，再挂规则 —— 这是本项目「先精确复现基线」的硬要求。

    exits（全部默认关闭；任何一项开启都是口径变更）：
      tp / sl          止盈 / 止损：当日收盘浮盈 ≥ tp（浮亏 ≤ −sl）→ 标记 → **次日开盘**平仓
      ts_days / ts_min 时间止损：持有 ≥ ts_days 且浮盈 < ts_min → 标记 → 次日开盘平仓
      regime_out       环境破位：**前一日** regime ∈ regime_out → 标记 → 次日开盘平仓
      regime_out_neg   配合 regime_out：仅平掉浮亏仓（True）/ 全部平仓（False）
      rot_gap          换仓（淘汰赛）：**当日开盘建仓前**，若新候选分 − 最弱在持仓的入场分
                       ≥ rot_gap，则卖出最弱仓（开盘价）换入新候选；此时不占新槽位

    退出执行时序（与生产 break_days 同一条路径）：收盘观测 → 标记 → 次日**开盘**平仓，
    所得现金当日即可用于建仓，槽位当日释放。这正是「提升灵活性」要测量的一天。
    """
    ex = exits or {}
    # 波动目标在本口径下已停用（config 里 vol_target=None）。若被打开，说明基线已变，
    # 本台不静默忽略 —— 直接报错，避免拿错基线做对照。
    assert vol_target is None, "vol_target 非空：本台未实现预算闸门，基线口径已变"
    assert not ignored, f"未识别参数 {sorted(ignored)}"
    tp = ex.get("tp")
    sl = ex.get("sl")
    ts_days = ex.get("ts_days")
    ts_min = ex.get("ts_min", 0.0)
    reg_out = set(ex.get("regime_out") or [])
    reg_neg_only = bool(ex.get("regime_out_neg", False))
    rot_gap = ex.get("rot_gap")

    dates = book.dates
    s_i = 0
    if start_date:
        s_i = book.idx.get(start_date)
        if s_i is None:
            nxt = [i for i, d0 in enumerate(dates) if d0 >= start_date]
            s_i = nxt[0] if nxt else 0
    start_k = max(s_i, 1)
    k_end = len(dates)
    K = int(target_positions or max_holdings or 1)
    COST = C.ROUND_TRIP_COST
    rf = cash_yield / 252.0

    plan: Dict[int, List[dict]] = defaultdict(list)
    skipped_untradable = 0
    for t in trades:
        i0 = book.idx.get(t["date"])
        if i0 is None or i0 < s_i:
            continue
        H = int(t["horizon"])
        b_i = i0 + 1
        e_i = book.idx.get(t["sell_date"], i0 + 1 + H) if t.get("sell_date") else i0 + 1 + H
        if b_i >= len(dates) or e_i <= b_i:
            continue
        sid0 = t.get("etf_sid") or book.etf_of(t["sw_name"], dates[b_i])
        if sid0 is None:
            skipped_untradable += 1
            continue
        plan[b_i].append({"sw": t["sw_name"], "sid": sid0, "end": e_i,
                          "horizon": H, "action": t.get("action"),
                          "reason": t.get("reason"),
                          "score": float(t.get("composite") or 0)})

    cash = float(initial_capital)
    holdings: List[dict] = []
    nav = [float(initial_capital)]
    nav_dates = [dates[max(start_k, 1) - 1] if start_k > 0 else dates[0]]
    events: List[dict] = []
    hold_count: List[int] = []
    deployed_log: List[float] = []
    plans: Dict[str, dict] = {}
    n_buys = n_sells = 0
    blocked_by_capacity = blocked_by_cash = 0
    n_rot = 0
    exit_reasons: Dict[str, int] = defaultdict(int)
    prev_close_cache: Dict[str, float] = {}
    bench_sid = book.bench_etf
    tickets: List[float] = []

    def _bench_ret(from_i: int, to_i: int):
        if not bench_sid:
            return None
        bb, bs = book.open_(bench_sid, from_i), book.close(bench_sid, to_i)
        if not bb or not bs or bb <= 0:
            return None
        return bs / bb - 1.0 - COST

    for k in range(start_k, k_end):
        d = dates[k]
        day_events: List[dict] = []
        prev_holdings = list(holdings)
        V_prev = nav[-1]

        # ---- ⓪ 开盘执行昨日标记的退出（所有原因共用一条路径）----
        still: List[dict] = []
        for h in holdings:
            if not h.get("_exit_open"):
                still.append(h)
                continue
            px = book.open_(h["secid"], k) or book.close(h["secid"], k)
            if not px:
                still.append(h)
                continue
            proceeds = h["shares"] * px - h["amt"] * COST
            cash += proceeds
            gross = px / h["buy_px"] - 1.0 if h["buy_px"] else None
            net = (gross - COST) if gross is not None else None
            n_sells += 1
            exit_reasons[h.get("_exit_why") or "早退"] += 1
            day_events.append({
                "k": "S", "sw": h["sw"], "sid": h["secid"], "price": round(px, 6),
                "bd": dates[h["buy_i"]], "bpx": round(h["buy_px"], 6),
                "h": h["horizon"], "a": h["action"], "why": h.get("_exit_why"),
                "gross": round(gross, 5) if gross is not None else None,
                "ret": round(net, 5) if net is not None else None,
                "ex": round(net - _bench_ret(h["buy_i"], k), 5)
                      if _bench_ret(h["buy_i"], k) is not None else None,
                "w": round(h["w"], 4), "amt": round(proceeds, 10),
                "sh": round(h["shares"], 10), "early": True,
                "held": k - h["buy_i"],
            })
        holdings = still

        # ---- ① 开盘买入 ----
        adds_all = sorted(plan.get(k, []), key=lambda x: -x["score"])
        if max_new_per_day is not None:
            adds_all = adds_all[:max_new_per_day]
        slots_free = max(0, max_holdings - len(holdings))
        blocked_by_capacity += max(0, len(adds_all) - slots_free)
        adds = list(adds_all[:slots_free])
        # 换仓（淘汰赛）：槽位已满时，允许"卖弱换强"——被换掉的仓在同一开盘卖出
        displaced: List[dict] = []
        if rot_gap is not None and len(adds) < len(adds_all):
            remain = adds_all[len(adds):]
            sold_sids = set()
            for cand in remain:
                if len(holdings) - len(displaced) + len(adds) >= max_holdings:
                    pass
                pool = [h for h in holdings
                        if h not in displaced and h["sid"] not in sold_sids
                        and h["buy_i"] < k]                     # 不当日买当日换
                if not pool:
                    break
                weak = min(pool, key=lambda h: h.get("score") or 0)
                if cand["score"] - (weak.get("score") or 0) < rot_gap:
                    break
                px = book.open_(weak["secid"], k) or book.close(weak["secid"], k)
                if not px:
                    continue
                proceeds = weak["shares"] * px - weak["amt"] * COST
                cash += proceeds
                gross = px / weak["buy_px"] - 1.0 if weak["buy_px"] else None
                net = (gross - COST) if gross is not None else None
                n_sells += 1
                n_rot += 1
                exit_reasons["换仓淘汰"] += 1
                sold_sids.add(weak["sid"])
                displaced.append(weak)
                day_events.append({
                    "k": "S", "sw": weak["sw"], "sid": weak["secid"],
                    "price": round(px, 6), "bd": dates[weak["buy_i"]],
                    "bpx": round(weak["buy_px"], 6), "h": weak["horizon"],
                    "a": weak["action"],
                    "why": f"换仓：被 {cand['sw']}(分 {cand['score']:.0f}) 淘汰",
                    "gross": round(gross, 5) if gross is not None else None,
                    "ret": round(net, 5) if net is not None else None,
                    "ex": None, "w": round(weak["w"], 4),
                    "amt": round(proceeds, 10), "sh": round(weak["shares"], 10),
                    "early": True, "held": k - weak["buy_i"], "rot": True,
                })
                adds.append(cand)
            holdings = [h for h in holdings if h not in displaced]

        if adds:
            ws = [h["w"] for h in holdings]
            cand_w = []
            for it in adds:
                w0 = ST.plan_w(book, it["sid"], k, it["score"], risk_parity,
                               rp_window, score_tilt)
                it["w"] = w0
                cand_w.append(w0)
            allw = ws + cand_w
            w_bar = float(np.mean(allw)) if allw else 1.0
            for it in adds:
                sid0 = it["sid"]
                px = book.open_(sid0, k)
                if not px or px <= 0:
                    continue
                budget = V_prev * (it["w"] / w_bar) / K if K > 0 else 0.0
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
                day_events.append({
                    "k": "B", "sw": it["sw"], "sid": sid0, "price": round(px, 6),
                    "h": it["horizon"], "a": it["action"], "sc": round(it["score"], 1),
                    "w": round(it["w"], 4), "amt": round(amt, 10),
                    "sh": round(shares, 10),
                })

        # ---- ② 收盘：到期卖出 + 标记新退出 ----
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
                    n_sells += 1
                    exit_reasons["到期"] += 1
                    day_events.append({
                        "k": "S", "sw": h["sw"], "sid": h["secid"],
                        "price": round(px, 6), "bd": dates[h["buy_i"]],
                        "bpx": round(h["buy_px"], 6), "h": h["horizon"], "a": h["action"],
                        "why": h["reason"],
                        "gross": round(gross, 5) if gross is not None else None,
                        "ret": round(net, 5) if net is not None else None,
                        "ex": round(net - _bench_ret(h["buy_i"], k), 5)
                              if _bench_ret(h["buy_i"], k) is not None else None,
                        "w": round(h["w"], 4), "amt": round(proceeds, 10),
                        "sh": round(h["shares"], 10), "held": k - h["buy_i"],
                    })
                continue
            if h.get("_exit_open"):
                kept.append(h)
                continue
            # 标记退出条件（只用截至当日的收盘价，次日开盘执行）
            if c and h["buy_px"]:
                r = c / h["buy_px"] - 1.0
                held = k - h["buy_i"]
                why = None
                if tp is not None and r >= tp:
                    why = f"止盈（浮盈 {r:.1%}）"
                elif sl is not None and r <= -sl:
                    why = f"止损（浮亏 {r:.1%}）"
                elif ts_days and held >= ts_days and r < ts_min:
                    why = f"时间止损（{held} 日浮盈 {r:.1%}）"
                if why is None and reg_out and regime and k >= 1:
                    prev = (regime.get(dates[k - 1]) or {}).get("regime")
                    if prev in reg_out and (not reg_neg_only or r < 0):
                        why = f"环境破位（前一日 {prev}）"
                if why:
                    h["_exit_open"] = True
                    h["_exit_why"] = why
            kept.append(h)
        holdings = kept

        # ---- ③ 计息 ④ 盯市 ----
        cash *= (1.0 + rf)
        mv = 0.0
        for h in holdings:
            c = book.close(h["secid"], k) or prev_close_cache.get(h["sid"]) or h["buy_px"]
            mv += h["shares"] * c
        cur = cash + mv
        nav.append(cur)
        nav_dates.append(d)
        hold_count.append(len(holdings))
        deployed_log.append(mv / cur if cur > 0 else 0.0)

        if day_events:
            idx = len(nav) - 1
            for ev in day_events:
                ev["i"] = idx
            events.extend(day_events)

        if collect_plans and k - 1 >= start_k:
            sig_date = dates[k - 1]
            book_rows = [[h["secid"], dates[h["buy_i"]], round(h["w"], 4),
                          1 if h["end"] <= k else 0] for h in prev_holdings]
            plans[sig_date] = {
                "date": sig_date, "buy_date": d,
                "cash_ratio": round(cash / cur, 4) if cur > 0 else 1.0,
                "room": int(max(0, max_holdings - len(prev_holdings))),
                "book": book_rows,
                "picks": [[it["sw"], it["sid"],
                           round(ST.plan_w(book, it["sid"], k, it["score"], risk_parity,
                                            rp_window, score_tilt), 4),
                           round(it["score"], 2)] for it in plan.get(k, [])],
            }

        prev_close_cache = {h["sid"]: book.close(h["secid"], k) for h in holdings
                            if book.close(h["secid"], k)}

    nav_arr = np.array(nav)
    stats = ST.perf_stats(nav_arr, nav_dates)
    stats.update({
        "avg_deployed": round(float(np.mean(deployed_log)), 3) if deployed_log else None,
        "avg_holdings": round(float(np.mean(hold_count)), 2) if hold_count else None,
        "max_holdings_used": int(max(hold_count)) if hold_count else 0,
        "executed_trades": n_buys, "n_sells": n_sells,
        "planned_trades": sum(len(v) for v in plan.values()),
        "blocked_by_capacity": blocked_by_capacity,
        "blocked_by_cash": blocked_by_cash,
        "skipped_untradable": skipped_untradable,
        "open_at_end": len(holdings),
        "n_rot": n_rot,
        "exit_reasons": dict(exit_reasons),
    })
    return {"dates": nav_dates, "nav": nav, "stats": stats, "events": events,
            "plans": plans, "holdings": [0] + hold_count,
            "invested": [0.0] + deployed_log}


def yearly(nav, dates):
    """按年切分净值 → 年收益。"""
    out = {}
    for i in range(1, len(nav)):
        y = dates[i][:4]
        out.setdefault(y, [1.0, 1.0])
        out[y][0] *= nav[i] / nav[i - 1]
        out[y][1] *= nav[i] / nav[i - 1]
    # 起始年可能只有部分
    res = {}
    for y, (_r, cum) in out.items():
        res[y] = cum - 1.0
    return res


def run_variant(book, trades, regime, exits, label, collect=False):
    kw = dict(max_holdings=32, target_positions=24, max_new_per_day=2,
              risk_parity=True, rp_window=60, score_tilt=0.5, cash_yield=0.0,
              start_date=C.BACKTEST_START)
    r = simulate(book, trades, exits=exits, regime=regime, collect_plans=collect, **kw)
    return r


def cmd_why() -> None:
    """三件事：① 分年一致性；② 止损为何失败（赢家的路径回撤）；③ 满仓是否本身是反向信号。"""
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    regime = json.load(open(f"{C.CACHE_DIR}/regime.json", encoding="utf-8"))
    cap = 32
    base_kw = dict(max_holdings=cap, target_positions=24, max_new_per_day=2,
                   risk_parity=True, rp_window=60, score_tilt=0.5, cash_yield=0.0,
                   start_date=C.BACKTEST_START)

    # ---------- ① 分年一致性 ----------
    picks = [("基线", {}), ("止损-15%", {"sl": 0.15}), ("止损-25%", {"sl": 0.25}),
             ("时间止损40", {"ts_days": 40, "ts_min": 0.0}), ("止盈+15%", {"tp": 0.15}),
             ("止盈+40%", {"tp": 0.40}), ("换仓差10", {"rot_gap": 10}),
             ("环境PANIC清仓", {"regime_out": {"PANIC"}})]
    res = {}
    for label, ex in picks:
        r = simulate(book, trades, exits=ex, regime=regime, **base_kw)
        res[label] = (yearly(r["nav"], r["dates"]), r["stats"], r)
    years = sorted(res["基线"][0])
    print("=" * 118)
    print("① 分年收益（口径变更必须看方向是否跨年一致；单年耀眼 = 嫌疑）")
    print("=" * 118)
    print("%-16s" % "变体" + "".join(f"{y:>9}" for y in years) + f"{'合计年化':>10}")
    for label, _ in picks:
        ys, s, _ = res[label]
        print("%-16s" % label + "".join(f"{100*ys.get(y,0):>8.1f}%" for y in years)
              + f"{100*s['cagr']:>9.2f}%")
    print()
    print("  逐年「优于基线」的年数：")
    for label, _ in picks[1:]:
        ys = res[label][0]
        yb = res["基线"][0]
        win = sum(1 for y in years if ys.get(y, 0) > yb.get(y, 0))
        print(f"    {label:<16} {win}/{len(years)}")

    # ---------- ② 止损为何失败：赢家是否都曾深亏 ----------
    b = res["基线"][2]
    dates = b["dates"]
    pos = defaultdict(list)
    done = []
    for ev in b["events"]:
        if ev["k"] == "B":
            pos[(dates[ev["i"]], ev["sw"], ev["sid"])].append(ev)
        elif ev["k"] == "S":
            key = (ev["bd"], ev["sw"], ev["sid"])
            if pos.get(key):
                bb = pos[key].pop(0)
                done.append((key[0], dates[ev["i"]], bb["sid"]))
    print()
    print("=" * 118)
    print("② 止损为何失败：赢家在持有期内是否都曾深亏过（出场前路径最低点 vs 最终收益）")
    print("=" * 118)
    rows = []
    for bd, sd, sid in done:
        bi, si = book.idx.get(bd), book.idx.get(sd)
        if bi is None or si is None or si - bi < 20:
            continue
        ent = book.open_(sid, bi)
        if not ent or ent <= 0:
            continue
        seg = [book.close(sid, j) for j in range(bi, si + 1)]
        seg = [x for x in seg if x]
        if not seg:
            continue
        trough = min(seg) / ent - 1.0            # 持有期内最差浮亏
        final = book.close(sid, si) / ent - 1.0 - C.ROUND_TRIP_COST
        rows.append((trough, final))
    tr = np.array([x[0] for x in rows])
    fi = np.array([x[1] for x in rows])
    n = tr.size
    print(f"  已成交交易 n={n}；最终盈利（净）比例 {100*(fi>0).mean():.1f}%；"
          f"平均净收益 {100*fi.mean():+.2f}%")
    print(f"  持有期内最低点中位数 {100*np.median(tr):+.1f}%，最深 {100*tr.min():+.1f}%")
    print()
    print("  %-22s %7s %12s %12s %10s" % ("若设该止损", "会被打掉", "被打掉者最终", "未被打者最终", "损失"))
    for th in (-0.10, -0.15, -0.20, -0.25, -0.30):
        hit = tr <= th
        kept = ~hit
        print("  %-22s %6.1f%% %11.2f%% %11.2f%% %9s" % (
            f"浮亏 {100*th:.0f}% 即砍", 100 * hit.mean(),
            100 * fi[hit].mean() if hit.any() else float("nan"),
            100 * fi[kept].mean() if kept.any() else float("nan"),
            "—"))
    # 关键：被砍掉的那批里，最终有多少是赚钱的
    print()
    for th in (-0.10, -0.15, -0.20, -0.25):
        hit = tr <= th
        if not hit.any():
            continue
        print(f"  触发 {100*th:.0f}% 止损的 {int(hit.sum()):3d} 笔中，最终仍是**盈利**的占 "
              f"{100*(fi[hit]>0).mean():.1f}%（均值 {100*fi[hit].mean():+.2f}%）")

    # ---------- ③ 满仓本身是不是反向信号 ----------
    hold_s = np.array(b["holdings"], dtype=float)
    full = hold_s >= cap
    bsid = book.bench_etf
    print()
    print("=" * 118)
    print("③ 满仓是否本身是反向信号：满仓日 vs 非满仓日的前瞻基准收益")
    print("=" * 118)
    for h in (20, 60, 80):
        f_full, f_idle = [], []
        for i, d in enumerate(dates):
            j = book.idx.get(d)
            if j is None or j + h >= len(book.dates):
                continue
            b0, b1 = book.close(bsid, j), book.close(bsid, j + h)
            if not b0 or not b1:
                continue
            rr = b1 / b0 - 1.0
            (f_full if full[i] else f_idle).append(rr)
        if f_full and f_idle:
            print(f"  前瞻 {h:>2} 日基准收益：满仓日 n={len(f_full):4d} 均值 "
                  f"{100*np.mean(f_full):+6.2f}%   非满仓 n={len(f_idle):4d} 均值 "
                  f"{100*np.mean(f_idle):+6.2f}%   差 "
                  f"{100*(np.mean(f_full)-np.mean(f_idle)):+6.2f}pp")


def cmd_scan() -> None:
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    regime = json.load(open(f"{C.CACHE_DIR}/regime.json", encoding="utf-8"))

    vs = [
        ("基线：只到期卖（现行）", {}),
        ("时间止损 30 日浮亏退", {"ts_days": 30, "ts_min": 0.0}),
        ("时间止损 40 日浮亏退", {"ts_days": 40, "ts_min": 0.0}),
        ("时间止损 40 日 <+5% 退", {"ts_days": 40, "ts_min": 0.05}),
        ("时间止损 60 日浮亏退", {"ts_days": 60, "ts_min": 0.0}),
        ("固定止损 -10%", {"sl": 0.10}),
        ("固定止损 -15%", {"sl": 0.15}),
        ("固定止损 -20%", {"sl": 0.20}),
        ("固定止损 -25%", {"sl": 0.25}),
        ("止盈 +15%", {"tp": 0.15}),
        ("止盈 +25%", {"tp": 0.25}),
        ("止盈 +40%", {"tp": 0.40}),
        ("环境破位 PANIC 清仓", {"regime_out": {"PANIC"}}),
        ("环境破位 PANIC+DEFEND 清仓", {"regime_out": {"PANIC", "DEFEND"}}),
        ("环境破位 PANIC 只清浮亏仓", {"regime_out": {"PANIC"}, "regime_out_neg": True}),
        ("换仓 淘汰差 10 分", {"rot_gap": 10}),
        ("换仓 淘汰差 20 分", {"rot_gap": 20}),
        ("换仓 淘汰差 30 分", {"rot_gap": 30}),
        ("时间止损40 + 固定止损-20%", {"ts_days": 40, "ts_min": 0.0, "sl": 0.20}),
        ("时间止损40 + 止盈+40%", {"ts_days": 40, "ts_min": 0.0, "tp": 0.40}),
        ("换仓20 + 时间止损40", {"rot_gap": 20, "ts_days": 40, "ts_min": 0.0}),
    ]

    print("=" * 118)
    print("退出规则组合层扫描（生产同源候选 / 2018-01 起 / 2117 日 / 与生产逐位一致的模拟器）")
    print("=" * 118)
    hdr = ("%-30s %8s %8s %7s %7s %8s %7s %6s %6s" %
           ("变体", "年化", "回撤", "Sharpe", "Calmar", "平均投出", "持仓笔", "成交", "卖次"))
    print(hdr)
    print("-" * 118)
    rows = {}
    for label, exits in vs:
        r = run_variant(book, trades, regime, exits, label)
        s = r["stats"]
        rows[label] = (s, r)
        print("%-30s %7.2f%% %7.2f%% %7.2f %7.3f %7.1f%% %7.2f %6d %6d" % (
            label, 100 * s["cagr"], 100 * s["maxdd"], s["sharpe"], s["calmar"],
            100 * s["avg_deployed"], s["avg_holdings"], s["executed_trades"],
            s["n_sells"]))
    print()
    base = rows["基线：只到期卖（现行）"][0]
    print("相对基线的差额（年化 / 回撤 / Sharpe / Calmar）：")
    for label, _ in vs[1:]:
        s = rows[label][0]
        print("  %-30s %+6.2fpp %+6.2fpp %+6.3f %+6.3f   %s" % (
            label, 100 * (s["cagr"] - base["cagr"]),
            100 * (s["maxdd"] - base["maxdd"]),
            s["sharpe"] - base["sharpe"], s["calmar"] - base["calmar"],
            s["exit_reasons"]))
    print()
    print("分年收益（基线 vs 前几名）：")
    ys = sorted(yearly(rows["基线：只到期卖（现行）"][1]["nav"],
                       rows["基线：只到期卖（现行）"][1]["dates"]).items())
    print("  %-6s" % "年" + "".join(f"{y:>9}" for y, _ in ys))
    print("  %-6s" % "基线" + "".join(f"{100*v:>8.1f}%" for _, v in ys))
    print()
    print("自检：基线与 cmd_verify 的生产读数应完全一致（年化 15.74% / 回撤 −33.13% / 成交 345）")


def cmd_verify() -> None:
    book = PL.load_etf_book()
    sim = json.load(open(SIM, encoding="utf-8"))
    trades = load_trades()
    kw = base_kwargs(sim)
    prod = ST.simulate_cash_account(book, trades, start_date=C.BACKTEST_START, **kw)
    mine = simulate(book, trades, exits=None, start_date=C.BACKTEST_START, **kw)
    a = np.array(prod["nav"], dtype=float)
    # ⚠️ 生产函数在出口把 nav 做了 round(x, 5)（前端展示精度），本台返回原始值。
    # 直接比原始值会看到 ~5e-06 的固定差（= 1e-5/2，纯取整），那不是逻辑差异。
    # 因此把本台的值也按同一精度取整后比较；同时另报未取整的最大偏差做留痕。
    b_raw = np.array(mine["nav"], dtype=float)
    b = np.round(b_raw, 5)
    print(f"nav 长度 生产 {a.size} / 本台 {b.size}")
    assert a.size == b.size, "长度不一致"
    print(f"（本台未取整 vs 生产已取整）最大绝对偏差 = {np.abs(a - b_raw).max():.3e}"
          f"   —— 应 ≈ 5e-06，即生产出口的 round(x,5)")
    d = np.abs(a - b)
    print(f"统一取整后逐点最大绝对偏差 = {d.max():.3e}")
    print(f"逐点是否全部相等 = {bool(np.array_equal(a, b))}")
    for k in ("cagr", "maxdd", "sharpe", "avg_deployed", "avg_holdings",
              "executed_trades", "planned_trades", "blocked_by_capacity",
              "blocked_by_cash", "n_sells"):
        print(f"  {k:22s} 生产 {prod['stats'].get(k)}  本台 {mine['stats'].get(k)}")
    print(f"  事件数 生产 {len(prod['events'])}  本台 {len(mine['events'])}")
    print("✅ 一致" if np.array_equal(a, b) else "❌ 不一致")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "diag"
    {"diag": cmd_diag, "path": cmd_path, "base": cmd_base, "why": cmd_why,
     "verify": cmd_verify, "scan": cmd_scan}[cmd]()


if __name__ == "__main__":
    main()

