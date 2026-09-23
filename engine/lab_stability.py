# -*- coding: utf-8 -*-
"""组合层稳定性突破实验（在 ark-1.6.0 生产口径上重验 downyear5 网格 + 波动目标）。

动机：用户目标「更稳的年化 + 更小回撤」。基线 F5 = floor0.35-温和-max
（11.69% / -25.51%）。downyear5 网格里 floor0.35-更陡-max 等格子三项全优于
基线，但当时未深挖。本台在当前生产交易清单上重验并补充稳定性指标：
  · 负年数 / 最差年 / 逐年 std（年度层面的「稳」）
  · 滚动 252 日收益分布 p5/p25（任意一年期视角的「稳」，与 entry-dates 同源）
  · 水下时间（maxdd 事件的峰值→恢复天数）

网格：
  A 暴露上限：floor {0.25,0.35,0.45} × 环境档 {温和,陡,更陡}（merge=max），
    生产档 = floor0.35-温和-max
  B 波动目标：vol_target {0.16(生产), 0.14, 0.12} × 环境档 {温和,更陡}
    （floor 固定 0.35）

运行：python3 engine/lab_stability.py  （离线，秒级~分钟级）
"""
import json
import sys

import numpy as np

sys.path.insert(0, ".")
import config as C  # noqa: E402
import qfq  # noqa: E402
import settlement as ST  # noqa: E402
from lab_downside import load_trades_from_events  # noqa: E402
from lab_downyear5 import ENV_MILD, ENV_STEEP, ENV_VSTEEP, mom_cap, env_cap  # noqa: E402

RC = C.RISK_CONTROL
PF = C.PORTFOLIO


def metrics_of(nav, dates):
    nav = np.array(nav, dtype=float)
    n = len(nav)
    yrs = n / 252.0
    cagr = nav[-1] / nav[0] ** 0 if False else (nav[-1] / nav[0]) ** (1.0 / yrs) - 1.0
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1.0
    mdd = float(dd.min())
    lr = np.diff(np.log(nav))
    sharpe = float(lr.mean() / lr.std() * np.sqrt(252))
    # 水下时间：maxdd 谷底所在事件从峰值到恢复的天数
    trough = int(dd.argmin())
    pk = int(nav[:trough + 1].argmax())
    rec = next((i for i in range(trough, n) if nav[i] >= nav[pk]), None)
    underwater = (rec - pk) if rec is not None else (n - pk)
    # 滚动一年收益
    roll = nav[252:] / nav[:-252] - 1.0
    return {
        "cagr": float(cagr), "maxdd": mdd, "sharpe": sharpe,
        "calmar": float(cagr / abs(mdd)) if mdd else 0.0,
        "underwater_days": int(underwater),
        "roll_p5": float(np.percentile(roll, 5)),
        "roll_p25": float(np.percentile(roll, 25)),
        "roll_win": float((roll > 0).mean()),
    }


def year_table(nav, dates):
    """逐年收益 = 本年末净值 / 上年末净值 - 1（不用 lab_downside.yearly，其需 bench）。"""
    nav = np.asarray(nav, dtype=float)
    idx = {}
    for i, d in enumerate(dates):
        idx[d[:4]] = i
    ys, prev = {}, None
    for y in sorted(idx):
        i = idx[y]
        ys[y] = float(nav[i] / nav[prev if prev is not None else 0] - 1.0)
        prev = i
    return {y: v for y, v in ys.items() if y != "2015"}


def main() -> None:
    trades, prod_dates = load_trades_from_events()
    ep = json.load(open(f"{C.CACHE_DIR}/etf_panel.json"))
    src, _ = qfq.apply_qfq(ep, None, verbose=False)
    book = ST.EtfBook(src)

    kw = dict(start_date=prod_dates[0],
              max_holdings=C.CLASSIFY["max_daily_picks"] * 4,
              risk_parity=RC["risk_parity"], rp_window=RC["rp_window"],
              vol_target=RC["vol_target"], vol_window=RC["vol_window"],
              vol_floor=RC["vol_floor"], cash_yield=RC["cash_yield"],
              score_tilt=PF["score_tilt"], max_new_per_day=PF["max_new_per_day"])

    cap_prod = ST.build_exposure_cap(book)

    # 基线复现校验（生产 = F5 落参）
    base = ST.simulate_etf_portfolio(book, trades, **kw, cap_series=cap_prod)
    prod = json.load(open(f"{C.ROOT}/web/public/data/review.json"))["settlement"]["curve"]
    prod_map = dict(zip(prod["dates"], prod["nav"]))
    diffs = [abs(nv - prod_map[d]) for d, nv in zip(base["dates"], base["nav"])
             if d in prod_map]
    print(f"基线复现偏差={max(diffs):.2e}（应≈0）")

    rows = []

    def run(name, cap=None, vol_target=None):
        k = dict(kw)
        if vol_target is not None:
            k["vol_target"] = vol_target
        r = ST.simulate_etf_portfolio(book, trades, **k, cap_series=cap)
        m = metrics_of(r["nav"], r["dates"])
        ys = year_table(r["nav"], r["dates"])
        m["neg_years"] = sum(1 for v in ys.values() if v < 0)
        m["worst_year"] = min(ys.values())
        m["year_std"] = float(np.std(list(ys.values()), ddof=1))
        rows.append((name, m, ys))
        return m

    m0 = run("生产F5(floor0.35-温和)", cap_prod)
    print(f"\n{'变体':<24}{'cagr':>8}{'maxdd':>9}{'sharpe':>8}{'calmar':>8}"
          f"{'水下日':>7}{'滚动p5':>8}{'滚动p25':>9}{'滚动胜率':>8}{'负年':>5}{'最差年':>8}")
    def prow(name, m):
        print(f"{name:<24}{m['cagr']*100:>7.2f}%{m['maxdd']*100:>8.2f}%"
              f"{m['sharpe']:>8.3f}{m['calmar']:>8.3f}{m['underwater_days']:>7}"
              f"{m['roll_p5']*100:>7.2f}%{m['roll_p25']*100:>8.2f}%"
              f"{m['roll_win']*100:>7.1f}%{m['neg_years']:>5}{m['worst_year']*100:>7.2f}%")
    prow("生产F5(floor0.35-温和)", m0)

    print("\n=== A 暴露上限网格（floor × 环境档，merge=max） ===")
    for fl in (0.25, 0.35, 0.45):
        for tag, mapping in (("温和", ENV_MILD), ("陡", ENV_STEEP), ("更陡", ENV_VSTEEP)):
            if fl == 0.35 and tag == "温和":
                continue  # 即生产档
            name = f"floor{fl}-{tag}"
            c = np.maximum(mom_cap(book, 250, fl), env_cap(book, mapping))
            prow(name, run(name, cap=c))

    print("\n=== B 波动目标 × 环境档（floor0.35） ===")
    for vt in (0.14, 0.12):
        for tag, mapping in (("温和", ENV_MILD), ("更陡", ENV_VSTEEP)):
            name = f"vt{vt}-{tag}"
            c = np.maximum(mom_cap(book, 250, 0.35), env_cap(book, mapping))
            prow(name, run(name, cap=c, vol_target=vt))

    print("\n=== 逐年（生产 vs 候选） ===")
    names = [r[0] for r in rows]
    show = ["生产F5(floor0.35-温和)"] + [n for n in names if n != names[0]]
    years = sorted({y for _n, _m, ys in rows for y in ys})
    print(f"{'变体':<24}" + "".join(f"{y:>9}" for y in years))
    for name in show:
        ys = next(ys for n, _m, ys in rows if n == name)
        print(f"{name:<24}" + "".join(f"{ys.get(y, 0)*100:>8.2f}%" for y in years))

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "baseline_parity_dev": max(diffs),
           "variants": {n: m for n, m, _ys in rows}}
    json.dump(out, open(f"{C.CACHE_DIR}/lab_stability.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"\n已写 {C.CACHE_DIR}/lab_stability.json")


if __name__ == "__main__":
    main()
