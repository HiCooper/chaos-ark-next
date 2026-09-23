# -*- coding: utf-8 -*-
"""下跌年份优化第四轮：信号层「追高过滤」反事实（改变交易集合）。

第三轮结论：市场级 cap（C6/F3/F5）能修复宽幅熊市年（2016/2018/2022），
但 2017、2024-12 这类「主题追高年」（基准上涨、组合亏损）治不动。
归因：亏损年由「行业指数处近 60 日高位区间的追高建仓」主导
（2017-10 小金属、2023-06 游戏出版、2024-12 半导体设备/证券保险）。

生产已有 CONCLUSION_FILTER.pos60_max=0.95（剔除区间顶部 5% 的追高）。
config 注释说明 0.88~0.98 为宽平台。本轮把阈值压紧做反事实：
  G1 pos60 > 0.90 剔除
  G2 pos60 > 0.88 剔除
  G3 pos250 > 0.95 剔除（买在一年高位附近）
  G4 (pos60>0.90) ∨ (pos250>0.97) 剔除
  G5 G1 + C6（叠加市场级动量 cap）

注意：被剔除结论空出的当日名额由低分候选顶上（与生产 max_new_per_day
逻辑一致），所以这是完整的反事实重放，不只是删除交易。

运行：绝对路径直接跑。
"""
import glob
import json
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import load_trades_from_events, yearly  # noqa: E402
from lab_downyear import build_caps  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO


def build_pos_lookup() -> dict:
    """(结论日, 行业名) -> (pos60, pos250)，来自 day/<date>.json 的 picks。"""
    look = {}
    for p in glob.glob(f"{C.ROOT}/web/public/data/day/*.json"):
        d = json.load(open(p))
        date = d.get("date")
        for pk in (d.get("picks") or []):
            m = pk.get("metrics") or {}
            look[(date, pk.get("name"))] = (m.get("pos60"), m.get("pos250"))
    return look


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    pos = build_pos_lookup()
    print(f"从 review.json 重建 {len(trades)} 笔交易，pos 查表 {len(pos)} 条")

    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)

    kw = dict(start_date=prod_dates[0],
              max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
              score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])

    base = ST.simulate_etf_portfolio(book, trades, **kw)
    prod = json.load(open(f"{C.ROOT}/web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    nav_diff = max(diffs) if diffs else float("nan")
    print(f"基线复现: cagr={base['stats']['cagr']:.4f} maxdd={base['stats']['maxdd']:.4f} "
          f"| 生产 nav 最大偏差={nav_diff:.2e}")

    def filtered(trades_, pred):
        out, dropped = [], 0
        for t in trades_:
            p60, p250 = pos.get((t["date"], t["sw_name"]), (None, None))
            if pred(p60, p250):
                dropped += 1
                continue
            out.append(t)
        return out, dropped

    caps = build_caps(book, base)
    c6 = caps["C6 年线动量"]

    variants = [
        ("G1 pos60>0.90剔除", lambda a, b: a is not None and a > 0.90),
        ("G2 pos60>0.88剔除", lambda a, b: a is not None and a > 0.88),
        ("G3 pos250>0.95剔除", lambda a, b: b is not None and b > 0.95),
        ("G4 pos60∨pos250", lambda a, b: (a is not None and a > 0.90) or (b is not None and b > 0.97)),
    ]

    rows = []
    for name, pred in variants:
        ft, dropped = filtered(trades, pred)
        r = ST.simulate_etf_portfolio(book, ft, **kw)
        rows.append((name, r, dropped))
        print(f"  {name}: 剔除 {dropped} 笔")
    ft, dropped = filtered(trades, variants[0][1])
    rows.append(("G5 G1+C6", ST.simulate_etf_portfolio(book, ft, **kw, cap_series=c6), dropped))

    print("\n=== 全区间指标 ===")
    print(f"{'变体':<18} {'剔除':>5} {'cagr':>7} {'maxdd':>8} {'sharpe':>7} {'calmar':>7} {'月胜率':>6}")
    b = base["stats"]
    print(f"{'基线':<18} {'-':>5} {b['cagr']:>7.4f} {b['maxdd']:>8.4f} {b['sharpe']:>7.3f} "
          f"{b['calmar']:>7.3f} {b['monthly_win_rate']:>6.3f}")
    for name, r, dropped in rows:
        s = r["stats"]
        print(f"{name:<18} {dropped:>5} {s['cagr']:>7.4f} {s['maxdd']:>8.4f} {s['sharpe']:>7.3f} "
              f"{s['calmar']:>7.3f} {s['monthly_win_rate']:>6.3f}")

    print("\n=== 逐年收益 ===")
    yb = yearly(base["nav"], base["dates"], base["bench"])
    down_years = [y for y, (a, _) in yb.items() if a < 0]
    for name, r, _ in rows:
        yv = yearly(r["nav"], r["dates"], r["bench"])
        print(f"\n{name}")
        for y in sorted(yb):
            a, bb = yb[y]
            c, _ = yv[y]
            mark = ""
            if y in down_years and c > a + 0.004:
                mark = " ↑" + f"{(c - a) * 100:+.1f}pp"
            elif abs(c - a) > 0.005:
                mark = " *"
            print(f"  {y}: {a*100:>6.1f}% -> {c*100:>6.1f}%  (基准 {bb*100:>6.1f}%){mark}")

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline_parity_dev": nav_diff,
           "variants": {name: {"dropped": dr, **{k: r["stats"][k] for k in
                                                 ("cagr", "maxdd", "sharpe", "calmar")}}
                        for name, r, dr in rows}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_downyear4.json", "w"),
              ensure_ascii=False, indent=1)
    print("\n已写 cache/lab_downyear4.json")


if __name__ == "__main__":
    main()
