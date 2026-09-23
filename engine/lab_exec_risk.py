# -*- coding: utf-8 -*-
"""可执行口径下的风控重扫（L4，离线）。

背景：2026-09-23 结算层口径修正——旧净值隐含「每日免费再平衡」，风控（波动目标 / F5 暴露上限）
可以直接缩放存量仓位，因此「降暴露 = 降回撤」。改成**有限资金池的可执行账户**后，风控只剩
「新仓预算的闸门」：压不了存量，只能少买、让存量随到期自然退出。**旧的全部风控结论必须重测**
（见 docs/口径变更-2026-09-23-可执行账户.md）。

本脚本用生产同源的候选结论（day 文件里所有「有可交易 ETF 且周期>0」的结论）跑同一套
可执行账户，逐项开关风控，看哪些旋钮在新口径下仍然有效。

注意：候选集与生产 `trades_exec` 有几条差异（生产走 settle_trade 结算 + open_edge 补尾部），
基线因此与生产净值有 ~0.3pp 的口径差；本表只做**变体之间的相对比较**。

运行：python lab_exec_risk.py
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import settlement as ST  # noqa: E402
import pipeline as PL  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO
MAXH = C.CLASSIFY["max_daily_picks"] * 4
START = C.BACKTEST_START or "2018-01-02"


def load_candidates() -> list:
    """生产同源候选：day 文件里每条「有可交易 ETF 且建议周期>0」的结论。"""
    out = []
    for f in sorted(glob.glob(f"{C.OUT_DIR}/day/*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d["date"] < START:
            continue
        for p in d.get("picks") or []:
            if not p.get("etf_code"):
                continue
            H = int((p.get("horizon") or {}).get("horizon") or 0)
            if H <= 0:
                continue
            out.append({"date": d["date"], "sw_name": p["name"], "horizon": H,
                        "composite": p.get("composite"), "action": p.get("action"),
                        "reason": p.get("trigger"), "etf_sid": None})
    return out


def main() -> None:
    book = PL.load_etf_book()
    trades = load_candidates()
    cap_prod = ST.build_exposure_cap(book)                 # ark-1.7 加陡档
    cap_soft = ST.build_exposure_cap(book, {              # ark-1.6 温和档
        "enabled": True, "momentum_window": 250, "momentum_floor": 0.35,
        "env_caps": {"PANIC": 0.30, "DEFEND": 0.55, "NEUTRAL": 0.80, "ATTACK": 1.00}})
    cap_steep = ST.build_exposure_cap(book, {             # 更陡一档
        "enabled": True, "momentum_window": 250, "momentum_floor": 0.25,
        "env_caps": {"PANIC": 0.10, "DEFEND": 0.30, "NEUTRAL": 0.60, "ATTACK": 1.00}})
    cap_mom35 = ST.build_exposure_cap(book, {             # 加陡环境档 + 动量 floor 0.35
        "enabled": True, "momentum_window": 250, "momentum_floor": 0.35,
        "env_caps": {"PANIC": 0.10, "DEFEND": 0.35, "NEUTRAL": 0.70, "ATTACK": 1.00}})

    prod = dict(risk_parity=True, rp_window=RC["rp_window"], vol_target=RC["vol_target"],
                vol_window=RC["vol_window"], vol_floor=RC["vol_floor"],
                cap_series=cap_prod, score_tilt=PF["score_tilt"],
                max_new_per_day=PF["max_new_per_day"], max_holdings=MAXH,
                target_positions=PF.get("target_positions", 16),
                cash_yield=RC["cash_yield"], start_date=START)

    def R(**over):
        kw = {**prod, **over}
        r = ST.simulate_cash_account(book, trades, **kw)
        s = r["stats"]
        return (s["cagr"], s["maxdd"], s["sharpe"], s["calmar"],
                s["avg_deployed"], s["avg_holdings"], s["blocked_by_cash"],
                s["final_cash_ratio"] or 0.0)

    rows = [
        ("基线（生产：rp+vol16/F.8+F5陡+tilt.5, 2/日, K16）", lambda: R()),
        ("无风控（等额预算 / 无闸门 / 无倾斜）", lambda: R(risk_parity=False, vol_target=None,
                                                    cap_series=None, score_tilt=0.0)),
        ("仅综合分倾斜 .5", lambda: R(risk_parity=False, vol_target=None, cap_series=None)),
        ("仅风险平价 1/σ", lambda: R(vol_target=None, cap_series=None, score_tilt=0.0)),
        ("仅波动目标 16%/floor .8", lambda: R(cap_series=None)),
        ("仅 F5 暴露上限（加陡档）", lambda: R(vol_target=None)),
        ("F5 温和档(1.6)", lambda: R(cap_series=cap_soft)),
        ("F5 加陡档 + 动量 floor .35", lambda: R(cap_series=cap_mom35)),
        ("F5 更陡（PANIC.10/DEF.30/NEU.60）", lambda: R(cap_series=cap_steep)),
        ("vol_floor .70", lambda: R(vol_floor=0.70)),
        ("vol_floor .90", lambda: R(vol_floor=0.90)),
        ("vol_target .12", lambda: R(vol_target=0.12)),
        ("vol_target .20", lambda: R(vol_target=0.20)),
        ("score_tilt 0（等预算）", lambda: R(score_tilt=0.0)),
        ("score_tilt .25", lambda: R(score_tilt=0.25)),
        ("K=8", lambda: R(target_positions=8)),
        ("K=12", lambda: R(target_positions=12)),
        ("K=24", lambda: R(target_positions=24)),
        ("K=32", lambda: R(target_positions=32)),
        ("每日新建 1 笔", lambda: R(max_new_per_day=1)),
        ("每日新建 3 笔", lambda: R(max_new_per_day=3)),
        ("亏损冷却 20 日（可执行）", lambda: R(sid_cooldown=20, sid_cooldown_loss_below=0.0)),
        ("亏损冷却 40 日", lambda: R(sid_cooldown=40, sid_cooldown_loss_below=0.0)),
    ]

    print(f"候选结论 {len(trades)} 条；账户起算 {START}；容量 {MAXH}；K={prod['target_positions']}")
    print("%-42s %8s %8s %7s %7s %8s %7s %8s %8s" % (
        "变体", "年化", "回撤", "Sharpe", "Calmar", "平均投出", "平均笔数", "现金挡单", "期末现金"))
    for label, fn in rows:
        c, m, sh, cal, dep, hold, bc, fc = fn()
        print("%-42s %7.2f%% %7.2f%% %7.2f %7.3f %7.1f%% %7.2f %8d %7.1f%%" % (
            label, 100 * c, 100 * m, sh, cal, 100 * dep, hold, bc, 100 * fc))


if __name__ == "__main__":
    main()
