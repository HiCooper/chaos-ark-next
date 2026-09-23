# -*- coding: utf-8 -*-
"""账本可执行性对账（ledger reconciliation）。

为什么必须有这一条：
    2026-09 发现生产净值曲线隐含「每日再平衡」——收益式 Σ(rᵢwᵢ)/Σwᵢ 每天把持仓拉回目标权重，
    而事件日志里只有建仓/到期卖出。两本账各自自洽，谁也没发现。根因是缺一条校验：
    **净值必须能由「事件日志 + 价格 + 现金」独立重放出来**——重放不出来，就说明有不存在的交易。

做法（完全不依赖模拟器代码，只读产物）：
    1) 读 web/public/data/review.json 的 settlement.curve.nav 与 settlement.events；
    2) 用事件重建一本现金账：B 事件扣现金加持仓、S 事件加现金平仓、每日现金计息、每日按收盘盯市；
    3) 逐日与发布净值对比；同时断言现金从不为负。

判定：净值最大相对偏差 < 5e-4（事件里的价格/股数是 6 位小数，允许舍入漂移），且现金 ≥ -1e-9。
任何隐含交易（例如每日再平衡）都会立刻放大成数量级偏差，直接 fail。

用法：python verify_ledger.py  → 打印对账结果，失败返回 1
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402  （用生产同款账簿加载器：含 qfq 份额折算复权）

NAV_TOL = 5e-4


def main() -> int:
    path = os.path.join(C.OUT_DIR, "review.json")
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    st = rep.get("settlement") or {}
    dates = (st.get("curve") or {}).get("dates") or []
    nav = (st.get("curve") or {}).get("nav") or []
    events = st.get("events") or []
    if not dates or not nav or not events:
        print("  ✗ review.json 缺少 curve/events，无法对账")
        return 1

    book = PL.load_etf_book()
    if book is None:
        print("  ✗ 无法加载 ETF 结算账簿")
        return 1
    rf = float(C.RISK_CONTROL["cash_yield"]) / 252.0

    by_day: dict[int, list] = {}
    for e in events:
        by_day.setdefault(int(e["i"]), []).append(e)

    # 注意：book 的日期轴是全历史（2012 起），curve.dates 只是回测窗口，
    # 必须逐日把窗口日期映射回 book 的索引，不能用 enumerate 的下标。
    kidx = [book.idx.get(d) for d in dates]
    cash = 1.0                                  # 初始本金（归一化）
    pos: dict[str, float] = {}                  # sid → 股数
    worst = 0.0
    worst_d = ""
    min_cash = cash
    for i, d in enumerate(dates):
        k = kidx[i]
        if k is None:
            continue
        for e in by_day.get(i, []):
            if e["k"] == "B":
                cash -= float(e["amt"])
                pos[e["sid"]] = pos.get(e["sid"], 0.0) + float(e.get("sh") or 0.0)
            else:
                cash += float(e["amt"])
                pos[e["sid"]] = max(0.0, pos.get(e["sid"], 0.0) - float(e.get("sh") or 0.0))
        cash *= (1.0 + rf)
        mv = 0.0
        for sid, sh in pos.items():
            if sh <= 0:
                continue
            px = book.close(sid, k)
            if px:
                mv += sh * px
        v = cash + mv
        if nav[i] > 0:
            dev = abs(v / nav[i] - 1.0)
            if dev > worst:
                worst, worst_d = dev, d
        min_cash = min(min_cash, cash)
        if os.environ.get("ARK_DBG") and d[5:10] in ("06-30", "12-31"):
            print(f"    [dbg] {d} cash={cash:.4f} mv={mv:.4f} v={v:.4f} nav={nav[i]:.4f} Σsh={sum(pos.values()):.4f}")

    ok_nav = worst < NAV_TOL
    ok_cash = min_cash >= -1e-9
    print(f"  账本重放：逐日净值最大偏差 {worst:.2e}（{worst_d}，阈值 {NAV_TOL:.0e}）"
          f"  {'✓' if ok_nav else '✗'}")
    print(f"  现金非负：最低现金 {min_cash:.6f}  {'✓' if ok_cash else '✗'}"
          f"   事件 {len(events)} 条 / {len(dates)} 个交易日")
    if not (ok_nav and ok_cash):
        print("  ✗ 对账失败：净值无法由事件日志重放，说明存在未披露的交易（例如再平衡）")
        return 1
    print("  ✓ 净值可由事件日志独立重放——不存在隐含交易")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
