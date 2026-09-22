# -*- coding: utf-8 -*-
"""离线重建 review.json：只跑「特征库缓存 → 结论重放 → 复盘引擎」，
不访问数据源（East Money 被限流时也能用）。

前提：cache/ 下的 features.npz / regime.json / etf_panel.json 是最新的。
适用场景：只改了复盘/结算逻辑，行情数据没有变化，不需要全量 --all。

    python rebuild_review.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pipeline as PL  # noqa: E402
import review as RV  # noqa: E402
import rules as RULES  # noqa: E402


def main() -> None:
    print("[1/3] 载入上下文（离线缓存，不访问网络）...")
    ctx = PL.load_context(refresh=False)
    fs = ctx["fs"]
    print(f"      特征库：{len(fs.codes)} 标的 x {len(fs.dates)} 天 "
          f"({fs.dates[0]} -> {fs.dates[-1]})")
    book = PL.load_etf_book()

    start_i = C.WARMUP
    if C.BACKTEST_START:
        bi = next((i for i, d in enumerate(fs.dates) if d >= C.BACKTEST_START), None)
        if bi is not None:
            start_i = max(start_i, bi)
    dates = fs.dates[start_i:]

    print(f"[2/3] 重放 {len(dates)} 个交易日 ({dates[0]} -> {dates[-1]}) ...")
    t0 = time.time()
    conclusions = PL.run_range(ctx, dates, book=book)
    print(f"      重放完成，用时 {time.time() - t0:.0f}s")

    print("[3/3] 运行复盘引擎 ...")
    rep = RV.build_review(fs, conclusions, dates, ctx["fwd"], ctx["regime"], book=book)
    today_path = f"{C.OUT_DIR}/today.json"
    today = json.load(open(today_path, encoding="utf-8")) if os.path.exists(today_path) else {}
    rep["rules"] = RULES.build_rules(settlement=rep.get("settlement"), today=today)
    rep["rules"]["engine_version"] = PL.ENGINE_VERSION
    rep["rules"]["config_hash"] = PL.config_hash()
    json.dump(rep, open(f"{C.OUT_DIR}/review.json", "w", encoding="utf-8"),
              ensure_ascii=False)

    s = rep.get("settlement") or {}
    if s:
        st = s.get("stats") or {}
        print(f"      可结算 {s.get('n_settled')} 条 + 在途 {s.get('n_open_edge')} 条，"
              f"期末在场 {st.get('open_at_end')} 笔")
        print(f"      年化 {st.get('cagr')}，最大回撤 {st.get('maxdd')}，"
              f"成交 {st.get('executed_trades')} 笔")
    print(f"      已写入 {C.OUT_DIR}/review.json")


if __name__ == "__main__":
    main()
