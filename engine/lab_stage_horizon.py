# -*- coding: utf-8 -*-
"""阶段自适应持有周期实验台（L4，一次性产物，不影响生产口径）。

外部方案：按行业周期阶段调整持有周期——
  萌发期(SPROUT) 60-90 / 确认期(CONFIRM) 40-60 / 中期(MID) 25-40 / 成熟期(MATURE) 15-25。

与现有系统的关系：
  - 周期阶段已在信号层算好（scoring.compute_stage，展示层标注），day/*.json picks 带 stage；
  - 当前持有周期 = 历史相似状态匹配，从 HORIZON_CANDIDATES=[60,80,100] 里挑，
    与阶段完全解耦。本实验测「阶段 -> 持有周期」这一假设的增量价值。

方法：交易清单从生产 review.json B/S 事件重建（同源），stage 从 day/*.json picks
按（结论日, 行业名）回查。为了使 horizon 改动真实生效，重建时只保留与
「建议周期到期日」不一致的 sell_date（提前退出的真实事件），到期卖出的让
simulate 按新 horizon 推算。基线必须先精确复现生产净值。

变体：
  H1 阶段定点中值    SPROUT 75 / CONFIRM 50 / MID 32 / MATURE 20（"" 不变）
  H2 阶段区间夹取    h = clip(h, lo, hi)（保留 analog 信号，夹进阶段区间）
  H3 只缩成熟期      MATURE -> 20，其余不变（单测「兑现窗口」假设）
  H4 只延萌发期      SPROUT -> 75，其余不变（单测「低位耐心」假设）
  H5 只缩不延        h = min(h, hi)（方向保守版）

运行：python lab_stage_horizon.py  （离线，只读 L1/L3 缓存）
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import RC, load_trades_from_events, yearly  # noqa: E402

# 外部方案的阶段区间（交易日）
STAGE_RANGE = {
    "SPROUT": (60, 90),
    "CONFIRM": (40, 60),
    "MID": (25, 40),
    "MATURE": (15, 25),
}
STAGE_MID = {k: (lo + hi) // 2 for k, (lo, hi) in STAGE_RANGE.items()}


def load_stage_map() -> dict:
    """(结论日, 行业名) -> stage，来自 day/*.json picks（生产同源）。"""
    day_dir = f"{C.OUT_DIR}/day" if hasattr(C, "OUT_DIR") else "../web/public/data/day"
    stage = {}
    n_pick = 0
    for f in sorted(os.listdir(day_dir)):
        if not f.endswith(".json"):
            continue
        d = json.load(open(os.path.join(day_dir, f)))
        if d.get("error"):
            continue
        for p in d.get("picks", []):
            n_pick += 1
            if p.get("stage"):
                stage[(f[:-5], p["name"])] = p["stage"]
    print(f"stage 回查: {n_pick} 条 picks, 有 stage 的 {len(stage)} 条")
    return stage


def apply_horizon(trades: list[dict], stage_map: dict, rule) -> list[dict]:
    """按 rule(stage, h) -> h' 重算 horizon；无 stage 或规则返回 None 则不变。"""
    out = []
    changed = 0
    for t in trades:
        t2 = dict(t)
        st = stage_map.get((t["date"], t["sw_name"]))
        nh = rule(st, t["horizon"]) if st else None
        if nh is not None and nh != t["horizon"]:
            t2["horizon"] = int(nh)
            changed += 1
        out.append(t2)
    return out, changed


def drop_expiry_sell_dates(book: "ST.EtfBook", trades: list[dict]) -> list[dict]:
    """把「sell_date == 建议周期到期日」的交易的 sell_date 删掉，让 simulate
    按 horizon 推算（二者等价，基线不变）；提前退出的真实事件保留。"""
    out = []
    kept = dropped = 0
    for t in trades:
        t2 = dict(t)
        if t2.get("sell_date"):
            i0 = book.idx.get(t2["date"])
            exp = (i0 + 1 + int(t2["horizon"])) if i0 is not None else None
            si = book.idx.get(t2["sell_date"])
            if exp is not None and si == exp:
                del t2["sell_date"]
                dropped += 1
            else:
                kept += 1
        out.append(t2)
    print(f"sell_date 处理: 到期卖出删 {dropped} 个（horizon 接管）, 提前退出保留 {kept} 个")
    return out


def main() -> None:
    trades_raw, prod_dates = load_trades_from_events()
    stage_map = load_stage_map()
    book = ST.EtfBook(ST.load_etf_panel(f"{C.CACHE_DIR}/etf_panel.json"))
    trades0 = drop_expiry_sell_dates(book, trades_raw)

    kw = dict(start_date=prod_dates[0],
              max_holdings=32,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"])

    # ---- 基线复现校验 ----
    base = ST.simulate_etf_portfolio(book, trades0, **kw)
    prod = json.load(open("../web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    print(f"基线复现: cagr={base['stats']['cagr']:.4f} maxdd={base['stats']['maxdd']:.4f} "
          f"| 生产 nav 最大偏差={max(diffs):.2e}")

    # ---- 变体 ----
    variants = [
        ("H1 阶段定点中值", lambda st, h: STAGE_MID.get(st)),
        ("H2 阶段区间夹取", lambda st, h: (lambda r: min(max(h, r[0]), r[1]))(STAGE_RANGE[st]) if st in STAGE_RANGE else None),
        ("H3 只缩成熟期", lambda st, h: 20 if st == "MATURE" else None),
        ("H4 只延萌发期", lambda st, h: 75 if st == "SPROUT" else None),
        ("H5 只缩不延", lambda st, h: (min(h, STAGE_RANGE[st][1]) if st in STAGE_RANGE else None)),
    ]

    # 原始 horizon 分布（对照）
    st_count = defaultdict(int)
    for t in trades_raw:
        st_count[stage_map.get((t["date"], t["sw_name"]), "?")] += 1
    print("交易按阶段分布:", dict(st_count))

    rows = []
    for name, rule in variants:
        tv, nchg = apply_horizon(trades0, stage_map, rule)
        r = ST.simulate_etf_portfolio(book, tv, **kw)
        hs = [t["horizon"] for t in tv]
        rows.append((name, r, nchg, float(np.mean(hs))))
        print(f"{name}: 改动 {nchg} 笔, 平均 horizon {np.mean([t['horizon'] for t in trades0]):.0f} -> {np.mean(hs):.0f}")

    # ---- 汇总 ----
    print("\n=== 全区间指标 ===")
    print(f"{'变体':<16} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} {'月胜率':>6} {'暴露':>6}")
    b = base["stats"]
    print(f"{'基线(analog)':<14} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f} {b['avg_exposure']:>6.3f}")
    for name, _, _, _, _s in [(x[0], x[1], x[2], x[3], x[1]['stats']) for x in rows]:
        pass
    for name, r, nchg, mh in rows:
        s = r["stats"]
        print(f"{name:<16} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f} {s['avg_exposure']:>6.3f}")

    print("\n=== 逐年组合收益（基线 -> 变体，基准） ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    for name, r, _, _ in rows:
        yv = yearly(r["nav"], r["dates"], r["bench"])
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = " *" if abs(c - a) > 0.005 else ""
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline": {"cagr": b["cagr"], "maxdd": b["maxdd"], "sharpe": b["sharpe"]},
           "variants": {name: {"cagr": r["stats"]["cagr"], "maxdd": r["stats"]["maxdd"],
                               "sharpe": r["stats"]["sharpe"], "calmar": r["stats"]["calmar"],
                               "n_changed": nchg}
                        for name, r, nchg, _ in rows}}
    json.dump(out, open("../cache/lab_stage_horizon.json", "w"), ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_stage_horizon.json")


if __name__ == "__main__":
    main()
