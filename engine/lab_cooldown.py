# -*- coding: utf-8 -*-
"""P3 第二轮：可执行口径下的**尾部控制**扫描（离线）。

背景（见 docs/口径变更-2026-09-23-可执行账户.md 第七、九节）：
旧口径里风控可以直接缩放存量仓位，所以「降暴露 = 降回撤」。改成有限资金池后，
风控只剩「新仓预算的闸门」，压不了存量，F5/波动目标在可执行口径下**是负贡献**；
真正的尾部旋钮是 K（目标持仓笔数）：K 越大越稳、年化越低。

本轮要回答的是另一个问题：**还能不能在不牺牲年化的前提下压尾部**——
不许改存量仓位（那就要显式卖出，属于新机制，需单独批准），只允许改「新仓的准入」：

  变体 A 静默期（sid_cooldown）：某标的**亏损到期**后，N 个交易日内不再建仓它。
      只在"重复买同一只亏损标的"上省钱，不动其他仓位。
  变体 B 单标的并发上限（max_per_sid）：同一只 ETF 最多同时占 n 个槽位。
      现在没有上限，同一只 ETF 可以占满 32 个槽位（集中度风险）。
  变体 C 基准闸门（market_gate）：基准 ETF 跌破 200 日线时不开新仓。

全部用生产同源候选（day 文件里「有可交易 ETF 且周期>0」的结论）跑同一套可执行账户；
本表只做**变体之间的相对比较**（候选集与生产 trades_exec 有 ~0.3pp 口径差）。

运行：python lab_cooldown.py
"""
import sys
import os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import settlement as ST  # noqa: E402
import pipeline as PL  # noqa: E402
from lab_exec_risk import load_candidates, MAXH, START  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO
BASE = dict(max_holdings=MAXH, target_positions=PF["target_positions"],
            risk_parity=True, rp_window=RC["rp_window"],
            vol_target=RC["vol_target"], vol_window=RC["vol_window"],
            vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
            score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])


def variants():
    yield ("基线（K=24，无额外限制）", {})
    for n in (10, 20, 40):
        for th, lbl in ((0.0, "任何亏损"), (-0.05, "亏损>5%")):
            yield (f"静默期 {n} 日（{lbl}后）",
                   dict(sid_cooldown=n, sid_cooldown_loss_below=th))
    for n in (1, 2, 4):
        yield (f"单标的最多 {n} 槽", dict(max_per_sid=n))
    yield ("基准跌破 200 日线不开新仓", dict(market_gate=True, gate_window=200))


def main() -> None:
    book = PL.load_etf_book()
    trades = load_candidates()
    print(f"候选 {len(trades)} 条 · K={PF['target_positions']} · 容量 {MAXH}\n")
    rows = []
    for name, extra in variants():
        r = ST.simulate_cash_account(book, trades, start_date=START,
                                     **{**BASE, **extra})
        s = r["stats"]
        rows.append((name, s))
        print(f"{name:26s} 年化 {s['cagr']:7.2%}  回撤 {s['maxdd']:7.2%}  "
              f"Sharpe {s['sharpe']:.3f}  Calmar {s['calmar']:.3f}  "
              f"均投出 {s['avg_deployed']:.1%}  成交 {s['executed_trades']:4d}  "
              f"现金约束 {s['blocked_by_cash']:4d}  闸门挡下 {s['blocked_by_gate']:4d}")

    base = rows[0][1]
    print("\n相对基线的变化（年化 / 回撤 / Calmar）：")
    for name, s in rows[1:]:
        print(f"  {name:26s} {s['cagr'] - base['cagr']:+7.2%}  "
              f"{s['maxdd'] - base['maxdd']:+7.2%}  {s['calmar'] - base['calmar']:+.3f}")

    # 最差一年（独立起账）只看两个候选：基线 vs 唯一可能同时改善年化的变体
    print("\n独立起账的一年分布（耗时约 12s/档）：")
    for name, extra in [("基线", {}), ("静默期 20 日（任何亏损后）",
                                       dict(sid_cooldown=20, sid_cooldown_loss_below=0.0))]:
        rf = ST.rolling_fresh_entry(book, trades, horizon=252, **{**BASE, **extra})
        print(f"  {name:26s} 均值 {rf['mean']:+.2%}  中位 {rf['median']:+.2%}  "
              f"最差 {rf['worst']:+.2%}  为正 {rf['win_rate']:.1%}  "
              f"跑赢基准 {rf['beat_rate']:.1%}")


if __name__ == "__main__":
    main()
